# The Table Extension contract

What a script must do to work as a Tableau Table Extension, and why.
Facts are tagged by source:
**[TabPy]** = read in TabPy 2.x source (`tabpy_server/handlers/evaluation_plane_handler.py`,
`base_handler.py`, `app/app.py`), verified by running TabPy 2.16 locally.
**[Docs]** = help.tableau.com pages `td_table_extensions.htm`,
`tc_table_extensions.htm`, `ts_table_extensions.htm`, `config_r_tabpy.htm`.
**[Unconfirmed]** = plausible, not verified. Don't state these as fact to users.

## How Tableau and TabPy run the script

1. Tableau POSTs `{"script": "<text>", "data": {"_arg1": {...}}}` to
   `/evaluate`. `data` holds the input table only if the Table Extension has
   one. **[Docs]** `_arg1` is a dict of column → list. **[TabPy]** With the
   Arrow transport it's a pandas DataFrame instead.
2. **[TabPy]** The script is wrapped:
   ```python
   def _user_script(tabpy, _arg1):      # or (tabpy) when there's no data
    <each line of the script, prefixed with ONE space>
   ```
   (`script.splitlines()`, then `" " + line`.) Then it's `exec`'d into the handler
   module's globals and called on a thread-pool worker, with a timeout of
   `TABPY_EVALUATE_TIMEOUT` (default **30 s**; a timeout → "User defined script timed out").
3. **[TabPy]** A DataFrame result is converted with `to_dict(orient='list')`.
   The JSON response is `simplejson.dumps(result, ignore_nan=True)`. The Arrow
   path runs `pyarrow.table(result)`.
4. **[Docs]** Tableau reads the returned dict as a table: keys are columns,
   values are equal-length lists.
5. **[Docs]** The script reruns every time the workbook is opened or the data
   source refreshes ("similar to a live connection"). **[Docs]** Parameters
   can't be referenced in the script.
   **[Docs]** On Server/Cloud, the workbook must be published before you can add a
   Table Extension, and the site's analytics extension must allow scripts
   (`TABPY_EVALUATE_ENABLE`, and a 404 "Ad-hoc scripts have been disabled" otherwise).
   **[Docs]** Tableau Cloud needs a TabPy reachable from Cloud over HTTPS with a
   certificate from a **public CA** (no self-signed or private PKI) and auth.
   If it's firewalled, allowlist the Cloud pod's egress IPs (the docs list
   44.224.205.196 and 44.230.200.109 for some pods; check the current list for yours).
6. **[Unconfirmed]** Behavior with 0 rows (we return all columns with
   empty lists, which TabPy accepts); whether extracts / scheduled refresh
   rerun the script; minimum Tableau version per feature; any
   Tableau-side timeout separate from TabPy's.

## Rules (`check_script.py` ids)

