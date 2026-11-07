import os, sys, json, time, getpass
import duckdb
import pandas as pd
from pydantic import BaseModel
from google import genai
from google.genai import types, errors

MODEL = sys.argv[1] if len(sys.argv) > 1 else "gemini-3.1-flash-lite"
PROMPT_VERSION = "v2"
BATCH_SIZE = 25
PAUSE_SECONDS = 5

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB = os.path.join(ROOT, "council.duckdb")
CACHE_FILE = os.path.join(ROOT, "data", "cache", f"llm_{PROMPT_VERSION}.json")
OUT_CSV = os.path.join(ROOT, "data", "processed", f"suppliers_llm_{PROMPT_VERSION}.csv")
LABELS = os.path.join(ROOT, "data", "labels", "labels.csv")

CATEGORIES = [
    "Social Care", "Education & Childcare", "Construction & Property", "IT & Telecoms",
    "Energy & Utilities", "Transport & Travel", "Waste & Environment", "Professional Services",
    "Staffing & Recruitment", "Housing & Accommodation", "Public Sector Bodies",
    "Community & Voluntary", "Culture, Leisure & Events", "Goods & Supplies", "Other",
]

PROMPT_V2 = """You are cleaning supplier names from Liverpool City Council's spending data (payments over £500).

For each supplier below, return:
- vendor_raw: copied exactly as given
- standard_name: the organisation's proper name.
  - Remove legal suffixes: Ltd, Limited, PLC, LLP.
  - Use normal capitalisation but keep acronyms in capitals (UK, NHS, TLT, CIC).
  - Names were truncated at 35 characters; complete them only if you are confident.
  - Give exactly the same standard_name to different spellings of the SAME organisation.
  - Keep branch, site or location names: the same chain in different places is a separate supplier
    (e.g. "X Nursery" and "X Nursery Northside" stay different; a care home run by a group keeps its own home name).
  - If the name is too vague to identify an organisation, keep it as given. Never invent a fuller name.
- category: exactly one of the categories below
- is_person: true if the supplier looks like a private individual rather than an organisation

Categories (choose by what the organisation mainly does):
- Social Care: care homes, home care, supported living, fostering/adoption, and charities whose main work is
  supporting vulnerable people (homelessness, domestic abuse, addiction, disability, mental health)
- Education & Childcare: schools, academies, nurseries, colleges, training and exam providers
- Construction & Property: builders, contractors, roofing, repairs, building works
- IT & Telecoms: software, IT services, networks, phones, technology equipment
- Energy & Utilities: electricity, gas, water, fuel, power equipment
- Transport & Travel: taxis, buses, coaches, vehicle hire, logistics, travel booking
- Waste & Environment: waste, recycling, cleaning services, grounds maintenance
- Professional Services: law firms, consultants, surveyors, accountants, insolvency and financial advisers,
  insurance, security services, membership and professional bodies
- Staffing & Recruitment: recruitment and temporary staffing agencies
- Housing & Accommodation: hotels, housing associations, hostels, landlords
- Public Sector Bodies: councils, NHS, police, combined authorities, government bodies
- Community & Voluntary: community groups, residents' associations, youth and community centres, general charities
- Culture, Leisure & Events: arts, theatre, music, sport, festivals, entertainers
- Goods & Supplies: shops, pharmacies, equipment, furniture, printing, books, play and leisure equipment
- Other: private individuals, payment references that are not organisations (e.g. containing "BACS"),
  and anything you cannot identify

The expense type is the council's budget code, not a description of the supplier. It is often misleading
(e.g. a consultant paid from a "Build Work" budget is still Professional Services). Use it only when the
name gives no clue.

Suppliers (vendor_raw ||| expense type):
{rows}
"""


class SupplierResult(BaseModel):
    vendor_raw: str
    standard_name: str
    category: str
    is_person: bool


CONFIG = types.GenerateContentConfig(
    response_mime_type="application/json",
    response_schema=list[SupplierResult],
    temperature=0,
    thinking_config=types.ThinkingConfig(thinking_level="low"),
)
PLAIN_CONFIG = types.GenerateContentConfig(
    response_mime_type="application/json",
    response_schema=list[SupplierResult],
    temperature=0,
)


def log(*args):
    print(*args, flush=True)


def build_prompt(batch):
    rows = "\n".join(f"{r.vendor_raw} ||| {r.main_expense_type}" for r in batch.itertuples())
    return PROMPT_V2.format(rows=rows)


