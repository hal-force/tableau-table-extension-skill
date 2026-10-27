#!/usr/bin/env python3
"""Scaffold a Tableau Table Extension connector script (TabPy / Python).

Writes a script with: a CONFIG block, the canonical TABEXT runtime (secret
lookup on the TabPy host, budget-aware HTTP, to_tableau() coercion), and a
FETCH section pre-written for the chosen auth + pagination. Remaining
connector-specific bits are marked TODO(tabext) - check_script.py fails
until they are resolved.

  python3 new_connector.py --name nasa_neows --kind rest \\
      --base-url https://api.nasa.gov/neo/rest/v1 --auth api_key_header --auth-header X-Api-Key \\
      --pagination date_window --out connectors/nasa_neows.py

  python3 new_connector.py --name acme --kind graphql --base-url https://api.acme.com/graphql \\
      --auth bearer --pagination cursor --out connectors/acme.py

  python3 new_connector.py --name hr --kind sql --sql-dialect postgresql+psycopg2 --out hr.py

  python3 new_connector.py --refresh-runtime connectors/nasa_neows.py   # re-inline latest runtime

kinds:       rest | graphql | sql
auth:        none | api_key_header | api_key_query | bearer | basic | oauth2_cc | mtls
pagination:  none | page | offset | cursor | next_url | link_header | date_window
"""
from __future__ import annotations

import argparse
import re
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tabext_common as C  # noqa: E402

AUTHS = ["none", "api_key_header", "api_key_query", "bearer", "basic", "oauth2_cc", "mtls"]
PAGES = ["none", "page", "offset", "cursor", "next_url", "link_header", "date_window"]


def q(s):
    return repr(s) if s is not None else "None"


# ------------------------------------------------------------------ auth
def auth_block(a):
    n = a.secret_prefix
    if a.auth == "none":
        return "def auth():\n    \"\"\"No authentication.\"\"\"\n    return {}, {}, None\n", []
    if a.auth == "api_key_header":
        return (textwrap.dedent('''\
            def auth():
                """API key in a request header. Returns (headers, params, requests_auth)."""
                return {%s: get_secret(%s)}, {}, None
            ''') % (q(a.auth_header or "X-API-Key"), q(n + ".api_key")), [n + ".api_key"])
    if a.auth == "api_key_query":
        return (textwrap.dedent('''\
            def auth():
                """API key as a query parameter. Prefer a header if the API accepts one: query
                strings end up in proxy/server access logs. (Runtime errors strip them and the
                runtime silences TabPy's urllib3 debug log.)"""
                return {}, {%s: get_secret(%s)}, None
            ''') % (q(a.auth_param or "api_key"), q(n + ".api_key")), [n + ".api_key"])
    if a.auth == "bearer":
        return (textwrap.dedent('''\
            def auth():
                """Static bearer / personal access token."""
                return {"Authorization": "Bearer " + get_secret(%s)}, {}, None
            ''') % q(n + ".token"), [n + ".token"])
    if a.auth == "basic":
        return (textwrap.dedent('''\
            def auth():
                """HTTP Basic with a (preferably read-only service) account."""
                return {}, {}, (get_secret(%s, redact=False), get_secret(%s))
            ''') % (q(n + ".username"), q(n + ".password")), [n + ".username", n + ".password"])
    if a.auth == "oauth2_cc":
        return (textwrap.dedent('''\
            def auth():
                """OAuth 2.0 client credentials (machine-to-machine). Token cached per run."""
                tok = oauth2_client_credentials(TOKEN_URL, get_secret(%s, redact=False), get_secret(%s),
                                                scope=OAUTH_SCOPE)
                return {"Authorization": "Bearer " + tok}, {}, None
            ''') % (q(n + ".client_id"), q(n + ".client_secret")), [n + ".client_id", n + ".client_secret"])
    if a.auth == "mtls":
        return (textwrap.dedent('''\
            def auth():
                """Mutual TLS: CLIENT_CERT / CLIENT_KEY are files ON THE TABPY HOST (a software
                certificate issued to the service - a CAC smart card can't be used headless)."""
                if not CLIENT_CERT:
                    raise RuntimeError("tabext: set CLIENT_CERT (and CLIENT_KEY) for mutual TLS")
                return {}, {}, None
            '''), [])
    raise SystemExit("unknown auth " + a.auth)


