# Connector patterns

All snippets go in the **FETCH** section of a scaffolded connector (never in
the runtime block) and use the runtime helpers: `http_get/http_post/
http_request`, `paginate`, `dig`, `items_at`, `get_secret`, `remember_secret`,
`check_budget`, `budget_left`, `redact`, `input_records`, `to_tableau`. Each must stay 3.8-compatible,
indented with spaces, and free of credential literals. Run `check_script.py`
after every edit.

## Choosing the approach
| Source | Use |
|---|---|
| A Tableau native connector exists and is allowed | **Use the native connector**, not a Table Extension |
| REST / JSON API, small-to-medium result (≤ ~100k rows, ≤ ~25 s) | `--kind rest` |
| GraphQL | `--kind graphql` (cursor pagination via `pageInfo { endCursor hasNextPage }`) |
| A database Tableau can't reach natively but TabPy can | `--kind sql` (SQLAlchemy + a driver on the TabPy host) |
| Huge, slow, metered, or async-export APIs, or a need to keep history | Prep flow → published Hyper (`tableau-prep-etl`), or an extract |
| Per-row enrichment of workbook data | `--input-key COL` (one call per distinct key) |

## Auth (scaffolded; notes)
- `api_key_header`: `--auth-header X-Api-Key`. **Prefer it** whenever the API accepts
  both header and query (api.data.gov does, for example).
- `api_key_query`: works, but the key ends up in proxy/server access logs.
- `bearer`: static PAT / API token. `basic`: service account user + password.
- `oauth2_cc`: `TOKEN_URL`, `OAUTH_SCOPE`. The token is cached for the run. Use
  `audience=` for Auth0-style IdPs.
- `mtls`: `CLIENT_CERT`/`CLIENT_KEY` paths on the TabPy host (plus `CA_BUNDLE`).
- **Login → session token** (two-step):
  ```python
  def auth():
      body = http_post(BASE_URL + "/auth/login",
                       json_body={"username": get_secret("acme.username"),
                                  "password": get_secret("acme.password")})
      tok = dig(body, "data.token")
      if not tok:
          raise RuntimeError("tabext: login returned no token")
      remember_secret(tok)                # so redact() hides it (also its URL/base64 forms)
      return {"Authorization": "Bearer " + tok}, {}, None
  ```
  The scaffold calls `auth()` per request, so cache the result: put a
  `_AUTH = {}` dict at top level, and return `_AUTH["v"]` if it's set.
- **HMAC-signed requests** (AWS SigV4 and similar): use the vendor SDK on the TabPy
  host (`boto3` reads env / instance-role credentials; never put keys in CONFIG).

