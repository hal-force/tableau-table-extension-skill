"""Shared helpers for the tableau-table-extension skill.

* exact emulation of how TabPy's /evaluate wraps a Table Extension script
* credential detection that never returns a secret value
* .twb / .twbx read + write that keeps Tableau's prologue
"""
from __future__ import annotations

import html
import io
import ipaddress
import os
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
RUNTIME_PATH = HERE.parent / "runtime" / "tabext_runtime.py"
RUNTIME_BEGIN = "# ==== BEGIN TABEXT RUNTIME"
RUNTIME_END = "# ==== END TABEXT RUNTIME"
PLACEHOLDERS = {"PASTE_YOUR_API_KEY_HERE", "DEMO_KEY", "YOUR_API_KEY", "CHANGEME"}

SCHEMA_TYPES = {"string", "integer", "real", "boolean", "date", "datetime"}

# --------------------------------------------------------------- TabPy wrap

def tabpy_wrap(script: str, n_args: int = 0) -> str:
    """Byte-for-byte what tabpy_server/handlers/evaluation_plane_handler.py does:

        function_to_evaluate = f"def _user_script(tabpy{arguments_str}):\\n"
        for u in user_code.splitlines():
            function_to_evaluate += " " + u + "\\n"
    """
    args = "".join(", _arg%d" % (i + 1) for i in range(n_args))
    out = "def _user_script(tabpy%s):\n" % args
    for u in script.splitlines():
        out += " " + u + "\n"
    return out


# ------------------------------------------------------- credential detection
# Two layers. Regexes work on any text (incl. workbook XML). For Python source
# an AST pass also finds string literals in credential positions whatever the
# quoting: credential-named variables / attributes / dict keys / kwargs,
# os.environ[...] = "...", auth=(user, "..."), HTTPBasicAuth(u, "..."),
# ("X-Api-Key", "...") header pairs, "Bearer " + "...".
_CRED_NAME = (r"api[_-]?key|apikey|access[_-]?key|access[_-]?token|auth[_-]?token|bearer[_-]?token|"
              r"refresh[_-]?token|id[_-]?token|token|secret[_-]?access[_-]?key|secret[_-]?key|"
              r"client[_-]?secret|pat[_-]?secret|private[_-]?key|subscription[_-]?key|app[_-]?key|"
              r"password|passwd|passphrase|pwd|pass|pw|secret|credentials?|cookie|session[_-]?id|sessionid")
_Q = "(?P<q>" + '"""' + "|'''|\"|')"
CRED_RX = [
    # name = "value" / "name": "value" (any quote style, value w/o the quote char)
    re.compile(r"(?i)(?:" + _CRED_NAME + r")[\"']*\s*[:=]\s*[bBrRuU]{0,2}" + _Q +
               r"(?P<v>(?:(?!(?P=q))[^\n]){6,}?)(?P=q)"),
    # query-string keys
    re.compile(r"(?i)[?&;](?:api[_-]?key|apikey|access[_-]?token|token|key|sig|signature|appid|app[_-]?key|"
               r"subscription-key|code|pwd|password)=(?P<v>[A-Za-z0-9._~+/%=-]{8,})"),
    # ODBC / JDBC connection strings: PWD=...; Password=...;
    re.compile(r"(?i)(?<=[;'\"])\s*(?:pwd|password)\s*=\s*(?P<v>[^;'\"\s{}<%][^;'\"\n]{3,}?)\s*(?=;|['\"])"),
    re.compile(r"(?i)\b(?:bearer|basic)\s+(?P<v>[A-Za-z0-9._~+/=-]{12,})"),
    # user:pass@host (password may contain '/' or ':')
    re.compile(r"(?i)://[^/\s:@'\"]+:(?P<v>[^\s'\"{}@]{3,})@"),
    re.compile(r"(?P<v>\bsk-[A-Za-z0-9_-]{20,})"),
    re.compile(r"(?P<v>\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,})"),       # Stripe
    re.compile(r"(?P<v>\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,})"),       # SendGrid
    re.compile(r"(?P<v>\bkey-[0-9a-f]{32})\b"),                                # Mailgun
    re.compile(r"(?P<v>\bshp(?:at|ss|ca|pa)_[0-9a-fA-F]{32})"),                 # Shopify
    re.compile(r"(?P<v>\bxox[eo]-[A-Za-z0-9-]{10,})"),
    re.compile(r"(?P<v>\b(?:AKIA|ASIA)[0-9A-Z]{16})\b"),
    re.compile(r"(?P<v>\bAIza[0-9A-Za-z_-]{35})"),
    re.compile(r"(?P<v>\bgh[pousr]_[A-Za-z0-9]{36})"),
    re.compile(r"(?P<v>\bgithub_pat_[A-Za-z0-9_]{40,})"),
    re.compile(r"(?P<v>\bglpat-[A-Za-z0-9_-]{20,})"),
    re.compile(r"(?P<v>\bxox[abpr]-[A-Za-z0-9-]{10,})"),
    re.compile(r"(?P<v>\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"),   # JWT
    re.compile(r"(?P<v>-----BEGIN [A-Z ]*PRIVATE KEY-----)"),
]
_N_CONTEXTUAL = 4      # CRED_RX[0:4] match by context, so the value must also look secret
# unquoted name=value / name: value in config-style files inside a .twbx (.ini/.cfg/.yaml/.txt/.env)
CONFIG_CRED_RX = re.compile(r"(?im)^\s*(?:export\s+)?[\w.-]*(?:" + _CRED_NAME + r")[\w.-]*\s*[:=]\s*"
                            r"(?![\"'])(?P<v>[^\s#;'\"]{6,})")