# ----------------------------------------------------------- REST pagination
def rest_fetch(a):
    p = a.pagination
    head = textwrap.dedent('''\
        def request_page(path, params=None, expect="json"):
            h, qp, basic = auth()
            h = dict(h, Accept="application/json")
            return http_get(BASE_URL.rstrip("/") + path, params=dict(qp, **(params or {})),
                            headers=h, auth=basic, expect=expect)


        def items_of(body):
            return items_at(body, ITEMS_PATH)   # raises if the path is missing: no silent empty table


        def later_page_params(url, qp):
            """Auth query params for a next/Link URL, unless the URL already carries them."""
            have = _urlsplit(url).query
            return dict((k, v) for k, v in qp.items() if not _re.search(r"(?:^|&)%s=" % _re.escape(k), have))

        ''')
    if p == "none":
        body = '''\
            def fetch():
                return items_of(request_page(ENDPOINT, params=QUERY))
            '''
    elif p == "page":
        body = '''\
            def get_page(state):
                page, first = state
                body = request_page(ENDPOINT, params=dict(QUERY, page=page, per_page=PAGE_SIZE))  # TODO(tabext): param names
                items = items_of(body)
                first = len(items) if first is None else first   # the API may cap page size below PAGE_SIZE
                # stop on an empty or short page (better: use the API's total / has_more if it has one)
                return items, ((page + 1, first) if items and len(items) >= min(first, PAGE_SIZE) else None)


            def fetch():
                return paginate(get_page, start=(1, None))
            '''
    elif p == "offset":
        body = '''\
            def get_page(state):
                offset, first = state
                body = request_page(ENDPOINT, params=dict(QUERY, offset=offset, limit=PAGE_SIZE))  # TODO(tabext): param names
                items = items_of(body)
                first = len(items) if first is None else first   # the API may cap page size below PAGE_SIZE
                # stop on an empty or short page (better: use the API's total / has_more if it has one)
                return items, ((offset + len(items), first) if items and len(items) >= min(first, PAGE_SIZE) else None)


            def fetch():
                return paginate(get_page, start=(0, None))
            '''
    elif p == "cursor":
        body = '''\
            def get_page(cursor):
                params = dict(QUERY, limit=PAGE_SIZE)  # TODO(tabext): page-size param name
                if cursor:
                    params["cursor"] = cursor          # TODO(tabext): cursor param name
                body = request_page(ENDPOINT, params=params)
                items = items_of(body)
                nxt = dig(body, NEXT_CURSOR_PATH)
                # done when the cursor is missing, empty or unchanged, or the page is empty
                return items, (nxt if nxt and nxt != cursor and items else None)


            def fetch():
                return paginate(get_page, start="")
            '''
    elif p == "next_url":
        body = '''\
            def get_page(url):
                if url is True:
                    url = BASE_URL.rstrip("/") + ENDPOINT
                    body = request_page(ENDPOINT, params=dict(QUERY, limit=PAGE_SIZE))
                else:
                    h, qp, basic = auth()  # the next URL usually carries the query already
                    body = http_get(url, params=later_page_params(url, qp),
                                    headers=dict(h, Accept="application/json"), auth=basic)
                items = items_of(body)
                nxt = dig(body, NEXT_URL_PATH)
                if not nxt or not items:
                    return items, None
                nxt = _urljoin(url, str(nxt))     # relative next links are common
                if not same_origin(nxt, BASE_URL):
                    raise RuntimeError("tabext: next-page URL is on another host (%s); refusing to send "
                                       "credentials there" % _where(nxt))
                return items, (nxt if nxt != url else None)


            def fetch():
                return paginate(get_page, start=True)
            '''
    elif p == "link_header":
        body = '''\
            def get_page(url):
                if url is True:
                    url = BASE_URL.rstrip("/") + ENDPOINT
                    r = request_page(ENDPOINT, params=dict(QUERY, per_page=PAGE_SIZE), expect="response")
                else:
                    h, qp, basic = auth()  # Link URLs carry the query already
                    r = http_get(url, params=later_page_params(url, qp),
                                 headers=dict(h, Accept="application/json"), auth=basic, expect="response")
                try:
                    body = r.json()
                except ValueError:
                    raise RuntimeError("tabext: %s returned non-JSON (HTTP %s)" % (_where(url), r.status_code)) from None
                items = items_of(body)
                nxt = r.links.get("next", {}).get("url")
                if not nxt or not items:
                    return items, None
                nxt = _urljoin(url, nxt)
                if not same_origin(nxt, BASE_URL):
                    raise RuntimeError("tabext: Link next URL is on another host (%s); refusing to send "
                                       "credentials there" % _where(nxt))
                return items, (nxt if nxt != url else None)


            def fetch():
                return paginate(get_page, start=True)
            '''
    elif p == "date_window":
        body = '''\
            def get_page(window_start):
                # windows cover [START_DATE, END_DATE); the API's end_date is inclusive
                window_end = min(window_start + _dt.timedelta(days=WINDOW_DAYS), END_DATE) - _dt.timedelta(days=1)
                body = request_page(ENDPOINT, params=dict(QUERY, start_date=window_start.isoformat(),
                                                          end_date=window_end.isoformat()))  # TODO(tabext): param names
                nxt = window_end + _dt.timedelta(days=1)
                return items_of(body), (nxt if nxt < END_DATE else None)


            def fetch():
                if WINDOW_DAYS < 1 or END_DATE <= START_DATE:
                    raise RuntimeError("tabext: need WINDOW_DAYS >= 1 and END_DATE after START_DATE")
                return paginate(get_page, start=START_DATE)
            '''
    else:
        raise SystemExit("unknown pagination " + p)
    return head + textwrap.dedent(body)