def call_gemini(client, prompt):
    global CONFIG
    for _ in range(3):
        try:
            response = client.models.generate_content(model=MODEL, contents=prompt, config=CONFIG)
            return json.loads(response.text or "[]")
        except json.JSONDecodeError:
            log("  bad JSON, retrying")
        except (TimeoutError, OSError) as e:
            log(f"  no reply in time ({type(e).__name__}), retrying")
        except Exception as e:
            if type(e).__name__ in ("ReadTimeout", "ConnectTimeout", "ConnectError", "RemoteProtocolError"):
                log(f"  no reply in time ({type(e).__name__}), retrying")
                continue
            if isinstance(e, errors.APIError) and e.code == 400 and "think" in str(e.message).lower() and CONFIG is not PLAIN_CONFIG:
                log("  model does not support thinking_level, switching it off")
                CONFIG = PLAIN_CONFIG
                continue
            if not isinstance(e, errors.APIError):
                raise
            if e.code == 429 and "day" not in str(e.message).lower():
                log("  rate limited, waiting 60s")
                time.sleep(60)
            elif e.code in (500, 502, 503, 504):
                log(f"  server error {e.code}, waiting 20s")
                time.sleep(20)
            else:
                raise
    return []


def main():
    api_key = os.environ.get("GEMINI_API_KEY") or getpass.getpass("Gemini API key: ")
    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=90_000, retry_options=types.HttpRetryOptions(attempts=1)),
    )

    log(f"model: {MODEL}")
    log("testing connection...")
    t0 = time.time()
    try:
        reply = client.models.generate_content(model=MODEL, contents="Reply with the word OK").text
        log(f"  ok ({time.time() - t0:.0f}s): {reply!r}")
    except Exception as e:
        log(f"  FAILED after {time.time() - t0:.0f}s: {type(e).__name__}: {e}")
        return

    labelled = pd.read_csv(LABELS)["vendor_raw"]
    con = duckdb.connect(DB, read_only=True)
    suppliers = con.sql("""
        SELECT vendor_raw, main_expense_type, n_payments, total_spend
        FROM supplier_rules
        ORDER BY upper(vendor_raw)
    """).df()
    con.close()
    suppliers = suppliers[suppliers["vendor_raw"].isin(labelled)].reset_index(drop=True)

    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    cache = json.load(open(CACHE_FILE)) if os.path.exists(CACHE_FILE) else {}
    todo = suppliers[~suppliers["vendor_raw"].isin(cache.keys())]
    log(f"suppliers: {len(suppliers)}  cached: {len(cache)}  to do: {len(todo)}")

    try:
        for start in range(0, len(todo), BATCH_SIZE):
            batch = todo.iloc[start:start + BATCH_SIZE]
            log(f"sending {len(batch)} suppliers ({batch['vendor_raw'].iloc[0][:25]}...)")
            t0 = time.time()
            results = call_gemini(client, build_prompt(batch))
            asked = set(batch["vendor_raw"])
            for r in results:
                if r.get("vendor_raw") in asked:
                    cache[r["vendor_raw"]] = r
            json.dump(cache, open(CACHE_FILE, "w"), indent=1)
            log(f"{len(cache)}/{len(suppliers)}  ({time.time() - t0:.0f}s)")
            time.sleep(PAUSE_SECONDS)
    except errors.APIError as e:
        log("STOPPED:", e.code, e.message)
    except KeyboardInterrupt:
        log("stopped by you - progress is saved")

    missing = set(suppliers["vendor_raw"]) - set(cache)
    log("missing:", len(missing))
    if missing:
        log("run the same command again later to finish")
        return

    llm = pd.DataFrame(cache.values())
    llm = llm[llm["vendor_raw"].isin(suppliers["vendor_raw"])]
    invalid = ~llm["category"].isin(CATEGORIES)
    llm.loc[invalid, "category"] = "Other"
    llm = llm.rename(columns={
        "standard_name": f"std_name_llm_{PROMPT_VERSION}",
        "category": f"category_llm_{PROMPT_VERSION}",
        "is_person": f"is_person_llm_{PROMPT_VERSION}",
    })

    con = duckdb.connect(DB)
    con.sql(f"CREATE OR REPLACE TABLE supplier_llm_{PROMPT_VERSION} AS SELECT * FROM llm")
    con.close()
    llm.to_csv(OUT_CSV, index=False)

    log("\ninvalid categories:", invalid.sum())
    log("distinct standard names:", llm[f"std_name_llm_{PROMPT_VERSION}"].nunique())
    log("flagged as individuals:", llm[f"is_person_llm_{PROMPT_VERSION}"].sum())
    log(llm[f"category_llm_{PROMPT_VERSION}"].value_counts().to_string())
    log(f"\nsaved {len(llm)} rows")


if __name__ == "__main__":
    main()