_NOT_SECRET = re.compile(r"^(?:\{.*\}|get_secret\(.*|<redacted>|<REDACTED>|%s|\$\{.*\}|\*+)$", re.S)
_CRED_NAME_RX = re.compile(r"(?i)(?:" + _CRED_NAME + r")")
# names whose *value* is a URL, a name or a setting, not a credential
_NOT_CRED_NAME = re.compile(
    r"(?i)(?:^|_)(?:url|uri|urls|path|paths|header|param|name|names|file|env|type|prefix|scheme|field|"
    r"col|column|endpoint|scope|scopes|style|mode|kind|label|help|doc|msg|message|len|length|"
    r"count|ttl|expires|expiry|expires_in|location|service)$")
# a dotted/dashed identifier ("acme.token", "X-Api-Key", "client_credentials") is a name,
# not a value - but only without digits: "f47ac10b-58cc-...", "Summer-2024-Pass" are keys.
_NOT_CRED_VALUE = re.compile(
    r"(?i)^(?:https?://\S*|bearer|basic|token|none|null|true|false|client_credentials|password|"
    r"authorization_code|refresh_token|[A-Za-z]+(?:[-_.][A-Za-z]+)+)$")


_CAMEL = re.compile(r"^[a-z]+(?:[A-Z][a-z0-9]*)+$|^[A-Z][a-z]+(?:[A-Z][a-z0-9]*)*$")


def _looks_secret(v, strict=True):
    """Is a value in a credential *position* plausibly the credential itself?
    strict (name-based context): >= 8 chars, no whitespace, not an identifier,
    dotted path, header name, URL or type word; pure letters only if long and
    not camelCase. relaxed (auth tuple, env var, Authorization header,
    connection-string PWD): >= 4 chars, not a URL / placeholder."""
    v = v.strip()
    if not v or v in PLACEHOLDERS or _NOT_SECRET.match(v) or v.lower().startswith(("http://", "https://")):
        return False
    if not strict:
        return len(v) >= 4 and v.lower() not in ("none", "null", "true", "false", "bearer", "basic", "token")
    if len(v) < 8 or re.search(r"\s", v) or _NOT_CRED_VALUE.match(v):
        return False
    if v.isalpha():
        return len(v) >= 16 and not _CAMEL.match(v)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v) and not re.search(r"\d", v):
        return False                    # snake_case identifier
    return True


