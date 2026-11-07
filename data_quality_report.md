# Data Quality Report: Liverpool City Council spend over £500 (Jan–Jun 2025)

**Source:** Liverpool City Council, published on Data Mill North. Six monthly CSV files (only January–June 2025 were available).
**Rows loaded:** 77,033

| # | Check | Finding | Impact / action |
|---|---|---|---|
| 1 | Amount format | Stored as text with £ sign and commas (e.g. "£15,499.00") | Converted to a number; every row converted successfully |
| 2 | Missing values | 2 rows with a blank Service Area; no other blanks | Kept, labelled "Unknown" |
| 3 | Exact duplicate rows | 3 rows identical to another row | Could be genuine repeat payments, so kept but noted |
| 4 | Repeated document numbers | 46 SAP document numbers appear more than once | Probably one invoice split across several lines; kept |
| 5 | Negative amounts | 1,221 rows (1.6%), totalling -£15,829,129.42 | Refunds or corrections; kept so totals are net |
| 6 | Dates outside the file's month | 18,847 rows (24.5%) have a posting date outside the month of their file; 2,388 are before 2025 (earliest 2023-11-21) | Files seem to group payments by when they were reported, not posted. Analysis uses posting date |
| 7 | Redacted suppliers | 7,549 rows (9.8%) show "REDACTED PERSONAL DATA", worth £15,073,471.74 | Payments to individuals; excluded from supplier analysis (GDPR) |
| 8 | Truncated supplier names | Names are capped at 35 characters; 288 distinct names hit the cap (e.g. "LIVERPOOL STREETSCENE SERVICES LIMI") | Handled in supplier standardisation (Phases 3–4) |
| 9 | Supplier spelling variants | 2,532 distinct supplier names; 37 groups differ only by case, spacing or punctuation (e.g. MERSEYTRAVEL / MerseyTravel / Merseytravel) | Main target for cleaning in Phases 3–4 |

**Net spend in the data:** £421,596,634.99. **Largest single payment:** £10,489,500.00.
