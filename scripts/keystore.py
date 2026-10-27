"""Shared secret store for the tableau-table-extension skill.

The generated Table Extension script (TABEXT runtime get_secret) resolves secrets with the
same lookup chain implemented here, so anything stored with
``set_secret.py`` is visible to the script at refresh time:

  1. Environment variable  TABLEAU_TABEXT_<NAME>   (NAME upper-cased, non
     alphanumerics -> '_', e.g. nasa.api_key -> TABLEAU_TABEXT_NASA_API_KEY)
  2. macOS Keychain         service 'tableau-tabext', account '<name>'
  3. OS keyring             (python 'keyring' package: Windows Credential
                             Manager, Linux Secret Service) if installed
  4. JSON file              $TABLEAU_TABEXT_SECRETS_FILE or
                             ~/.tableau-tabext/secrets.json (chmod 600)

Values that are not plain single-line ASCII (service-account JSON, keys
with quotes) are stored as 'b64:<base64>' so they survive the Keychain
CLI; ``get_secret`` decodes them transparently.

Nothing in this module ever prints a secret value.
"""
from __future__ import annotations

import base64
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Optional

SERVICE = "tableau-tabext"
SECURITY = "/usr/bin/security"
# Characters that pass through `security -i` quoting untouched. Anything
# else (quotes, spaces, backslashes, newlines, JSON) is stored base64.
_SAFE = re.compile(r"^[A-Za-z0-9._~+/=:@%-]+$")
# Secret / service names are interpolated into a `security -i` command line,
# so they are restricted to characters that need no quoting.
NAME_RX = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
DEFAULT_DIR = Path(os.path.expanduser("~/.tableau-tabext"))


def check_name(name: str, what: str = "secret name") -> str:
    if not NAME_RX.match(name or ""):
        raise ValueError("invalid %s %r: use 1-64 of A-Z a-z 0-9 . _ -" % (what, name))
    return name


def _warn_if_shared(p: Path) -> None:
    """Warn (stderr, no value) when the secrets file is group/other readable."""
    try:
        if os.name == "posix" and p.stat().st_mode & 0o077:
            print("WARNING: %s is readable by other users; run: chmod 600 '%s'" % (p, p),
                  file=sys.stderr)
    except OSError:
        pass


def env_name(name: str) -> str:
    return "TABLEAU_TABEXT_" + "".join(c if c.isalnum() else "_" for c in name).upper()


def secrets_file() -> Path:
    return Path(os.environ.get("TABLEAU_TABEXT_SECRETS_FILE")
                or os.path.expanduser("~/.tableau-tabext/secrets.json"))


def _decode(v: Optional[str]) -> str:
    v = (v or "").strip()
    if v.startswith("b64:"):
        try:
            return base64.b64decode(v[4:]).decode("utf-8").strip()
        except Exception:
            return ""
    return v


def _encode(v: str) -> str:
    """Keychain-CLI-safe representation of a value."""
    if _SAFE.match(v) and '"' not in v and "\\" not in v and not v.startswith("b64:"):
        return v                        # (a literal "b64:..." value is encoded too, or reads would decode it)
    return "b64:" + base64.b64encode(v.encode("utf-8")).decode("ascii")


def is_macos_keychain() -> bool:
    return sys.platform == "darwin" and os.path.exists(SECURITY)