def _ast_cred_spans(text):
    """Spans of string literals in credential positions in Python source.
    Returns [] if the text isn't Python."""
    import ast
    try:
        tree, shift = ast.parse(tabpy_wrap(text, 1)), 1     # wrapped: +1 line, +1 col
    except (SyntaxError, ValueError):
        try:
            tree, shift = ast.parse(text), 0
        except (SyntaxError, ValueError):
            return []
    lines = text.splitlines(True)
    starts = [0]
    for ln in lines:
        starts.append(starts[-1] + len(ln))

    def off(lineno, col):
        li = lineno - 1 - shift
        if li < 0 or li >= len(lines):
            return None
        bline = ((" " * shift) + lines[li]).encode("utf-8")
        return starts[li] + max(0, len(bline[:col].decode("utf-8", "replace")) - shift)

    spans = []

    def consts(node):
        """String constants in an expression, not descending into calls
        (get_secret("name") arguments are names, not values)."""
        stack = [node]
        while stack:
            c = stack.pop()
            if isinstance(c, ast.Call):
                continue
            yield c
            stack.extend(ast.iter_child_nodes(c))

    def flag(node, strict=True):
        for c in consts(node):
            if isinstance(c, ast.Constant) and isinstance(c.value, (str, bytes)):
                v = c.value.decode("utf-8", "replace") if isinstance(c.value, bytes) else c.value
                if not strict and re.match(r"(?i)\s*(bearer|basic|token)\s+\S", v):
                    v = v.split(None, 1)[1]     # "Bearer <x>": judge <x>
                if not _looks_secret(v, strict):
                    continue
                if getattr(c, "end_lineno", None) is None:
                    continue
                a, b = off(c.lineno, c.col_offset), off(c.end_lineno, c.end_col_offset)
                if a is not None and b is not None and b > a:
                    spans.append((a, b))

    def is_cred_name(n):
        return bool(n) and bool(_CRED_NAME_RX.search(n)) and not _NOT_CRED_NAME.search(n)

    def target_name(t):
        if isinstance(t, ast.Name):
            return t.id
        if isinstance(t, ast.Attribute):
            return t.attr
        if isinstance(t, ast.Subscript):
            s = t.slice
            if type(s).__name__ == "Index":                 # py3.8 ast.Index wrapper
                s = s.value
            if isinstance(s, ast.Constant) and isinstance(s.value, str):
                if isinstance(t.value, ast.Attribute) and t.value.attr == "environ":
                    return "secret"                         # os.environ["X"] = "literal"
                return s.value
        return None

    schema_dicts = set()
    for n in ast.walk(tree):            # SCHEMA = {...}: column names + type words, never secrets
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Dict) and \
                any(target_name(t) in ("SCHEMA", "COLUMNS") for t in n.targets):
            schema_dicts.add(id(n.value))

    for n in ast.walk(tree):
        if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            names = [target_name(t) for t in targets]
            if n.value is not None and any(is_cred_name(x) for x in names):
                flag(n.value, strict="secret" not in names or not any(
                    isinstance(t, ast.Subscript) for t in targets))
        elif isinstance(n, ast.Dict) and id(n) not in schema_dicts:
            for k, v in zip(n.keys, n.values):
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    if k.value.lower() in ("authorization", "proxy-authorization"):
                        flag(v, strict=False)
                    elif is_cred_name(k.value):
                        flag(v)
        elif isinstance(n, ast.Call):
            for kw in n.keywords:
                if kw.arg == "auth" and isinstance(kw.value, (ast.Tuple, ast.List)):
                    for el in kw.value.elts[1:]:
                        flag(el, strict=False)
                elif kw.arg and kw.arg != "auth" and is_cred_name(kw.arg):
                    flag(kw.value)
            fn = getattr(n.func, "attr", None) or getattr(n.func, "id", None)
            if fn in ("HTTPBasicAuth", "HTTPDigestAuth") and len(n.args) > 1:
                flag(n.args[1], strict=False)
            elif fn in ("setdefault", "putenv") and len(n.args) > 1 and \
                    isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str) and \
                    (fn == "putenv" or n.args[0].value.startswith("TABLEAU_TABEXT_") or
                     is_cred_name(n.args[0].value)):
                flag(n.args[1], strict=False)
        elif isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
            left = n.left
            if isinstance(left, ast.Constant) and isinstance(left.value, str) and \
                    left.value.strip().lower() in ("bearer", "basic", "token"):
                flag(n.right, strict=False)
        elif isinstance(n, (ast.Tuple, ast.List)) and len(n.elts) == 2:
            k = n.elts[0]
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                if k.value.lower() in ("authorization", "proxy-authorization"):
                    flag(n.elts[1], strict=False)
                elif is_cred_name(k.value) and "-" in k.value:
                    flag(n.elts[1])                     # ("X-Api-Key", "literal")
    return spans