| Rule | Level | Why |
|---|---|---|
| TE-TABS | error | The one-space prefix plus a tab gives inconsistent indentation → `TabError` inside the wrapper **[TabPy]** |
| TE-LINESEP | error | `str.splitlines()` also splits on raw `\x0b \x0c \x1c \x1d \x1e \x85`, U+2028 and U+2029. Inside a string literal that breaks the line, so the script no longer compiles. Write them as escapes (`\u2028`) **[TabPy]** |
| TE-COMPILE | error | The script doesn't compile **inside the wrapper**. A top-level `return` is legal there (and required), but e.g. `from __future__ import ...` is a SyntaxError because it's no longer the first statement of a module |
| TE-PYVER | error | Syntax newer than `--py` (default 3.8) per `ast.parse(feature_version=...)`: e.g. `match`, `except*`, parenthesized context managers. It doesn't catch newer *library* calls (`str.removeprefix` is 3.9, runtime `X \| Y` unions are 3.10), so also run on the TabPy host's Python |
| TE-SECRET | error | A credential-looking literal. The workbook is shareable and TabPy logs the full script at **INFO** ("function to evaluate=…") **[TabPy]**. Rotate any that were ever saved |
| TE-TODO | error | An unresolved `TODO(tabext)` scaffold marker |
| TE-RETURN | error | No top-level `return`: the wrapper returns `None` → Tableau error |
| TE-RETURN-LAST | warning | The last top-level statement isn't the `return`, so code after it is dead or the result is conditional |
| TE-RETURN-NONE | error | `return` / `return None` at top level |
| TE-RETURN-SHAPE | warning | Returns something that's visibly not a dict / DataFrame (a list, a string, a number) |
| TE-ORIENT | error | `to_dict(orient=...)` that isn't `'list'`. Records/index shapes aren't a table Tableau can read |
| TE-NOCOERCE | warning | Returns raw `df.to_dict('list')`. pandas `Timestamp`, `datetime`, `date`, numpy `int64`/`bool_` **fail** `simplejson.dumps` (500 "not JSON serializable"); `float64` and `Decimal` pass. Mixed-type columns fail `pyarrow.table` (`ArrowInvalid`); all-`None` columns become Arrow type `null` **[TabPy + tested]**. `to_tableau()` fixes all of this |
| TE-YIELD / TE-AWAIT | error | A top-level `yield` turns `_user_script` into a generator (TabPy can't serialize it). `await` is a syntax error in a plain def (reported as TE-AWAIT rather than TE-COMPILE) |
| TE-GLOBAL | warning | The function runs in the TabPy handler module's globals. `global x` writes into TabPy's own module, shared across requests and users |
| TE-MAIN | error | `if __name__ == "__main__":` is never true there (`__name__` is the handler module), so the guarded code silently doesn't run |
| TE-SIGNAL | error | `signal.*` only works on the main thread; the script runs on a worker thread |
| TE-EXIT | error | `sys.exit`, `exit` and `quit` raise `SystemExit`, which escapes a worker and takes the request (or worse) with it. Raise `RuntimeError("tabext: …")` instead |
| TE-INTERACTIVE | error | `input()` / `getpass()`: TabPy is headless; the call fails or hangs until the timeout. (Also avoid `breakpoint()`, `pdb`, `webbrowser`; those aren't checked) |
| TE-PRINT | info | `print()` goes to TabPy's stdout/log, not to Tableau. Fine for diagnostics; never print data or secrets |
| TE-TIMEOUT | error | `requests.*` / a session call without `timeout=` can hang the full 30 s and tie up a TabPy worker. Use the runtime's `http_*` |
| TE-TLS | error | Turning certificate checks off: `verify=False`, `ssl=False`, `.verify_mode = 0`, `CERT_NONE`, `_create_unverified_context`, and dict/DSN options (`sslmode` disable/allow/prefer/require, `TrustServerCertificate=yes`, `Encrypt=no`). It's a lint, not a sandbox: an obfuscated bypass can slip past. For an internal / DoD CA, set `CA_BUNDLE` to a PEM on the TabPy host |
| TE-HTTP | error/warning | A plain `http://` URL to a non-loopback host. Error when the script calls `get_secret()` (the runtime refuses to send credentials over clear text, and they would cross the network unencrypted); warning otherwise. `localhost`, `127.*` and `[::1]` are allowed |
| TE-NAME | error | A name is read but never bound in the script, the wrapper's parameters (`tabpy`, `_arg1`) or builtins: a `NameError` on refresh. Usually a CONFIG constant that was deleted or misspelled |
| TE-ARG1 | warning | Uses `_arg1` directly. It's only defined when the Table Extension has an input table (otherwise `NameError`), and it's a dict *or* a DataFrame depending on transport. Use `input_records()`. `--uses-input` silences it |
| TE-RUNTIME | warning | No TABEXT runtime block (an older or hand-written script) |
| TE-RUNTIME-DRIFT | error/warning | The runtime block differs from `runtime/tabext_runtime.py` (error if same version: someone hand-edited it; warning if an older version: run `new_connector.py --refresh-runtime`) |
| TE-CONFIG | error | The runtime needs top-level `TIME_BUDGET_S, CONNECT_TIMEOUT_S, READ_TIMEOUT_S, RETRIES, CA_BUNDLE, CLIENT_CERT, CLIENT_KEY, MAX_ROWS, ON_BAD_VALUE` |
| TE-BUDGET | warning | `TIME_BUDGET_S` > 28: TabPy kills the request at 30 s by default before the runtime can return a readable error. Raise `TABPY_EVALUATE_TIMEOUT` on the host too |
| TE-SCHEMA | error | `SCHEMA` isn't a literal dict of `string/integer/real/boolean/date/datetime` |
| TE-COLNAME | error/warning | Error: column names that collide ignoring case, or contain `[` `]` (they break Tableau field references). Warning: names over 120 characters |
| TE-DEPS | info | Third-party imports the TabPy host must have installed (`requests` when `http_*` is used; `keyring` is optional) |

## Output contract (`run_local.py` validates this)

- Return a `dict` with `str` keys. Each value is a `list`, all lists are the same
  length, and there's ≥ 1 column.
- Each column holds one Python type from `str | int | float | bool`, plus `None`.
  `float('nan')` serializes as `null` (`ignore_nan=True`).
- Dates and datetimes go out as **strings** (`YYYY-MM-DD`, `YYYY-MM-DD HH:MM:SS`,
  UTC). Tableau records them as strings. `patch_workbook.py --schema` sets
  `datatype='date'|'datetime'` with `datatype-customized='true'`, which is what
  Tableau itself writes when a user changes the type.
- Keep it well under `TABPY_MAX_REQUEST_SIZE_MB` (100 MB default, which applies to the
  request) and `MAX_ROWS`. A Table Extension is for API-sized data. For millions
  of rows, use Prep/Hyper.

## The runtime's own guarantees
`to_tableau(rows, SCHEMA)` fixes the column order, coerces each value to the declared
type (numeric strings with thousands separators, epoch s/ms/ns, ISO with `Z`/offsets → UTC,
`yes/no/1/0/t/f` booleans, dict/list → JSON string, numpy scalars → Python),
and nulls out bad values (counted on stderr) unless `ON_BAD_VALUE = "error"`.
Any exception raised while it pulls rows becomes a one-line `tabext:` error,
redacted and with query strings removed.
`http_request` derives the read timeout from the remaining budget, retries 429/5xx
with `Retry-After` only while the budget allows it, and turns TLS, DNS, 401, 403 and 404
failures into one-line `tabext:` messages with every resolved secret redacted
and the query string removed.