def rest_config(a):
    lines = [
        "BASE_URL = %s" % q(a.base_url or "https://api.example.com/v1"),
        "ENDPOINT = %s  # TODO(tabext): path of the list endpoint" % q(a.endpoint or "/items"),
        "QUERY = {}  # fixed query parameters (filters). Tableau parameters can't reach this script.",
        "ITEMS_PATH = %s  # TODO(tabext): dotted path to the record list in the JSON body ('' = body is the list)" % q(
            "data" if a.items_path is None else a.items_path),
    ]
    if a.pagination in ("page", "offset", "cursor", "next_url", "link_header"):
        lines.append("PAGE_SIZE = %d" % a.page_size)
    if a.pagination == "cursor":
        lines.append("NEXT_CURSOR_PATH = 'meta.next_cursor'  # TODO(tabext): where the next cursor lives")
    if a.pagination == "next_url":
        lines.append("NEXT_URL_PATH = 'next'  # TODO(tabext): where the next-page URL lives")
    if a.pagination == "date_window":
        lines += [
            "import datetime as _dtc",
            "START_DATE = _dtc.date.today()  # TODO(tabext): window start (relative dates keep refreshes current)",
            "END_DATE = START_DATE + _dtc.timedelta(days=7)  # exclusive: [START_DATE, END_DATE)",
            "WINDOW_DAYS = 7  # the API's maximum range per request",
        ]
    return "\n".join(lines + oauth_config(a))


def oauth_config(a):
    if a.auth != "oauth2_cc":
        return []
    return ["TOKEN_URL = %s  # TODO(tabext)" % q(a.token_url or "https://auth.example.com/oauth2/token"),
            "OAUTH_SCOPE = None"]


# ----------------------------------------------------------------- GraphQL
def graphql_config(a):
    return textwrap.dedent('''\
        GRAPHQL_URL = %s
        PAGE_SIZE = %d
        QUERY = """
        query Items($first: Int!, $after: String) {
          items(first: $first, after: $after) {   # TODO(tabext): real query
            nodes { id name updatedAt }
            pageInfo { hasNextPage endCursor }
          }
        }
        """
        CONNECTION_PATH = "data.items"  # TODO(tabext): path to the connection object''') % (
        q(a.base_url or "https://api.example.com/graphql"), a.page_size) + \
        "".join("\n" + x for x in oauth_config(a))


def graphql_fetch(a):
    return textwrap.dedent('''\
        def get_page(after):
            h, qp, basic = auth()
            body = http_post(GRAPHQL_URL, json_body={"query": QUERY, "variables": {"first": PAGE_SIZE, "after": after}},
                             headers=dict(h, Accept="application/json"), params=qp, auth=basic)
            if not isinstance(body, dict):
                raise RuntimeError("tabext: GraphQL response is not a JSON object")
            if body.get("errors"):
                raise RuntimeError("tabext: GraphQL errors: " + redact(str(body["errors"]))[:300])
            conn = dig(body, CONNECTION_PATH)
            if not isinstance(conn, dict):
                raise RuntimeError("tabext: CONNECTION_PATH %r is missing or not an object in the response"
                                   % CONNECTION_PATH)
            info = conn.get("pageInfo") or {}
            return conn.get("nodes") or [], (info.get("endCursor") if info.get("hasNextPage") else None)


        def fetch():
            return paginate(lambda s: get_page(None if s is True else s), start=True)
        ''')