def credential_spans(text: str, python: bool = None, config: bool = False):
    """(start, end) spans of credential-looking literals. Never returns values.
    python=None: also run the AST pass when the text parses as Python.
    config=True: also match unquoted name=value lines (.ini / .yaml / .env).
    The runtime block is exempt only when it is byte-identical to the canonical
    runtime: anything else between the markers is scanned like any other code."""
    text = text or ""
    spans = []
    for i, rx in enumerate(CRED_RX):
        for m in rx.finditer(text):
            v = m.group("v")
            if v in PLACEHOLDERS or _NOT_SECRET.match(v) or v.startswith("{"):
                continue
            if i < _N_CONTEXTUAL and not _looks_secret(v, strict=i != 2):
                continue
            spans.append(m.span("v"))
    if config:
        for m in CONFIG_CRED_RX.finditer(text):
            if _looks_secret(m.group("v"), strict=True):
                spans.append(m.span("v"))
    if python is not False:
        spans += _ast_cred_spans(text)
    rt = extract_runtime(text)
    if rt and is_canonical_runtime(rt):
        ra = text.find(rt)
        spans = [s for s in spans if not (ra <= s[0] < ra + len(rt))]
    merged = []
    for a, b in sorted(spans):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return merged


def has_credential(text: str, python: bool = None) -> bool:
    return bool(credential_spans(text, python))


def redact_text(text: str, mask: str = "<REDACTED>") -> str:
    out, last = [], 0
    for a, b in credential_spans(text):
        out += [text[last:a], mask]
        last = b
    out.append(text[last:])
    return "".join(out)


def line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


# ------------------------------------------------------------------ runtime

def runtime_block() -> str:
    return RUNTIME_PATH.read_text()


def runtime_version(text: str):
    m = re.search(re.escape(RUNTIME_BEGIN) + r" (v\d+)", text)
    return m.group(1) if m else None


def is_canonical_runtime(block: str) -> bool:
    """True only if a runtime block is exactly this skill's current runtime."""
    return block is not None and block.strip() == runtime_block().strip()


def is_loopback_url(url: str) -> bool:
    try:
        h = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def extract_runtime(text: str):
    a = text.find(RUNTIME_BEGIN)
    if a < 0:
        return None
    b = text.find(RUNTIME_END, a)
    if b < 0:
        return None
    b = text.find("\n", b)
    return text[a: len(text) if b < 0 else b + 1]


# ----------------------------------------------------------------- workbook
PROLOGUE = "<?xml version='1.0' encoding='utf-8' ?>\n"
MAX_TWB_BYTES = 256 * 1024 * 1024          # a .twb (XML) larger than this is refused (zip bomb guard)
_DTD = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)", re.I)
SCAN_SUFFIXES = (".twb", ".tds", ".xml", ".json", ".py", ".txt", ".csv", ".ini", ".cfg", ".yaml", ".yml", ".env", ".tfl")
SCAN_MAX_BYTES = 20 * 1024 * 1024


