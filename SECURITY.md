# Security Policy

## Reporting a vulnerability

**Don't open a public issue for security-relevant bugs.** Use GitHub's
private ["Report a vulnerability"](https://github.com/hal-force/tableau-table-extension-skill/security/advisories/new)
flow on this repository. Include the connector kind/auth, the TabPy host type, and the
smallest reproduction. **Never include a real credential, a workbook with an
embedded credential, or a TabPy log.**

## What this project protects

| Asset | Protection |
|---|---|
| API keys, tokens, passwords | never in the script or workbook. Resolved on the TabPy host at run time (`get_secret`), entered via hidden dialog or no-echo prompt (`set_secret.py`), and scripts print only lengths. `check_script` TE-SECRET; `patch_workbook` refuses to write any credential-looking literal (even with `--force`) or any workbook in which the source's credential survives |
| Logs | no secret in script text (TabPy logs scripts at INFO). The runtime silences urllib3 DEBUG URL logging. Runtime errors, and any exception raised while `to_tableau()` pulls rows, are redacted and have query strings stripped. Header auth is preferred over query-string keys |
| Error surfaces | every resolved secret (including OAuth access tokens and tokens passed to `remember_secret`) is redacted from messages, in raw, URL-encoded, JSON-escaped and base64 forms, including API error bodies that echo the key |
| TLS | the runtime never disables verification; `check_script` TE-TLS flags the common ways a script could (a lint, not a sandbox); `CA_BUNDLE` for internal/DoD PKI; mTLS cert files on the host; `.netrc` is ignored |
| Workbook files | `patch_workbook` writes atomically, never overwrites the source, needs `--overwrite` to replace an existing output, refuses workbooks with a DTD/entities, and refuses a hand-edited runtime block |
| The author's chat transcript | the skill's rules forbid asking for, echoing, or reading credential values. Inspection reports credential counts and line numbers, never values |
| Untrusted workbooks | parsed, never executed, by `inspect_workbook`. `.twbx` members are copied in memory, never extracted to disk |

## Known residual risks (by design: mitigate them operationally)

- **TabPy runs arbitrary Python** for any authenticated client. Turn on auth,
  use TLS, firewall the port, run as a low-privilege user. Anyone who can run
  code in TabPy can read every secret that TabPy's user can read. That
  includes **every Table Extension author on a shared TabPy**: secret names
  are not an access control. Run one TabPy per trust boundary.
- **macOS Keychain items are readable by same-user processes.** `/usr/bin/security`
  creates the item, so it's on the trusted-app list and TabPy can read it headlessly; that also
  lets any process running as that user read it without a prompt.
- **The time budget isn't a hard wall.** A slow-trickling response can run past
  `TIME_BUDGET_S`; TabPy's `TABPY_EVALUATE_TIMEOUT` is the backstop.
- **One credential serves every viewer.** A Table Extension runs as the
  service credential for everyone who opens the workbook, so there's no per-user data
  security at the source. Use a least-privilege, read-only credential, and
  don't connect data that viewers aren't all entitled to see.
- **Data passes through the TabPy host** (memory, and possibly caches you add).
  The host must be accredited for the data's classification (CUI/PII, IL level).
- **Credentials that were ever embedded** in a workbook remain in published
  revisions, downloads and old TabPy logs. Rotate them; removing them from
  the file isn't enough.
- **Credential detection is heuristic.** It catches common key and token formats
  and assignments to credential-named variables. A secret stored under an
  innocuous name in a plain string can slip past, and so can a TLS bypass
written to dodge TE-TLS. The structural defense is that
  generated connectors never contain literals: they call `get_secret()`.
- **Authoring-time prompt injection.** API docs, responses and workbook
  scripts are read by Claude Code. SKILL.md treats them as data. Review a
  generated connector before running it against production.