# --------------------------------------------------------------------- SQL
def sql_config(a):
    return textwrap.dedent('''\
        # Prefer a native Tableau connector when one exists. Use this when the database
        # has no native/allowed connector (e.g. no ODBC/JDBC on Tableau Cloud) but
        # the TabPy host can reach it with a Python driver.
        DIALECT = %s  # SQLAlchemy dialect+driver; install sqlalchemy + driver on the TabPy host
        DB_HOST = %s  # TODO(tabext)
        DB_PORT = None
        DB_NAME = "analytics"  # TODO(tabext)
        DB_QUERY_ARGS = %s  # TLS: keep certificate verification on
        DB_CONNECT_ARGS = %s  # driver connect timeout (seconds)
        FETCH_BATCH = 5000
        SQL = """
        SELECT id, name, updated_at FROM schema.table  -- TODO(tabext): read-only query, explicit columns
        WHERE updated_at >= :since
        """
        SQL_PARAMS = {"since": "2026-01-01"}  # bound parameters, never string formatting''') % (
        q(a.sql_dialect or "postgresql+psycopg2"), q(a.base_url or "db.example.internal"),
        *sql_tls_defaults(a.sql_dialect or "postgresql+psycopg2"))


def sql_tls_defaults(dialect):
    """(DB_QUERY_ARGS, DB_CONNECT_ARGS) source text with TLS verification on, per dialect."""
    d = dialect.split("+")[0]
    if d == "postgresql":
        return '{"sslmode": "verify-full"}', '{"connect_timeout": 10}'
    if d == "mssql":
        return ('{"driver": "ODBC Driver 18 for SQL Server", "Encrypt": "yes", "TrustServerCertificate": "no"}',
                '{"timeout": 10}')
    if d == "mysql":
        return '{}', '{"connect_timeout": 10, "ssl": {"check_hostname": True}}  # TODO(tabext): ssl ca path'
    return '{}  # TODO(tabext): turn on TLS with certificate verification for this driver', '{}'



def sql_fetch(a):
    n = a.secret_prefix
    return textwrap.dedent('''\
        def fetch():
            import sqlalchemy as sa
            url = sa.engine.URL.create(DIALECT, username=get_secret(%s, redact=False), password=get_secret(%s),
                                       host=DB_HOST, port=DB_PORT, database=DB_NAME, query=DB_QUERY_ARGS)
            engine = sa.create_engine(url, pool_pre_ping=True, connect_args=DB_CONNECT_ARGS)
            try:
                with engine.connect() as conn:
                    res = conn.execution_options(stream_results=True).execute(sa.text(SQL), SQL_PARAMS)
                    keys = list(res.keys())
                    while True:
                        check_budget("the SQL query")
                        batch = res.fetchmany(FETCH_BATCH)
                        if not batch:
                            break
                        for r in batch:
                            yield dict(zip(keys, r))
            except sa.exc.SQLAlchemyError as e:
                msg = (str(e).splitlines() or [type(e).__name__])[0]
                raise RuntimeError("tabext: database error: " + redact(msg)[:300]) from None
            finally:
                engine.dispose()
        ''') % (q(n + ".username"), q(n + ".password")), [n + ".username", n + ".password"]


