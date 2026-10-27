# Troubleshooting

Start with the same run Tableau does, from the TabPy host's point of view:

```bash
python3 scripts/run_local.py connector.py --tabpy https://tabpy-host:9004 --tabpy-user U
```
If that works and Tableau doesn't, the problem is between Tableau and TabPy.
If it fails, the message is the same one Tableau shows.

## Messages Tableau shows

| Message | Cause | Fix |
|---|---|---|
| `tabext: secret 'x' not found on the TabPy host…` | the secret is stored on a different machine (your laptop vs Server's TabPy), or under a different OS user | run `set_secret.py x` **as the TabPy user, on that host**; Docker/K8s: env var `TABLEAU_TABEXT_X` from the platform's secret store |
| `tabext: could not reach <url> from the TabPy host…` | DNS, proxy, firewall, enclave boundary | `curl` from the TabPy host; set `HTTPS_PROXY` in TabPy's environment; a TabPy outside the data's network can't reach it |
| `tabext: TLS certificate verification failed…` | internal / DoD CA | `CA_BUNDLE = "/path/on/tabpy/host/ca.pem"`. Never `verify=False` |
| `tabext: HTTP 401 … credentials missing or rejected` | wrong / expired / rotated secret, or wrong header name | `set_secret.py NAME --check`, then re-store. Check `--auth-header` against the docs |
| `tabext: HTTP 403 …` | the credential lacks scope, or IP allowlisting | ask the API owner. Service account scopes |
| `tabext: … returned HTTP 429 and asked to wait Ns…` | rate limit / quota exhausted (e.g. NASA `DEMO_KEY`) | a real key; fewer calls per run (bigger pages, smaller window); fewer opens (extract) |
| `tabext: … exceeded TIME_BUDGET_S…` | too much data or a slow API | narrow the window or filters. Raise `TIME_BUDGET_S` **and** TabPy's `TABPY_EVALUATE_TIMEOUT` together. Large data → Prep/Hyper |
| `User defined script timed out` | TabPy's own timeout fired first (script without the runtime, or `TIME_BUDGET_S` ≥ the timeout) | keep `TIME_BUDGET_S` ≈ timeout − 5 |
| `tabext: … returned non-JSON (HTTP 200, text/html)` | a login page, SSO interstitial or proxy block page | wrong URL, missing auth, or the proxy needs auth |
| `tabext: more than MAX_ROWS…` | the safety cap | raise `MAX_ROWS` deliberately or narrow the query |
| HTTP 404 "The endpoint you're trying to query did not respond" | the script raised `KeyError('response')` **outside** `to_tableau()` (TabPy rewrites any top-level `KeyError` into this), or `tabpy.query()` named an endpoint that doesn't exist | use `dig()` / `items_at()`. Check the deployed function name. Errors raised while `to_tableau()` pulls rows come back as a 500 with `tabext: KeyError: …` instead |
| 500 `tabext: ITEMS_PATH 'data' not found in the response (top-level keys: …)` | the API returned a different shape, usually an error object with a 200 status | read the listed keys; fix `ITEMS_PATH` or handle the API's error body |
| HTTP 404 "Ad-hoc scripts have been disabled" | `TABPY_EVALUATE_ENABLE = false` on that TabPy | Table Extensions need it on |
| `Object of type Timestamp/int64 is not JSON serializable` | returned raw pandas/numpy | `return to_tableau(rows, SCHEMA)` |
| `ArrowInvalid: Could not convert …` | a column holds more than one Python type | declare the column in SCHEMA so `to_tableau` coerces it |

## Things that aren't errors

- **Dates show as text.** The script returns ISO strings by design. Use
  `patch_workbook.py --schema`, or change the type in Tableau (Tableau saves it
  as `datatype-customized`).
- **Some values are null.** `ON_BAD_VALUE = "null"` nulls unparseable values
  and logs a count to TabPy's stderr ("N value(s) in column … could not be
  coerced"). Set it to `"error"` to fail the refresh instead.
- **It's slow to open.** It runs on every open. Cache on the TabPy host
  (`connector_patterns.md`), or extract.
- **The parameter doesn't change anything.** Table Extensions can't read
  parameters. Use an input table (Custom SQL) instead.

## Between Tableau and TabPy

- Desktop: Help → Settings and Performance → Manage Analytics Extension
  Connection → Test Connection.
- Server/Cloud: site settings → Analytics Extensions. Cloud needs HTTPS with
  a public-CA certificate, auth, and the Cloud egress IPs allowed.
- On Server/Cloud, a workbook has to be published before a Table Extension can be added.
- TabPy log: script runs appear as `function to evaluate=` at INFO. Errors
  appear with a traceback at ERROR.

## `check_script` says no, but I'm sure it's fine
Every rule has a reason in `reference/table_extension_contract.md`. Errors
are behaviors TabPy really has. A warning can be justified. Say why in a
comment and move on. `patch_workbook --force` overrides errors **except
TE-SECRET**.