## Pagination (scaffolded)
`page`, `offset`, `cursor`, `next_url`, `link_header` (RFC 5988 `Link:
<…>; rel="next"`), and `date_window` (walks `[start, end)` in
`WINDOW_DAYS` slices, e.g. NeoWs' 7-day maximum). All go through
`paginate(get_page, start)`, where `get_page(state) -> (items, next_state)`, and
`check_budget()` runs between pages. Stop conditions to get right:
- `page`/`offset`: stop when a page returns fewer than `PAGE_SIZE` items (or when
  `total` is reached). Don't rely on an empty page alone if the API repeats the last page.
- `cursor`: stop when the cursor is missing, empty **or unchanged**.
- Rate limits: one run must make well under the per-minute limit. Each viewer's
  open is another run.

## Shaping rows
- Nested object → `dig(rec, "a.b.c")`. First array element → `dig(rec, "items.0.x")`.
- Array that should become rows (explode):
  ```python
  def records():
      for parent in fetch():
          for child in parent.get("lines") or [ {} ]:      # keep parents with no children
              yield parent, child

  def rows():
      for p, c in records():
          yield {"order_id": dig(p, "id"), "sku": dig(c, "sku"), "qty": dig(c, "qty")}
  ```
- Dict keyed by date/id (NeoWs `near_earth_objects`): iterate over `.items()` and add the key
  as a column.
- Leave a variable-shape blob as one `string` column (the runtime JSON-encodes it).

## Non-JSON responses
- **CSV over HTTP**:
  ```python
  import csv, io
  def rows():
      text = http_get(BASE_URL + "/export.csv", headers=auth()[0], expect="text")
      for r in csv.DictReader(io.StringIO(text)):
          yield {"id": r.get("ID"), "amount": r.get("Amount")}
  ```
- **Excel**: `r = http_get(url, expect="response")`, then
  `pandas.read_excel(io.BytesIO(r.content), dtype=str)` (needs `openpyxl` on the host).
  Declare the real types in SCHEMA and let `to_tableau` coerce them.
- **XML / SOAP**: `http_request("POST", url, data=envelope, headers={"Content-Type":
  "text/xml"}, expect="text")`. Parse with `defusedxml.ElementTree` if it's
  installed (stdlib `xml.etree` doesn't resolve external entities, but is
  still exposed to entity-expansion bombs from a hostile server).
- **Gzip/zip**: `r.content`, then `gzip`/`zipfile` from the stdlib.

## SQL (`--kind sql`)
- Read-only account. Explicit column list. Bound parameters (`:since`), never
  string formatting.
- `check_budget()` only runs between `fetchmany` batches, so **set
  driver-level timeouts** or one slow query blows past TabPy's 30 s:
  - PostgreSQL: `connect_args={"connect_timeout": 5, "options": "-c statement_timeout=20000"}`
  - SQL Server (pyodbc): `connect_args={"timeout": 5}`, plus `DB_QUERY_ARGS={"driver": "ODBC Driver 18 for SQL Server", "Encrypt": "yes"}`
  - Oracle (oracledb): `conn.call_timeout = 20000` in a `@event.listens_for(engine, "connect")` hook
  - Snowflake / Databricks: use their SQLAlchemy dialects and set the statement timeout in session parameters
- TLS to the DB: `sslmode=verify-full` + `sslrootcert` (pg), `Encrypt=yes` (mssql).
- Licensed drivers (Oracle client, DB2) must be legally installed on the TabPy host.

## Input-table lookups (`--input-key COL`)
`_arg1` exists only when the Table Extension has an input table. It's a dict
of lists (JSON transport) or a DataFrame (Arrow). `input_records()` handles
both, and returns `[]` when there's no input table. The scaffold dedupes keys and checks
the budget per key. N distinct keys means N calls, so prefer a bulk endpoint (`?ids=a,b,c`
in chunks of the API's maximum) when there is one. Parameters can't be used in a
Table Extension. To feed values in, make the input table Custom SQL.

## Async export jobs (start → poll → download)
Usually a poor fit (a run must finish in ~25 s). If the job reliably completes in
seconds:
```python
job = http_post(BASE_URL + "/exports", json_body={"since": SINCE}, headers=h)
while True:
    st = http_get(BASE_URL + "/exports/" + str(job["id"]), headers=h)
    if st.get("status") == "done":
        break
    if budget_left() < 5:
        raise RuntimeError("tabext: export job still running - use a Prep flow for this API")
    _time.sleep(2)
```
Otherwise, schedule it in Prep / a job and point Tableau at the result.

## Caching across opens (optional, on the TabPy host)
Every open reruns the script. For slow or metered APIs, a short TTL cache on the
TabPy host avoids repeat calls. The cache holds **data**, so put it somewhere as
protected as the source:
```python
CACHE_TTL_S = 900
def cached(key, produce):
    d = _os.path.expanduser("~/.tableau-tabext/cache"); _os.makedirs(d, mode=0o700, exist_ok=True)
    p = _os.path.join(d, key + ".json")
    try:
        if _time.time() - _os.path.getmtime(p) < CACHE_TTL_S:
            with open(p) as fh:
                return _json.load(fh)
    except (OSError, ValueError):
        pass
    rows = list(produce())
    tmp = p + ".%d.tmp" % _os.getpid()
    with open(tmp, "w") as fh:
        _json.dump(rows, fh, default=str)
    _os.replace(tmp, p)          # atomic; concurrent viewers don't read half a file
    return rows
# ...
return to_tableau(cached("acme_orders", rows), SCHEMA)
```
Don't cache per-user data in a shared cache. The connector runs with one
credential for everyone anyway; say so to the user.

## Production pattern: deployed function (keeps the code out of the workbook)
TabPy can host a deployed function, which the Table Extension then calls by name:
```python
# deploy once, from an admin machine (tabpy-tools / tabpy.tabpy_tools.client)
from tabpy.tabpy_tools.client import Client
client = Client("https://tabpy.example:9004/")
client.set_credentials("admin", "<from your secret store>")
client.deploy("acme_orders", acme_orders_fn, "Acme orders, last 30 days", override=True)
```
Table Extension script: `return tabpy.query("acme_orders")["response"]`.
That gives central updates and keeps code and URLs out of workbooks. The function
must still return the `{col: [values]}` shape, so build it from the same
connector code (`to_tableau` included). Note: if the endpoint name is wrong,
TabPy answers with the misleading 404 "endpoint … did not respond".
(Verified on TabPy 2.16: `tabpy.query(name)["response"]` returns the deployed function's dict, 200. A wrong name gives the 404 above. Not yet verified from Tableau itself.)