class Workbook:
    """A .twb or .twbx opened for read / patch. The original is never modified."""

    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.is_twbx = self.path.suffix.lower() == ".twbx"
        if self.is_twbx:
            with zipfile.ZipFile(self.path) as z:
                names = [n for n in z.namelist() if n.lower().endswith(".twb") and "/" not in n.strip("/")]
                names = names or [n for n in z.namelist() if n.lower().endswith(".twb")]
                if not names:
                    raise SystemExit("no .twb inside %s" % self.path)
                self.inner = names[0]
                info = z.getinfo(self.inner)
                if info.file_size > MAX_TWB_BYTES:
                    raise SystemExit("%s: the .twb inside is %d MB uncompressed; refusing (limit %d MB)"
                                     % (self.path.name, info.file_size >> 20, MAX_TWB_BYTES >> 20))
                raw = z.read(self.inner)
        else:
            self.inner = None
            if self.path.stat().st_size > MAX_TWB_BYTES:
                raise SystemExit("%s is larger than %d MB; refusing" % (self.path.name, MAX_TWB_BYTES >> 20))
            raw = self.path.read_bytes()
        self.raw = raw.decode("utf-8-sig")
        if _DTD.search(self.raw):
            # Tableau never writes a DTD; one in a workbook is an entity-expansion attempt
            raise SystemExit("%s contains a DOCTYPE/ENTITY declaration; refusing to parse it" % self.path.name)
        m = re.search(r"<workbook\b", self.raw)
        head = self.raw[: m.start()] if m else ""
        self.comments = "".join(re.findall(r"<!--.*?-->\s*", head, flags=re.S))
        ET.register_namespace("user", "http://www.tableausoftware.com/xml/user")
        self.root = ET.fromstring(raw)

    def serialize(self) -> str:
        body = ET.tostring(self.root, encoding="unicode")
        # keep CRs in script text the way Tableau writes them
        body = body.replace("\r", "&#13;")
        return PROLOGUE + ("\n" + self.comments if self.comments else "") + body + "\n"

    def member_credentials(self):
        """[(member, count)] for credential-looking literals in the OTHER members of a
        .twbx (text-like files up to SCAN_MAX_BYTES; read in memory, never extracted)."""
        if not self.is_twbx:
            return []
        out = []
        with zipfile.ZipFile(self.path) as z:
            for info in z.infolist():
                n = info.filename
                if n == self.inner or info.is_dir() or not n.lower().endswith(SCAN_SUFFIXES):
                    continue
                if info.file_size > SCAN_MAX_BYTES:
                    out.append((n, -1))         # too big to scan
                    continue
                text = z.read(n).decode("utf-8", "replace")
                low = n.lower()
                k = len(credential_spans(text, python=low.endswith(".py"),
                                         config=low.endswith((".ini", ".cfg", ".yaml", ".yml", ".txt", ".env"))))
                if k:
                    out.append((n, k))
        return out

    def plain_text(self) -> str:
        return html.unescape(ET.tostring(self.root, encoding="unicode"))

    def write(self, out_path) -> Path:
        """Write atomically (temp file + os.replace, so a symlink at out_path is
        replaced, not followed). A .twb source can only be written as .twb."""
        out_path = Path(out_path).expanduser()
        suffix = out_path.suffix.lower()
        if suffix not in (".twb", ".twbx"):
            raise SystemExit("output must end in .twb or .twbx: %s" % out_path)
        if suffix == ".twbx" and not self.is_twbx:
            raise SystemExit("can't write %s: the source is a .twb (no packaged data to carry); "
                             "use a .twb output name" % out_path.name)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        text = self.serialize()
        ET.fromstring(text.encode("utf-8"))  # re-parse check
        fd, tmp = tempfile.mkstemp(dir=str(out_path.parent), prefix=".tabext-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                if suffix == ".twb":
                    fh.write(text.encode("utf-8"))
                else:
                    with zipfile.ZipFile(self.path) as zin, zipfile.ZipFile(fh, "w", zipfile.ZIP_DEFLATED) as zout:
                        for item in zin.infolist():
                            if item.filename == self.inner:
                                zout.writestr(item, text.encode("utf-8"))
                                continue
                            with zin.open(item) as src, zout.open(item, "w") as dst:
                                shutil.copyfileobj(src, dst, 1 << 20)   # streamed: no member held in memory
            os.chmod(tmp, 0o666 & ~_umask())
            os.replace(tmp, out_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return out_path


def _umask() -> int:
    m = os.umask(0)
    os.umask(m)
    return m


def table_extensions(root):
    """Yield (datasource, object, node) for every Table Extension in a workbook."""
    for ds in root.iter("datasource"):
        for obj in ds.iter("object"):
            node = obj.find("./properties/analytics-extension-object-node")
            if node is not None:
                yield ds, obj, node


def script_of(node) -> str:
    return (node.findtext("script-code") or "").replace("\r\n", "\n").replace("\r", "\n")


def read_text_input(path) -> str:
    return Path(path).expanduser().read_text(encoding="utf-8-sig")


def stream(text: str):
    return io.StringIO(text)