# --------------------------------------------------------------------- read
def get_secret(name: str, service: str = SERVICE) -> tuple[str, str]:
    """Return (value, source). value == '' when not found."""
    check_name(name)
    check_name(service, "service")
    v = _decode(os.environ.get(env_name(name), ""))
    if v:
        return v, "env:" + env_name(name)
    if is_macos_keychain():
        try:
            r = subprocess.run([SECURITY, "find-generic-password", "-s", service,
                                "-a", name, "-w"],
                               capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                v = _decode(r.stdout)
                if v:
                    return v, "macos-keychain"
        except Exception:
            pass
    try:
        import keyring  # type: ignore
        v = _decode(keyring.get_password(service, name))
        if v:
            return v, "keyring"
    except Exception:
        pass
    p = secrets_file()
    try:
        data = json.loads(p.read_text())
        raw = data.get(name, "") or ""
        v = _decode(raw if isinstance(raw, str) else json.dumps(raw))
        if v:
            _warn_if_shared(p)
            return v, "file:" + str(p)
    except Exception:
        pass
    return "", ""


# -------------------------------------------------------------------- write
def store_secret(name: str, value: str, backend: str = "auto",
                 service: str = SERVICE) -> str:
    """Persist a secret. Returns the backend used. Never echoes the value."""
    check_name(name)
    check_name(service, "service")
    value = value.strip()
    if not value:
        raise ValueError("empty value - nothing stored")
    if backend == "auto":
        backend = "macos-keychain" if is_macos_keychain() else (
            "keyring" if _has_keyring() else "file")

    if backend == "macos-keychain":
        enc = _encode(value)
        # Feed the command through `security -i` on stdin so the secret
        # never appears in argv (ps / shell history).
        cmd = ('add-generic-password -U -s "%s" -a "%s" -l "%s" -w "%s"\n'
               % (service, name, "Tableau Table Extension: " + name, enc))
        r = subprocess.run([SECURITY, "-i"], input=cmd, capture_output=True,
                           text=True, timeout=30)
        got, _ = get_secret_from_keychain(name, service)
        if got != value:
            raise RuntimeError("Keychain write did not verify (security rc=%s). "
                               "Is the login keychain unlocked?" % r.returncode)
        return backend

    if backend == "keyring":
        import keyring  # type: ignore
        keyring.set_password(service, name, _encode(value))
        return backend

    if backend == "file":
        p = secrets_file()
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if p.parent.is_symlink():
            raise RuntimeError("%s is a symlink; refusing to write secrets through it" % p.parent)
        if p.parent.resolve() == DEFAULT_DIR.resolve():
            os.chmod(p.parent, 0o700)   # only tighten our own directory, never a custom parent
        elif p.parent.stat().st_mode & 0o022:
            print("warning: %s is group/world-writable; another user could replace the secrets file"
                  % p.parent, file=sys.stderr)
        data = _read_secrets_file(p)
        data[name] = _encode(value)
        _write_secrets_file(p, data)
        return "file:" + str(p)

    raise ValueError("unknown backend " + backend)


def get_secret_from_keychain(name: str, service: str = SERVICE) -> tuple[str, str]:
    check_name(name)
    check_name(service, "service")
    if not is_macos_keychain():
        return "", ""
    r = subprocess.run([SECURITY, "find-generic-password", "-s", service, "-a",
                        name, "-w"], capture_output=True, text=True, timeout=15)
    return (_decode(r.stdout), "macos-keychain") if r.returncode == 0 else ("", "")


def delete_secret(name: str, service: str = SERVICE) -> list[str]:
    check_name(name)
    check_name(service, "service")
    removed = []
    if is_macos_keychain():
        r = subprocess.run([SECURITY, "delete-generic-password", "-s", service,
                            "-a", name], capture_output=True, text=True)
        if r.returncode == 0:
            removed.append("macos-keychain")
    if _has_keyring():
        try:
            import keyring  # type: ignore
            keyring.delete_password(service, name)
            removed.append("keyring")
        except Exception:
            pass
    p = secrets_file()
    if p.exists():
        data = _read_secrets_file(p)
        if name in data:
            del data[name]
            _write_secrets_file(p, data)
            removed.append("file")
    return removed


def _read_secrets_file(p: Path) -> dict:
    """Existing secrets, or {} if there is no file. Never silently discards a
    file it can't parse (that would wipe every other stored secret)."""
    if p.is_symlink():
        raise RuntimeError("%s is a symlink; refusing to use it as the secrets file" % p)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        raise RuntimeError("%s is not valid JSON (%s); fix or move it - not overwriting it"
                           % (p, type(e).__name__)) from None
    if not isinstance(data, dict):
        raise RuntimeError("%s must hold a JSON object; not overwriting it" % p)
    return data


def _write_secrets_file(p: Path, data: dict) -> None:
    """Atomic, owner-only write: temp file (O_EXCL, 0600, no symlink follow) in
    the same directory, fsync, then rename over the old file."""
    tmp = p.with_name(".%s.%d.tmp" % (p.name, os.getpid()))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(tmp), flags, stat.S_IRUSR | stat.S_IWUSR)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)   # umask can't loosen it
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp), str(p))
    except BaseException:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise


def _has_keyring() -> bool:
    try:
        import keyring  # type: ignore  # noqa: F401
        return True
    except Exception:
        return False


def describe(value: str) -> str:
    """Non-revealing description of a secret, safe to print."""
    if not value:
        return "absent"
    return "present (%d chars)" % len(value)
