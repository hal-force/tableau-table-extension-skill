# Changelog

## Unreleased: code + security review fixes (runtime v3), 2026-10-27

Security
- A credential hidden between the runtime markers was exempt from TE-SECRET
  and from `patch_workbook`'s refusal. Now only the byte-identical canonical
  block is exempt, and a same-version hand-edited block is refused even with
  `--force`.
- `new_connector --input-key` was interpolated into the generated code
  (code injection). It's now validated and emitted as a CONFIG literal.
- Credential-named headers (`X-Api-Key`, `Cookie`, …) now block cross-host
  redirects, like `Authorization` does.
- The runtime session ignores `~/.netrc` (`trust_env = False`), honors
  `REQUESTS_CA_BUNDLE` / `CURL_CA_BUNDLE`, and always passes `verify`/`cert`.
- `to_tableau` redacts and query-strips any exception raised while it pulls rows.
- `remember_secret()` also redacts URL-encoded, JSON-escaped and base64 forms.
- Loopback checks use `ipaddress` (`http://127.evil.example` was treated as local).
- The detector catches UUID-style keys, Stripe, SendGrid, Mailgun, Shopify, Slack
  `xoxe`/`xoxo` tokens, dashed passwords, `pw`/`passphrase`/`cookie` names, and
  unquoted `name = value` lines in `.ini/.cfg/.yaml/.txt/.env` package members.
- TE-TLS also catches `ssl=False`, `.verify_mode = 0`, `CERT_NONE`,
  `_create_unverified_context`, and DSN dicts (`sslmode`, `TrustServerCertificate`, `Encrypt`).
- Workbooks with a DOCTYPE or ENTITY are refused (entity expansion);
  `MAX_TWB_BYTES` is 256 MB.
- `patch_workbook` writes atomically, needs `--overwrite` to replace an existing
  output, and can't write a `.twbx` from a `.twb`. `inspect --extract` writes
  0600 files without following symlinks. The keystore file backend refuses a
  symlinked parent directory and round-trips values that start with `b64:`.
- Requirement floors: `requests>=2.32.4` (CVE-2024-47081, CVE-2024-35195),
  `pyarrow>=14.0.1` (CVE-2023-47248).

Correctness
- `items_at(body, ITEMS_PATH)`: a missing path raises a readable error that
  lists the response's keys; `null` means zero rows.
- Later `next_url` / `link_header` pages no longer duplicate auth query
  parameters (e.g. `api_key`) that the link already carries.
- GraphQL scaffolds raise if `CONNECTION_PATH` is missing.
- `'1,5'` is no longer read as 15; epoch nanoseconds; exact large `Decimal` integers.
- `get_secret(name, redact=False)` for usernames / client ids.
- `Retry-After` HTTP dates; monotonic budget clock.
- TE-TABS ignores tabs inside triple-quoted strings; TE-AWAIT is reported
  (it used to surface as TE-COMPILE); TE-COLNAME `[` `]` is an error.
- `tools/gen_cli_docs.py --check` tolerates argparse formatting differences
  between Python versions.

Tests and CI
- 130 checks in-process, 156 with `--tabpy` (scaffold matrix, `.twbx` round trip,
  keystore round trip, every checker rule, every fix above).
- GitHub Actions on Python 3.11 / 3.12 / 3.13.

## v0.1.0-preview — 2026-10-27

First version.

- `runtime/tabext_runtime.py` (TABEXT RUNTIME v1): `get_secret` (env → macOS
  Keychain → keyring → secrets file, resolved on the TabPy host),
  budget-aware `http_request` with timeouts and 429/5xx + `Retry-After` retries,
  secret redaction, OAuth2 client credentials, `paginate`, `dig`,
  `input_records` (JSON dict or Arrow DataFrame), and `to_tableau` coercion
  (numpy, Decimal, pandas Timestamp/NaT, epoch s/ms, ISO offsets → UTC).
  It silences urllib3 DEBUG logging, which otherwise writes query-string keys to TabPy's log.
- `new_connector.py`: REST / GraphQL / SQL scaffolds, 7 auth schemes, 7
  pagination styles, input-table lookups, `--refresh-runtime`.
- `check_script.py`: 32 rules, compiled inside TabPy's exact wrapper, with Python 3.8
  syntax floor.
- `run_local.py`: in-process TabPy emulation or a real `/evaluate`. It validates
  JSON and Arrow serialization, declared vs observed types, and writes `schema.json`.
- `inspect_workbook.py`, `patch_workbook.py` (`replace` / `new`, .twb and
  .twbx, column typing, dropped-column sheet warnings, credential refusal).
- `set_secret.py` / `keystore.py` (service `tableau-tabext`, env prefix `TABLEAU_TABEXT_`).
- Worked example: NASA NeoWs close approaches.
- `tests/test_skill.py`: 51 checks in-process, 71 with `--tabpy`.

Known gaps: patched/new workbooks not yet opened in Tableau Desktop by the
tests; zero-row and extract-refresh behavior unconfirmed.