# ------------------------------------------------------------------ build
def build(a):
    secrets = []
    if a.kind == "sql":
        cfg = sql_config(a)
        fetch, secrets = sql_fetch(a)
        auth_code = ""
        deps = "sqlalchemy + the %s driver" % (a.sql_dialect or "postgresql+psycopg2").split("+")[-1]
    else:
        auth_code, secrets = auth_block(a)
        cfg = rest_config(a) if a.kind == "rest" else graphql_config(a)
        fetch = rest_fetch(a) if a.kind == "rest" else graphql_fetch(a)
        deps = "requests"
    if a.input_key:
        loop = textwrap.dedent('''\
            INPUT_KEY = %s  # input-table column that drives one lookup per row


            def rows():
                """One lookup per row of the Table Extension's input table (column INPUT_KEY)."""
                seen = set()
                for inp in input_records():
                    key = inp.get(INPUT_KEY)
                    if key is None or key != key or key == "" or key in seen:  # None / NaN / blank
                        continue
                    seen.add(key)
                    check_budget("per-input-row lookups (%%d done)" %% len(seen))
                    for rec in fetch_for(key):  # TODO(tabext): write fetch_for(key)
                        yield to_row(rec, inp)
            ''') % q(a.input_key)
        to_row = textwrap.dedent('''\
            def to_row(rec, inp=None):
                """Flatten one record. Keys must match SCHEMA."""
                return {
                    "id": dig(rec, "id"),  # TODO(tabext): map every SCHEMA column
                }
            ''')
    else:
        loop = "def rows():\n    for rec in fetch():\n        yield to_row(rec)\n"
        to_row = textwrap.dedent('''\
            def to_row(rec):
                """Flatten one record. Keys must match SCHEMA; use dig() for nested JSON."""
                return {
                    "id": dig(rec, "id"),  # TODO(tabext): map every SCHEMA column
                }
            ''')
    secret_note = ", ".join(secrets) if secrets else "none"
    header = textwrap.dedent('''\
        # %(title)s -> Tableau Table Extension (TabPy / Python)
        # Generated by the tableau-table-extension skill. Paste into Tableau:
        #   Data Source page > New Table Extension > Script > Apply > Update Now
        # Runs on every workbook open and data-source refresh (like a live connection).
        # Secrets (resolved ON THE TABPY HOST, never stored in the workbook): %(secrets)s
        #   store with: python3 set_secret.py <name>   on the TabPy host
        # TabPy host Python needs: %(deps)s
        # Grain: one row per <TODO(tabext): what one row means>
        ''') % {"title": a.title or a.name, "secrets": secret_note, "deps": deps}
    config = textwrap.dedent('''\
        # ------------------------------------------------------------------ CONFIG
        %s

        TIME_BUDGET_S = %s      # stay under TabPy's TABPY_EVALUATE_TIMEOUT (default 30 s)
        CONNECT_TIMEOUT_S = 10
        READ_TIMEOUT_S = 20
        RETRIES = 2
        CA_BUNDLE = None        # path ON THE TABPY HOST to a PEM bundle for corporate/DoD CAs
        CLIENT_CERT = None      # mTLS client cert path on the TabPy host
        CLIENT_KEY = None
        MAX_ROWS = %d
        ON_BAD_VALUE = "null"   # "null" = unreadable values become null (logged); "error" = fail the refresh

        SCHEMA = {  # output column -> string | integer | real | boolean | date | datetime
            "id": "string",  # TODO(tabext): one entry per output column, in display order
        }
        ''') % (cfg, a.time_budget, a.max_rows)
    return (header + "\n" + config + "\n" + C.runtime_block().rstrip() + "\n\n"
            "# ------------------------------------------------------------------- FETCH\n" +
            auth_code + ("\n\n" if auth_code else "") + fetch + "\n\n" + to_row + "\n\n" + loop +
            "\n\nreturn to_tableau(rows(), SCHEMA)\n")


def refresh_runtime(path):
    p = Path(path).expanduser()
    text = p.read_text()
    old = C.extract_runtime(text)
    if old is None:
        raise SystemExit("%s has no TABEXT runtime markers" % p)
    new = C.runtime_block()
    if old.strip() == new.strip():
        print("runtime already current (%s)" % C.runtime_version(new))
        return 0
    p.write_text(text.replace(old, new.rstrip("\n") + "\n"))
    print("runtime refreshed %s -> %s in %s" % (C.runtime_version(old), C.runtime_version(new), p))
    return 0


NAME_RX = re.compile(r"^[A-Za-z0-9._-]{1,40}$")
# an input-table column name: what Tableau allows in practice, minus quotes,
# backslashes and brackets (it lands in generated Python and in field references)
INPUT_KEY_RX = re.compile(r"^[A-Za-z0-9 _.$@()#%&+:/-]{1,128}$")
_CTRL = re.compile("[\\x00-\\x1f\\x7f\\x85\\u2028\\u2029]")


