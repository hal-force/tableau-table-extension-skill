#!/usr/bin/env python3
"""Static compatibility check for a Tableau Table Extension (TabPy) script.

Compiles the script exactly the way TabPy's /evaluate wraps it
(def _user_script(tabpy, _arg1...): + one space per line) and then checks
the rules that make a script work as a Table Extension - see
reference/table_extension_contract.md for the why behind every rule id.

  python3 check_script.py connector.py
  python3 check_script.py connector.py --json        # machine-readable
  python3 check_script.py connector.py --py 3.8      # oldest TabPy-host Python to support

Exit code: 0 = no errors (warnings allowed), 1 = errors, 2 = usage.
Never prints a credential value (findings show line numbers only).
"""
from __future__ import annotations

import argparse
import ast
import builtins
import importlib.util
import json
import re
import sys
import sysconfig
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tabext_common as C  # noqa: E402

HTTP_VERBS = {"get", "post", "put", "patch", "delete", "head", "options", "request"}
RUNTIME_CONFIG = ["TIME_BUDGET_S", "CONNECT_TIMEOUT_S", "READ_TIMEOUT_S", "RETRIES", "CA_BUNDLE",
                  "CLIENT_CERT", "CLIENT_KEY", "MAX_ROWS", "ON_BAD_VALUE"]
COMMON_THIRD_PARTY = {"requests", "pandas", "numpy", "urllib3", "keyring", "simplejson",
                      "pyarrow", "certifi"}


class Finding(dict):
    pass


def _is_stdlib(mod):
    names = getattr(sys, "stdlib_module_names", None)       # 3.10+
    if names is not None:
        return mod in names
    if mod in sys.builtin_module_names:
        return True
    try:
        spec = importlib.util.find_spec(mod)
    except (ImportError, ValueError):
        return False
    if spec is None or not spec.origin:
        return False
    if spec.origin in ("built-in", "frozen"):
        return True
    std = sysconfig.get_paths()["stdlib"]
    return spec.origin.startswith(std) and "site-packages" not in spec.origin


def _bound_names(fn):
    """Every name bound anywhere inside the wrapper function (all scopes)."""
    out = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                out.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            out.update(n.names)
        elif type(n).__name__ in ("MatchAs", "MatchStar") and getattr(n, "name", None):
            out.add(n.name)
        elif type(n).__name__ == "MatchMapping" and getattr(n, "rest", None):
            out.add(n.rest)
    return out


def _evaluated_annotations(fn):
    """Annotations Python evaluates when the def runs (args + returns of nested defs).
    Local variable annotations inside a function body are never evaluated."""
    for n in ast.walk(fn):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not fn:
            a = n.args
            for arg in a.posonlyargs + a.args + a.kwonlyargs + [x for x in (a.vararg, a.kwarg) if x]:
                if arg.annotation is not None:
                    yield arg.annotation
            if n.returns is not None:
                yield n.returns


def _string_continuation_lines(text):
    """Line numbers that are inside a multi-line string literal (after its first
    line): a tab there is string content (pasted SQL), not indentation."""
    import io
    import tokenize
    out = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.STRING and tok.end[0] > tok.start[0]:
                out.update(range(tok.start[0] + 1, tok.end[0] + 1))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return out


