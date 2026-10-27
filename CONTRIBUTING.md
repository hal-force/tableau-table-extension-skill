# Contributing

## Ground rules

- **Preview status** (`v0.1.x-preview`): interfaces may change.
- **License:** contributions are Apache-2.0 (see `LICENSE`).
- **No secrets, ever.** No API keys, tokens, `.env`, `secrets.json`, `.pem`/`.key`
  files, TabPy logs, or Tableau workbooks/extracts (`*.twb*`, `*.hyper` and `*.tds*` are
  gitignored because they can carry credentials and data). If one gets
  staged, stop, remove it from history, and **rotate the credential**.
- **No non-public data** in `examples/`. Use public APIs (`DEMO_KEY`-style)
  and placeholder hostnames.
- **Scripts never print secret values**, only presence and length. That
  includes error paths: route messages through `redact()` (runtime) or
  `redact_text()` (authoring).

## Setup

```sh
git clone https://github.com/hal-force/tableau-table-extension-skill.git
cd tableau-table-extension-skill
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -I tests/test_skill.py        # must stay all-pass
```

Through a real TabPy (recommended before any change to the runtime). This is a throwaway,
loopback-only server with a random password that's held only in this shell:

```sh
PW=$(openssl rand -hex 16)
.venv/bin/tabpy-user add -u tester -p "$PW" -f /tmp/tabpy-pwd.txt
printf '[TabPy]\nTABPY_PORT = 9137\nTABPY_PWD_FILE = /tmp/tabpy-pwd.txt\n' > /tmp/tabpy.conf
TABLEAU_TABEXT_MOCK_TOKEN=mock-token-123456 .venv/bin/tabpy --config /tmp/tabpy.conf > /tmp/tabpy.out 2>&1 &
TABLEAU_TABEXT_TABPY_PASSWORD="$PW" .venv/bin/python -I tests/test_skill.py \
    --tabpy http://127.0.0.1:9137 --tabpy-user tester
kill %1; rm -f /tmp/tabpy-pwd.txt /tmp/tabpy.conf /tmp/tabpy.out
```

## Changing the runtime

1. Edit `runtime/tabext_runtime.py`. Keep it Python 3.8, spaces only, and
   valid inside the TabPy wrapper. Bump `v1` → `v2` in **both** markers if
   behavior changes (so `check_script` reports old copies as warnings, not drift
   errors).
2. `python3 scripts/new_connector.py --refresh-runtime examples/nasa_neows/nasa_neows.py`
3. Run the tests, both in-process and `--tabpy`.

## Adding a checker rule

Add it in `scripts/check_script.py` with a new `TE-*` id, a row in
`reference/table_extension_contract.md` stating **why** (with a TabPy
source or Tableau doc reference), and a case in `tests/test_skill.py`
(`test_checker`). Error = Tableau/TabPy will fail. Warning = likely wrong.
Info = something the host admin needs to know.

## Adding an auth or pagination style

Add a branch in `scripts/new_connector.py` (`auth_block` / the pagination
templates), make sure every combination compiles (the tests scaffold a
matrix), and document it in `reference/connector_patterns.md`.

## CLI changes

Update the argparse help text, then run `python3 tools/gen_cli_docs.py`
(the tests fail if `docs/cli.md` is stale).
