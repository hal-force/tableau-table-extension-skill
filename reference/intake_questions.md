# Intake: what to ask, and what to find out yourself

Read the API or source docs **first**. Ask the user only what the docs and the
workbook can't tell you. Use a single AskUserQuestion call (max 4 questions,
2–4 concrete options each, your recommendation first and marked "(Recommended)").
Ask a second round only if an answer opens a new unknown (e.g. "Tableau Cloud"
→ "is your TabPy reachable from Cloud over public-CA HTTPS?").

## Find out yourself (don't ask)
| Item | Where |
|---|---|
| Base URL, auth schemes offered, header vs query param | API docs |
| Pagination style + max page size | API docs ("next", "cursor", "Link", "offset") |
| Rate limits, quotas, `Retry-After` | API docs, response headers |
| Record path + field names/types | docs, or one safe sample call (`DEMO_KEY` / public endpoint) |
| Existing grain, columns, sheets that depend on them | `inspect_workbook.py` |
| Whether the original script embeds a credential | `inspect_workbook.py` (count only) |

## The question menu (pick ≤ 4 that are actually open)

**1. Where does TabPy run?** (decides secret storage and the network path)
- TabPy on this Mac (Tableau Desktop) *(the usual choice for authoring)*
- A Linux/Docker TabPy (`set_secret.py --backend file` or an env var from the
  orchestrator's secret store)
- The TabPy that Tableau Server uses (secrets go on that host, managed by its admin)
- Tableau Cloud → TabPy must be HTTPS with a **public-CA** cert, auth on, and
  reachable from Cloud (allowlist Cloud egress IPs). No self-signed or private PKI.

**2. Auth and whose credential?**
- Service account / scoped read-only token *(recommended for anything published)*
- My personal token (fine for a private prototype; it runs as you for every viewer)
- OAuth client credentials (client id + secret; give the token URL and scope)
- Mutual TLS / PKI (a software cert + key on the TabPy host; a CAC on a smart card
  can't be used by a headless server)
- No auth / public API

**3. Can the TabPy host reach the API?**
- Yes, it's public internet and TabPy has internet access
- Through a corporate proxy (`HTTPS_PROXY` in TabPy's environment)
- Internal endpoint with an internal / DoD CA (needs `CA_BUNDLE` on the host)
- Different enclave or network from TabPy → it won't work there. Stop and pick a
  TabPy inside the data's network (or move the data)

**4. What should one row be, and how much data?**
- Offer 2–3 concrete grains from the docs ("one row per order", "per order
  line", "per day per site") and a default window ("last 30 days")
- Point out the ~25 s budget per run and calls per run vs the rate limit

**5. How fresh, and how many viewers?** (it reruns on every open and refresh)
- Live-ish: each open hits the API (OK for small calls / few viewers)
- Daily is enough: suggest an extract of the data source, or Prep → published
  Hyper (`tableau-prep-etl` skill) if the API is slow, metered or rate-limited
- Needs history the API doesn't keep: a Table Extension stores nothing → Prep
  plus an append, or a database

**6. Input table?** Only if the user wants lookups driven by data in the
workbook ("enrich each customer row"). → `--input-key COL`. Note: N rows = N
calls, so dedupe (the scaffold does) and watch the budget. Parameters can't
be used. An input table (e.g. Custom SQL) is the only way to feed values in.

**7. Data sensitivity?** CUI / PII / classified data passes through the TabPy
host's memory and, on error, possibly its logs. Confirm the TabPy host is
accredited for it (IL level, ATO boundary). Never print rows in diagnostics.

## Defaults to state rather than ask
`TIME_BUDGET_S = 25`, `RETRIES = 2`, connect timeout 10 s, read timeout 20 s,
`MAX_ROWS = 500000`, `ON_BAD_VALUE = "null"`, UTC timestamps, snake_case
columns, header auth when the API offers it, the smallest useful default window.
