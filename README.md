# tableau-table-extension-skill

A [Claude Code](https://claude.com/claude-code) skill that builds **Tableau
Table Extensions**: data-model tables whose rows come from a Python script
on [TabPy](https://github.com/tableau/TabPy). It connects a workbook to a REST
or GraphQL API, a database Tableau can't reach natively, a file endpoint, or
per-row lookups, **with the Python written to TabPy's actual execution
contract and no credential in the workbook or the chat.**

You say "connect this dashboard to the Acme orders API". The skill:

1. **Reads the API docs** and any existing workbook (`inspect_workbook.py`
   lists each Table Extension, its columns and types, the sheets that use it,
   and any embedded credentials, by count only).
2. **Asks only what the docs can't answer**, in one round: where TabPy runs
   (Desktop, Linux/Docker, Server, or Cloud with public-CA HTTPS), auth and
   whose credential, whether the TabPy host can reach the API, grain and volume
   vs the 30 s limit, and refresh cadence (it reruns on every open).
3. **Scaffolds a connector** (`new_connector.py`: REST, GraphQL or SQL × 7 auth
   schemes × 7 pagination styles × optional input-table lookups) around a
   tested runtime:
   - secrets resolved on the TabPy host (env → Keychain → keyring → file);
   - timeouts, plus 429/5xx retries that respect `Retry-After` but stop within budget;
   - error messages that are readable in Tableau and have secrets redacted;
   - `to_tableau()` coercion to the one shape both of TabPy's transports
     (JSON and Arrow) accept.
4. **Checks it statically** (`check_script.py`): it compiles inside TabPy's exact
   `def _user_script(tabpy, _arg1):` wrapper with Python 3.8 syntax, and applies 30
   rules, each tied to TabPy source or Tableau docs.
5. **Stores credentials without exposure** (`set_secret.py`: hidden dialog,
   no-echo prompt, or file; it reports only "stored (N chars)").
6. **Runs it the way TabPy does** (`run_local.py`, in-process emulation or a
   real TabPy `/evaluate`), validates it against the contract, and writes `schema.json`.
7. **Writes the workbook** (`patch_workbook.py replace|new`). It sets output
   column types (including date and datetime), warns about broken sheets, and
   refuses to write if any credential remains.

## Install
```bash
git clone https://github.com/hal-force/tableau-table-extension-skill.git ~/claude-projects/tableau-table-extension-skill
ln -s ~/claude-projects/tableau-table-extension-skill ~/.claude/skills/tableau-table-extension
cd ~/claude-projects/tableau-table-extension-skill
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # authoring + local testing
```
On the **TabPy host**, install `requests>=2.32.4` (plus `sqlalchemy` and a driver for SQL
connectors, and `keyring` optionally).

## Try it
```bash
.venv/bin/python -I tests/test_skill.py           # 130 checks against a local mock API
.venv/bin/python -I tests/test_skill.py --tabpy http://127.0.0.1:9004 [--tabpy-user U]   # + real TabPy (156)
```
The worked example `examples/nasa_neows/` rebuilds the NASA "Close
Approaches" workbook's Table Extension: header auth, a 7-day date-window
pager, typed columns, and no embedded key.

## Layout
```
SKILL.md                      workflow + hard rules (what Claude follows)
runtime/tabext_runtime.py     canonical runtime inlined into every connector
scripts/                      inspect / scaffold / check / set_secret / run / patch
reference/                    contract (rule ids + why), intake, patterns, security, XML
docs/                         architecture, CLI reference (generated), troubleshooting
examples/nasa_neows/          worked example
tests/test_skill.py           self-test (mock API; optional real TabPy)
tools/gen_cli_docs.py         regenerates docs/cli.md from --help
```

## Documentation
| Read | For |
|---|---|
| [docs/architecture.md](docs/architecture.md) | how a run works end to end, and why the code is shaped the way it is |
| [docs/cli.md](docs/cli.md) | every script and flag |
| [docs/troubleshooting.md](docs/troubleshooting.md) | each error Tableau can show, with its cause and fix |
| [reference/table_extension_contract.md](reference/table_extension_contract.md) | the TabPy / Tableau rules, each with its source |
| [reference/connector_patterns.md](reference/connector_patterns.md) | auth, pagination, CSV/Excel/XML, SQL timeouts, input lookups, caching, deployed functions |
| [reference/security.md](reference/security.md), [SECURITY.md](SECURITY.md) | where secrets live per host, leak channels, hardening, residual risks |
| [CONTRIBUTING.md](CONTRIBUTING.md) | dev setup, runtime changes, new rules |

## Security notes
- TabPy logs every script at **INFO**. Its default logging config also runs
  urllib3 at **DEBUG**, which logs request URLs including query-string API keys.
  The runtime silences urllib3, and the scaffold prefers header auth.
- A credential that was ever saved in a workbook or ran through TabPy must be
  **rotated**. Removing it from the file doesn't remove copies or log lines.
- See `reference/security.md` for per-host secret storage and TabPy hardening.

## Limits / not yet verified
- Workbooks written by `patch_workbook.py` haven't yet been opened in Tableau Desktop
  as part of the tests (the XML matches what Tableau writes).
- Zero-row results, extract / scheduled-refresh behavior, and minimum Tableau
  versions are unconfirmed.
- A Table Extension reruns on every open and stores no history. For big, slow
  or metered sources use Prep → Hyper
  ([tableau-prep-etl-skill](https://github.com/hal-force/tableau-prep-etl-skill)).
