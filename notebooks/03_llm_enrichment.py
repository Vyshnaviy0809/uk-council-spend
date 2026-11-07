import os, sys, json, time, getpass
import duckdb
import pandas as pd
from pydantic import BaseModel
from google import genai
from google.genai import types, errors

MODEL = sys.argv[1] if len(sys.argv) > 1 else "gemini-3.8-flash"
PROMPT_VERSION = "v1"
BATCH_SIZE = 10
PAUSE_SECONDS = 5

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB = os.path.join(ROOT, "council.duckdb")
CACHE_FILE = os.path.join(ROOT, "data", "cache", f"llm_{PROMPT_VERSION}.json")
OUT_CSV = os.path.join(ROOT, "data", "processed", f"suppliers_llm_{PROMPT_VERSION}.csv")

CATEGORIES = [
    "Social Care", "Education & Childcare", "Construction & Property", "IT & Telecoms",
    "Energy & Utilities", "Transport & Travel", "Waste & Environment", "Professional Services",
    "Staffing & Recruitment", "Housing & Accommodation", "Public Sector Bodies",
    "Community & Voluntary", "Culture, Leisure & Events", "Goods & Supplies", "Other",
]

PROMPT_V1 = """You are cleaning supplier names from Liverpool City Council's spending data (payments over £500).

For each supplier below, return:
- vendor_raw: copied exactly as given
- standard_name: the organisation's proper name in title case, without punctuation noise.
  Names were truncated at 35 characters; complete them if you are confident.
  Different spellings of the same organisation must get the same standard_name.
- category: exactly one of: {categories}
- is_person: true if the supplier looks like a private individual rather than an organisation

The expense type is a hint, but the supplier name matters most.

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
    return PROMPT_V1.format(categories=", ".join(CATEGORIES), rows=rows)


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

    con = duckdb.connect(DB, read_only=True)
    suppliers = con.sql("""
        SELECT vendor_raw, main_expense_type, n_payments, total_spend
        FROM supplier_rules
        ORDER BY upper(vendor_raw)
    """).df()
    con.close()

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