def check(text: str, min_py=(3, 8), uses_input=None):
    f = []

    def add(level, rule, msg, line=None):
        f.append(Finding(level=level, rule=rule, line=line, message=msg))

    # ---- text-level rules (before parsing) ---------------------------------
    in_string = _string_continuation_lines(text)
    for i, ln in enumerate(text.split("\n"), 1):
        if i not in in_string and "\t" in ln[: len(ln) - len(ln.lstrip())]:
            add("error", "TE-TABS", "tab used for indentation; TabPy prefixes every line with one "
                "space, so tabs become ' \\t' and break (inconsistent indentation). Use spaces.", i)
    odd = [m.start() for m in re.finditer("[\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]", text)]
    for p in odd[:5]:
        add("error", "TE-LINESEP", "unusual line-separator character; TabPy's str.splitlines() "
            "splits on it and corrupts the script. Use an escape like \\u2028.", C.line_of(text, p))
    for a, _b in C.credential_spans(text):
        add("error", "TE-SECRET", "credential-looking literal. Use get_secret('name') and store the "
            "value on the TabPy host with set_secret.py. Rotate this credential if it was ever "
            "saved in a workbook or ran through TabPy (TabPy logs every script at INFO).", C.line_of(text, a))
    for m in re.finditer(r"(?i)\bTODO\(tabext\)", text):
        add("error", "TE-TODO", "unfinished TODO(tabext) marker left by the scaffold", C.line_of(text, m.start()))

    # ---- compile in TabPy's wrapper -----------------------------------------
    wrapped = C.tabpy_wrap(text, 1)
    try:
        compile(wrapped, "<tabpy _user_script>", "exec")
    except SyntaxError as e:
        ln = (e.lineno or 1) - 1
        if "await" in (e.msg or ""):
            add("error", "TE-AWAIT", "`await` outside an async def: TabPy's wrapper is a plain function. "
                "Use synchronous calls (the runtime's http_*).", max(ln, 1))
        else:
            add("error", "TE-COMPILE", "does not compile inside TabPy's wrapper: %s" % e.msg, max(ln, 1))
        return f
    try:
        tree = ast.parse(wrapped, feature_version=min_py)
    except SyntaxError as e:
        add("error", "TE-PYVER", "uses syntax newer than Python %d.%d (%s). Raise --py if your TabPy "
            "host is newer." % (min_py + (e.msg,)), max((e.lineno or 2) - 1, 1))
        tree = ast.parse(wrapped)
    fn = tree.body[0]
    body = fn.body

    def L(node):
        return max(getattr(node, "lineno", 2) - 1, 1)

    # ---- syntax that ast.parse(feature_version=...) accepts but old Pythons reject
    src_lines = wrapped.split("\n")
    for n in ast.walk(fn):
        if min_py < (3, 9) and isinstance(n, (ast.With, ast.AsyncWith)) and \
                any(it.optional_vars is not None for it in n.items):
            seg = "\n".join(src_lines[n.lineno - 1: (getattr(n, "end_lineno", n.lineno) or n.lineno)])
            head = seg.split(":", 1)[0] if len(n.items) == 1 else seg
            if re.search(r"\bwith\s*\((?:[^()]|\([^()]*\))*\bas\b", head):
                add("error", "TE-PYVER", "parenthesized context managers `with (a as x, b as y):` need "
                    "Python 3.9+ (3.10 officially). Use nested `with` statements.", L(n))
        if min_py < (3, 9) and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for d in n.decorator_list:
                e = d.func if isinstance(d, ast.Call) else d
                while isinstance(e, ast.Attribute):
                    e = e.value
                if not isinstance(e, ast.Name):
                    add("error", "TE-PYVER", "decorator expression needs Python 3.9+ (relaxed "
                        "decorator grammar). Assign it to a name first.", L(d))
    for ann in _evaluated_annotations(fn):
        for x in ast.walk(ann):
            if min_py < (3, 9) and isinstance(x, ast.Subscript) and isinstance(x.value, ast.Name) and \
                    x.value.id in ("list", "dict", "tuple", "set", "frozenset", "type"):
                add("error", "TE-PYVER", "annotation %s[...] raises TypeError on Python 3.8 when the "
                    "def runs. Drop the annotation or use typing.%s." % (x.value.id, x.value.id.title()), L(ann))
                break
            if min_py < (3, 10) and isinstance(x, ast.BinOp) and isinstance(x.op, ast.BitOr):
                add("error", "TE-PYVER", "annotation `X | Y` raises TypeError before Python 3.10 when "
                    "the def runs. Drop it or use typing.Optional/Union.", L(ann))
                break

    # ---- top-level return ----------------------------------------------------
    def walk_shallow(nodes):
        """Walk statements of the script body without entering nested defs/classes/lambdas."""
        nested = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda,
                  ast.GeneratorExp, ast.ListComp, ast.SetComp, ast.DictComp)
        stack = [n for n in nodes if not isinstance(n, nested)]
        while stack:
            n = stack.pop()
            yield n
            for ch in ast.iter_child_nodes(n):
                if not isinstance(ch, nested):
                    stack.append(ch)

    shallow = list(walk_shallow(body))
    returns = [n for n in shallow if isinstance(n, ast.Return)]
    if not returns:
        add("error", "TE-RETURN", "no top-level `return`. A Table Extension script must end with "
            "`return to_tableau(rows, SCHEMA)` (or `return df.to_dict(orient='list')`); without it "
            "TabPy answers null and Tableau gets no table.")
    else:
        last = body[-1]
        if not isinstance(last, ast.Return):
            add("warning", "TE-RETURN-LAST", "the final statement is not a `return`; make the output "
                "path obvious and unconditional.", L(last))
        for r in returns:
            v = r.value
            ok = False
            if isinstance(v, ast.Call):
                fname = getattr(v.func, "id", None) or getattr(v.func, "attr", None)
                if fname == "to_tableau":
                    ok = True
                elif fname == "to_dict":
                    orient = [k for k in v.keywords if k.arg == "orient"]
                    pos = v.args[0] if v.args else None
                    val = orient[0].value if orient else pos
                    if isinstance(val, ast.Constant) and val.value == "list":
                        ok = True
                        add("warning", "TE-NOCOERCE", "returns df.to_dict(orient='list') directly: "
                            "pandas Timestamps/dates and numpy int64/bool_ are NOT serializable by "
                            "TabPy and mixed-type columns break TabPy's Arrow transport. Prefer "
                            "`return to_tableau(df, SCHEMA)`.", L(r))
                    else:
                        add("error", "TE-ORIENT", "to_dict() must use orient='list' (a dict of "
                            "equal-length column lists).", L(r))
                        ok = True
            if isinstance(v, (ast.Dict, ast.Name)):
                ok = True
                add("warning", "TE-NOCOERCE", "returns a raw dict/variable; make sure every value is "
                    "a list of equal length holding str/int/float/bool/None only. Prefer to_tableau().", L(r))
            if v is None or (isinstance(v, ast.Constant) and v.value is None):
                add("error", "TE-RETURN-NONE", "`return` with no value - Tableau gets no table.", L(r))
            elif not ok and not isinstance(v, (ast.Dict, ast.Name)):
                add("warning", "TE-RETURN-SHAPE", "can't verify the return shape statically; run "
                    "run_local.py to validate the output contract.", L(r))
        for n in shallow:
            if isinstance(n, (ast.Yield, ast.YieldFrom)):
                add("error", "TE-YIELD", "top-level `yield` turns TabPy's _user_script into a generator "
                    "and returns nothing to Tableau. Put generators inside a def.", L(n))
            if isinstance(n, ast.Await):
                add("error", "TE-AWAIT", "top-level await is not allowed in a TabPy script.", L(n))

    # ---- statement rules (whole tree) ---------------------------------------
    rt_text = C.extract_runtime(text)
    rt_lo = rt_hi = -1
    # only the byte-identical canonical runtime is exempt from the rules below; a
    # modified block between the markers is checked like any other code
    if rt_text is not None and C.is_canonical_runtime(rt_text):
        rt_lo = C.line_of(text, text.find(rt_text))
        rt_hi = rt_lo + rt_text.count("\n")

    def in_rt(node):
        return rt_lo <= L(node) <= rt_hi

    def dotted(e):
        parts = []
        while isinstance(e, ast.Attribute):
            parts.append(e.attr)
            e = e.value
        if isinstance(e, ast.Name):
            parts.append(e.id)
            return ".".join(reversed(parts))
        return ""

    session_names = set()
    false_names = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call):
            if dotted(n.value.func) in ("requests.Session", "requests.session", "Session"):
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        session_names.add(t.id)
        if isinstance(n, (ast.With, ast.AsyncWith)):
            for it in n.items:
                if isinstance(it.context_expr, ast.Call) and isinstance(it.optional_vars, ast.Name) and \
                        dotted(it.context_expr.func) in ("requests.Session", "requests.session", "Session"):
                    session_names.add(it.optional_vars.id)
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) and n.value.value in (False, 0, ""):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    false_names.add(t.id)
        # s.verify = False / ctx.check_hostname = False / ctx.verify_mode = ssl.CERT_NONE
        if isinstance(n, ast.Assign) and not in_rt(n):
            for t in n.targets:
                if isinstance(t, ast.Attribute) and t.attr in ("verify", "check_hostname") and \
                        isinstance(n.value, ast.Constant) and n.value.value in (False, 0, "", None):
                    add("error", "TE-TLS", ".%s = %r disables certificate checks. Set CA_BUNDLE to the CA "
                        "bundle path on the TabPy host instead." % (t.attr, n.value.value), L(n))
                if isinstance(t, ast.Attribute) and t.attr == "verify_mode" and \
                        isinstance(n.value, ast.Constant) and n.value.value in (0, False):
                    add("error", "TE-TLS", ".verify_mode = 0 (CERT_NONE) disables certificate checks.", L(n))
                if isinstance(t, ast.Subscript) and isinstance(n.value, ast.Constant) and \
                        dotted(t.value).endswith("environ"):
                    k = t.slice.value if type(t.slice).__name__ == "Index" else t.slice   # py3.8 wrapper
                    k = k.value if isinstance(k, ast.Constant) else None
                    if k in ("PYTHONHTTPSVERIFY", "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE") and \
                            n.value.value in ("0", "", 0):
                        add("error", "TE-TLS", "setting %s to %r turns certificate verification off."
                            % (k, n.value.value), L(n))
        if isinstance(n, ast.Call) and not in_rt(n) and isinstance(n.func, ast.Attribute) and \
                n.func.attr in ("setdefault", "putenv", "update"):
            pairs = []
            if n.func.attr == "update" and n.args and isinstance(n.args[0], ast.Dict):
                pairs = list(zip(n.args[0].keys, n.args[0].values))
            elif len(n.args) > 1:
                pairs = [(n.args[0], n.args[1])]
            pairs += [(ast.Constant(kw.arg), kw.value) for kw in n.keywords if kw.arg]
            for kn, vn in pairs:
                if isinstance(kn, ast.Constant) and isinstance(vn, ast.Constant) and \
                        kn.value in ("PYTHONHTTPSVERIFY", "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE") and \
                        vn.value in ("0", "", 0):
                    add("error", "TE-TLS", "setting %s to %r turns certificate verification off."
                        % (kn.value, vn.value), L(n))
        if isinstance(n, ast.Attribute) and not in_rt(n) and \
                (n.attr == "CERT_NONE" or n.attr == "_create_unverified_context"):
            add("error", "TE-TLS", "ssl.%s disables certificate checks. Use CA_BUNDLE." % n.attr, L(n))
        if isinstance(n, ast.Constant) and not in_rt(n) and n.value in ("CERT_NONE", "_create_unverified_context"):
            add("error", "TE-TLS", "%r disables certificate checks. Use CA_BUNDLE." % n.value, L(n))
        if isinstance(n, ast.Call) and not in_rt(n):
            for kw in n.keywords:
                if kw.arg == "ssl" and isinstance(kw.value, ast.Constant) and kw.value.value is False:
                    add("error", "TE-TLS", "ssl=False disables certificate checks. Use CA_BUNDLE.", L(n))
        if isinstance(n, ast.Dict) and not in_rt(n):
            for kn, vn in zip(n.keys, n.values):
                if not (isinstance(kn, ast.Constant) and isinstance(kn.value, str) and isinstance(vn, ast.Constant)):
                    continue
                k, v = kn.value.lower(), vn.value
                sv = str(v).lower()
                if k in ("verify", "ssl", "check_hostname") and v in (False, 0, ""):
                    why = "%r: %r disables certificate checks" % (kn.value, v)
                elif k == "sslmode" and sv in ("disable", "allow", "prefer", "require"):
                    why = "sslmode=%r doesn't verify the server certificate (use verify-full)" % v
                elif k == "trustservercertificate" and sv in ("yes", "true", "1"):
                    why = "TrustServerCertificate=%r skips certificate verification" % v
                elif k in ("encrypt",) and sv in ("no", "false", "0", "optional"):
                    why = "Encrypt=%r sends the database session unencrypted" % v
                else:
                    continue
                add("error", "TE-TLS", why + ". Set CA_BUNDLE / the driver's CA option instead.", L(n))
    imports = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Global):
            add("warning", "TE-GLOBAL", "`global` writes into TabPy's handler module namespace, shared "
                "by every request. Keep state local.", L(n))
        if isinstance(n, ast.If) and isinstance(n.test, ast.Compare) and \
                getattr(n.test.left, "id", "") == "__name__":
            add("error", "TE-MAIN", "`if __name__ == '__main__':` never runs inside TabPy (the script "
                "is a function body). Put the logic at top level.", L(n))
        if isinstance(n, ast.Import) and not in_rt(n):
            imports |= {a.name.split(".")[0] for a in n.names}
        if isinstance(n, ast.ImportFrom) and n.module and n.level == 0 and not in_rt(n):
            imports.add(n.module.split(".")[0])
            if n.module.split(".")[0] in ("signal",):
                add("error", "TE-SIGNAL", "signal handlers only work on the main thread; TabPy runs "
                    "scripts in a worker thread.", L(n))
        if isinstance(n, ast.Import) and any(a.name == "signal" for a in n.names):
            add("error", "TE-SIGNAL", "signal handlers only work on the main thread; TabPy runs "
                "scripts in a worker thread.", L(n))
        if isinstance(n, ast.Call):
            func = n.func
            fname = getattr(func, "id", None) or getattr(func, "attr", None)
            owner = getattr(getattr(func, "value", None), "id", None)
            if fname in ("exit", "quit") and owner in (None, "sys") or (owner == "os" and fname == "_exit"):
                add("error", "TE-EXIT", "exit()/sys.exit() inside TabPy aborts the request (or the "
                    "worker). Raise RuntimeError('readable message') instead.", L(n))
            if fname in ("input", "getpass") and owner in (None, "getpass"):
                add("error", "TE-INTERACTIVE", "interactive input is impossible inside TabPy.", L(n))
            if fname == "print" and not in_rt(n):
                add("info", "TE-PRINT", "print() goes to the TabPy console/log, not to Tableau. Never "
                    "print secrets or full payloads.", L(n))
            kw = {k.arg for k in n.keywords}
            if None in kw:
                pass        # **kwargs may carry timeout= / verify=
            elif fname == "urlopen" and "timeout" not in kw and len(n.args) < 3:
                add("error", "TE-TIMEOUT", "urlopen() without timeout= can hang until TabPy's "
                    "TABPY_EVALUATE_TIMEOUT kills the request. Use http_get() from the runtime.", L(n))
            elif fname in HTTP_VERBS and (owner == "requests" or owner in session_names):
                if "timeout" not in kw:
                    add("error", "TE-TIMEOUT", "%s.%s() without timeout= can hang until TabPy's "
                        "TABPY_EVALUATE_TIMEOUT (30 s default) kills the request. Use http_get()/"
                        "http_request() from the runtime or pass timeout=." % (owner, fname), L(n))
            for k in n.keywords:
                if k.arg == "verify" and (
                        isinstance(k.value, ast.Constant) and k.value.value in (False, 0, "") or
                        isinstance(k.value, ast.Name) and k.value.id in false_names):
                    add("error", "TE-TLS", "verify=False disables certificate checks. Set CA_BUNDLE to "
                        "the CA bundle path on the TabPy host instead.", L(n))
            if fname == "disable_warnings":
                add("warning", "TE-TLS", "urllib3.disable_warnings() usually hides verify=False.", L(n))
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    bound = _bound_names(fn) | set(dir(builtins)) | {"tabpy", "_arg1", "__name__", "__file__"}
    seen_undef = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in bound and \
                n.id not in seen_undef:
            seen_undef.add(n.id)
            add("error", "TE-NAME", "%r is used but never defined: NameError when Tableau refreshes. "
                "Define it in CONFIG or FETCH." % n.id, L(n))
    uses_secret = rt_text is not None and "get_secret(" in text.replace(rt_text, "") or \
        rt_text is None and "get_secret(" in text
    for n in ast.walk(fn):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and not in_rt(n) and \
                re.match(r"(?i)http://[^\s/]", n.value.strip()) and not C.is_loopback_url(n.value.strip()):
            add("error" if uses_secret else "warning", "TE-HTTP",
                "plain http:// URL. %s" % ("The runtime refuses to send credentials over it (and they "
                "would cross the network in clear text): use https://." if uses_secret else
                "Data crosses the network unencrypted; use https:// if the server offers it."), L(n))
    if "_arg1" in names and uses_input is not True:
        add("warning", "TE-ARG1", "references _arg1 directly. It only exists when the Table Extension "
            "has an input table; without one this raises NameError. Use input_records() (runtime) or "
            "locals().get('_arg1').")

    # ---- runtime + SCHEMA ----------------------------------------------------
    rt = C.extract_runtime(text)
    if rt is None:
        add("warning", "TE-RUNTIME", "no TABEXT runtime block: you lose get_secret(), budget-aware "
            "retries, redacted errors and to_tableau() coercion. Scaffold with new_connector.py.")
    else:
        canon = C.runtime_block()
        if rt.strip() != canon.strip():
            same_ver = C.runtime_version(rt) == C.runtime_version(canon)
            add("warning" if not same_ver else "error", "TE-RUNTIME-DRIFT",
                "runtime block differs from runtime/tabext_runtime.py (%s). Run new_connector.py "
                "--refresh-runtime FILE; put custom code outside the markers." %
                ("outdated version" if not same_ver else "edited by hand"))
        assigned = {t.id for n in body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
        assigned |= {n.target.id for n in body if isinstance(n, ast.AnnAssign) and n.value is not None
                     and isinstance(n.target, ast.Name)}
        missing = [c for c in RUNTIME_CONFIG if c not in assigned]
        if missing:
            add("error", "TE-CONFIG", "runtime needs these CONFIG constants at top level: " + ", ".join(missing))
        def top_assigns(name):
            for n in body:
                if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == name for t in n.targets):
                    yield n
                elif isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == name and n.value is not None:
                    yield n

        budget = None
        for n in top_assigns("TIME_BUDGET_S"):
            if True:
                if isinstance(n.value, ast.Constant):
                    budget = n.value.value
        if isinstance(budget, (int, float)) and budget > 28:
            add("warning", "TE-BUDGET", "TIME_BUDGET_S=%s is above TabPy's default 30 s "
                "TABPY_EVALUATE_TIMEOUT. Raise that setting on the TabPy host too, or Tableau gets "
                "'User defined script timed out'." % budget)
        if "to_tableau" in text:
            schema = None
            for n in top_assigns("SCHEMA"):
                if True:
                    try:
                        schema = ast.literal_eval(n.value)
                    except Exception:
                        add("error", "TE-SCHEMA", "SCHEMA must be a literal dict {column: type}.", L(n))
            if schema is None and "SCHEMA" in names:
                add("error", "TE-SCHEMA", "SCHEMA is used but not assigned as a top-level literal dict.")
            if isinstance(schema, dict):
                bad = {k: v for k, v in schema.items() if v not in C.SCHEMA_TYPES}
                if bad:
                    add("error", "TE-SCHEMA", "unknown SCHEMA types %s (allowed: %s)" % (
                        bad, ", ".join(sorted(C.SCHEMA_TYPES))))
                lower = {}
                for k in schema:
                    if not isinstance(k, str) or not k.strip():
                        add("error", "TE-COLNAME", "column names must be non-empty strings: %r" % (k,))
                        continue
                    if k.lower() in lower:
                        add("error", "TE-COLNAME", "columns %r and %r differ only by case; Tableau field "
                            "names are case-insensitive." % (lower[k.lower()], k))
                    lower[k.lower()] = k
                    if re.search(r"[\[\]]", k):
                        add("error", "TE-COLNAME", "column %r contains [ or ]: Tableau wraps field names "
                            "in brackets, so the workbook's field references break." % k)
                    elif len(k) > 120:
                        add("warning", "TE-COLNAME", "column %r: very long names are hard to use "
                            "in Tableau calculations." % k)

    # ---- dependencies ----------------------------------------------------------
    third = {i for i in imports if not _is_stdlib(i) and not i.startswith("_")}
    if rt_text is not None and re.search(r"\b(http_get|http_post|http_request|oauth2_client_credentials)\(",
                                         text.replace(rt_text, "")):
        third.add("requests")
    if third:
        add("info", "TE-DEPS", "install on the TabPy host's Python: " + ", ".join(sorted(third)) +
            ("  (optional: keyring, for non-macOS secret lookup)" if rt_text is not None else ""))
    return f


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("script")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--py", default="3.8", help="oldest Python on the TabPy host (default 3.8)")
    ap.add_argument("--uses-input", action="store_true",
                    help="the Table Extension has an input table (silences TE-ARG1)")
    a = ap.parse_args(argv)
    try:
        major, minor = (int(x) for x in a.py.split(".")[:2])
    except ValueError:
        ap.error("--py like 3.8")
    text = C.read_text_input(a.script)
    findings = check(text, (major, minor), True if a.uses_input else None)
    errors = [x for x in findings if x["level"] == "error"]
    if a.json:
        print(json.dumps({"ok": not errors, "findings": findings}, indent=2))
    else:
        order = {"error": 0, "warning": 1, "info": 2}
        for x in sorted(findings, key=lambda d: (order[d["level"]], d["line"] or 0)):
            loc = (" line %d" % x["line"]) if x["line"] else ""
            print("%-7s %-16s%s: %s" % (x["level"].upper(), x["rule"], loc, x["message"]))
        print("\n%s: %d error(s), %d warning(s)" % ("FAIL" if errors else "OK", len(errors),
              sum(1 for x in findings if x["level"] == "warning")))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
