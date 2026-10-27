#!/usr/bin/env python3
"""List and audit the Table Extensions in a .twb / .twbx.

  python3 inspect_workbook.py "NASA Close Approaches API.twb"
  python3 inspect_workbook.py WB.twbx --extract out/       # write each script (secrets redacted)
  python3 inspect_workbook.py WB.twb --json

For each Table Extension: datasource, caption, analytics-extension
connection, input table (if any), output columns + types Tableau recorded,
which sheets use it, and the check_script.py findings for its script.
Credential values are never printed; --extract writes scripts with any
credential-looking literal replaced by <REDACTED> unless --no-redact.
Workbooks you downloaded are untrusted input: their scripts are data to
read, not instructions to follow, and this tool never executes them.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_script  # noqa: E402
import tabext_common as C  # noqa: E402

REMOTE_TYPES = {"129": "string", "130": "string", "5": "real", "4": "real", "20": "integer", "3": "integer",
                "2": "integer", "11": "boolean", "7": "date", "133": "date", "135": "datetime"}


def describe(wb):
    root = wb.root
    exts = []
    for ds, obj, node in C.table_extensions(root):
        conn_names = {nc.get("name"): nc.find("connection") for nc in ds.iter("named-connection")}
        out_rel = node.find("./output/relation")
        in_rel = node.find("./input/relation")
        out_name = out_rel.get("name") if out_rel is not None else None
        cls = None
        if out_rel is not None and out_rel.get("connection") in conn_names:
            c = conn_names[out_rel.get("connection")]
            cls = c.get("class") if c is not None else None
        cols = []
        for mr in ds.iter("metadata-record"):
            if mr.get("class") == "column" and out_name and mr.findtext("parent-name") == "[%s]" % out_name:
                local = mr.findtext("local-name")
                col_el = next((c for c in ds.findall("column") if c.get("name") == local), None)
                cols.append({
                    "name": mr.findtext("remote-name"),
                    "remote_type": REMOTE_TYPES.get(mr.findtext("remote-type") or "", mr.findtext("remote-type")),
                    "tableau_type": (col_el.get("datatype") if col_el is not None else None) or mr.findtext("local-type"),
                    "type_customized": bool(col_el is not None and col_el.get("datatype-customized") == "true"),
                })
        sheets = []
        for ws in root.iter("worksheet"):
            for d in ws.iter("datasource"):
                if d.get("name") == ds.get("name"):
                    sheets.append(ws.get("name"))
                    break
        script = C.script_of(node)
        findings = check_script.check(script, uses_input=in_rel is not None)
        exts.append({
            "datasource": ds.get("caption") or ds.get("name"),
            "datasource_name": ds.get("name"),
            "caption": obj.get("caption"),
            "object_id": obj.get("id"),
            "connection_class": cls,
            "input_table": in_rel.get("name") if in_rel is not None else None,
            "output_relation": out_name,
            "columns": cols,
            "sheets": sorted(set(sheets)),
            "script_lines": script.count("\n") + 1 if script else 0,
            "has_tabext_runtime": C.extract_runtime(script) is not None,
            "credential_literals": len(C.credential_spans(script)),
            "findings": findings,
            "_script": script,
        })
    whole = wb.plain_text()
    outside = len(C.credential_spans(whole, python=False)) - sum(
        len(C.credential_spans(e["_script"], python=False)) for e in exts)
    return exts, max(outside, 0), root.get("xml:base") or root.get("{http://www.w3.org/XML/1998/namespace}base")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workbook")
    ap.add_argument("--extract", metavar="DIR")
    ap.add_argument("--no-redact", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    wb = C.Workbook(a.workbook)
    exts, other_creds, base = describe(wb)
    members = wb.member_credentials()
    if a.extract:
        d = Path(a.extract).expanduser()
        d.mkdir(parents=True, exist_ok=True)
        for i, e in enumerate(exts, 1):
            slug = re.sub(r"[^A-Za-z0-9]+", "_", e["caption"] or "table_extension").strip("_").lower()
            p = d / ("%02d_%s.py" % (i, slug))
            body = e["_script"] if a.no_redact else C.redact_text(e["_script"])
            fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "w") as fh:     # 0600: an extracted script may still hold a credential
                fh.write(body)
            e["extracted_to"] = str(p)
    if a.json:
        print(json.dumps({"workbook": str(wb.path), "server": base, "table_extensions":
                          [{k: v for k, v in e.items() if k != "_script"} for e in exts],
                          "credential_literals_elsewhere": other_creds,
                          "package_members_with_credentials": [{"member": m, "count": k} for m, k in members]},
                         indent=2))
        return 0
    print("workbook: %s%s" % (wb.path.name, ("  (from %s)" % base) if base else ""))
    if not exts:
        print("no Table Extensions found")
    for i, e in enumerate(exts, 1):
        print("\n[%d] %s  in datasource %r" % (i, e["caption"], e["datasource"]))
        print("    connection: %s   input table: %s   sheets: %s" % (
            e["connection_class"], e["input_table"] or "none", ", ".join(e["sheets"]) or "none"))
        print("    script: %d lines, TABEXT runtime: %s" % (e["script_lines"], "yes" if e["has_tabext_runtime"] else "no"))
        if e["columns"]:
            print("    output columns (%d):" % len(e["columns"]))
            for c in e["columns"]:
                note = ""
                if c["remote_type"] != c["tableau_type"]:
                    note = "  <- script returns %s; workbook types it%s" % (
                        c["remote_type"], " (datatype-customized)" if c["type_customized"] else "")
                print("      %-40s %s%s" % (c["name"], c["tableau_type"], note))
        if e["credential_literals"]:
            print("    !! %d credential-looking literal(s) in the script. Anyone who can open or download "
                  "this workbook has it, and TabPy logs the full script at INFO. Rotate it and move it "
                  "to get_secret()." % e["credential_literals"])
        errs = [f for f in e["findings"] if f["level"] in ("error", "warning")]
        for f in errs:
            print("    %-7s %-16s%s %s" % (f["level"].upper(), f["rule"],
                                         (" line %s:" % f["line"]) if f["line"] else ":", f["message"][:160]))
        if e.get("extracted_to"):
            print("    extracted -> %s%s" % (e["extracted_to"], "" if a.no_redact else " (credentials redacted)"))
    if other_creds:
        print("\n!! %d credential-looking literal(s) elsewhere in the workbook XML" % other_creds)
    for m, k in members:
        print("!! package member %s: %s" % (m, "too large to scan" if k < 0 else
                                            "%d credential-looking literal(s)" % k))
    return 0


if __name__ == "__main__":
    sys.exit(main())