def validate(ap, a):
    """Every value lands in generated Python source. repr() quotes literals, but
    comments and URLs need tighter shapes, so reject anything odd."""
    if not NAME_RX.match(a.name):
        ap.error("--name: 1-40 of letters, digits . _ -")
    a.secret_prefix = a.secret_prefix or a.name
    if not NAME_RX.match(a.secret_prefix):
        ap.error("--secret-prefix: 1-40 of letters, digits . _ -")
    for opt in ("title", "base_url", "endpoint", "items_path", "auth_header", "auth_param",
                "token_url", "sql_dialect", "input_key"):
        v = getattr(a, opt)
        if v is not None and (_CTRL.search(v) or len(v) > 300):
            ap.error("--%s: no control characters or line breaks, at most 300 chars" % opt.replace("_", "-"))
    if a.input_key is not None and not INPUT_KEY_RX.match(a.input_key):
        ap.error("--input-key: a column name of 1-128 letters, digits, spaces and _ . $ @ ( ) # % & + : / -")
    if a.kind != "sql" and a.base_url:
        if not re.match(r"^https?://[^\s/?#@]+(?:/[^\s?#]*)?$", a.base_url):
            ap.error("--base-url: an http(s) URL without query, fragment or user:pass@")
        if a.base_url.lower().startswith("http://") and not C.is_loopback_url(a.base_url):
            print("warning: --base-url is plain http://. The runtime refuses to send credentials "
                  "over it; use https:// for any authenticated API.", file=sys.stderr)
    if a.kind == "sql" and a.base_url and not re.match(r"^[A-Za-z0-9.-]+$", a.base_url):
        ap.error("--base-url (sql): a bare host name")
    if a.token_url and not re.match(r"^https://[^\s/?#@]+(?:/[^\s?#]*)?$", a.token_url):
        ap.error("--token-url: an https URL")
    if a.endpoint and not re.match(r"^/[^\s#]*$", a.endpoint):
        ap.error("--endpoint: a path starting with /")
    if a.items_path is not None and not re.match(r"^[A-Za-z0-9_.$@-]*$", a.items_path):
        ap.error("--items-path: dotted path (letters, digits _ . $ @ -)")
    if a.auth_header and not re.match(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,80}$", a.auth_header):
        ap.error("--auth-header: an HTTP header name")
    if a.auth_param and not re.match(r"^[A-Za-z0-9._~-]{1,80}$", a.auth_param):
        ap.error("--auth-param: a query parameter name")
    if a.sql_dialect and not re.match(r"^[a-z0-9_]+(\+[a-z0-9_]+)?$", a.sql_dialect):
        ap.error("--sql-dialect: dialect[+driver], e.g. postgresql+psycopg2")
    if not 1 <= a.page_size <= 100000:
        ap.error("--page-size: 1..100000")
    if not 1 <= a.time_budget <= 3600:
        ap.error("--time-budget: 1..3600 seconds (and below TabPy's TABPY_EVALUATE_TIMEOUT)")
    if a.max_rows < 1:
        ap.error("--max-rows: at least 1")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh-runtime", metavar="SCRIPT")
    ap.add_argument("--name", help="short id, also the secret-name prefix (e.g. nasa -> nasa.api_key)")
    ap.add_argument("--title")
    ap.add_argument("--kind", choices=["rest", "graphql", "sql"], default="rest")
    ap.add_argument("--base-url", help="REST/GraphQL base URL, or DB host for --kind sql")
    ap.add_argument("--endpoint")
    ap.add_argument("--items-path")
    ap.add_argument("--auth", choices=AUTHS, default="none")
    ap.add_argument("--auth-header", help="header name for api_key_header (default X-API-Key)")
    ap.add_argument("--auth-param", help="query param for api_key_query (default api_key)")
    ap.add_argument("--token-url")
    ap.add_argument("--secret-prefix", help="defaults to --name")
    ap.add_argument("--pagination", choices=PAGES, default="none")
    ap.add_argument("--page-size", type=int, default=100)
    ap.add_argument("--sql-dialect")
    ap.add_argument("--input-key", help="input-table column that drives one lookup per row")
    ap.add_argument("--time-budget", type=int, default=25)
    ap.add_argument("--max-rows", type=int, default=500000)
    ap.add_argument("--out")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    if a.refresh_runtime:
        return refresh_runtime(a.refresh_runtime)
    if not a.name or not a.out:
        ap.error("--name and --out are required")
    validate(ap, a)
    out = Path(a.out).expanduser()
    if out.exists() and not a.force:
        raise SystemExit("%s exists (use --force)" % out)
    out.parent.mkdir(parents=True, exist_ok=True)
    text = build(a)
    out.write_text(text)
    todos = text.count("TODO(tabext)")
    print("wrote %s  (%d TODO(tabext) markers to resolve; then run check_script.py and run_local.py)" % (out, todos))
    return 0


if __name__ == "__main__":
    sys.exit(main())
