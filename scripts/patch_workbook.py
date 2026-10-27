#!/usr/bin/env python3
"""Put a connector script into a Tableau workbook as a Table Extension.

Replace the script of an existing Table Extension (keeps sheets, the
analytics-extension connection and internal ids):
  python3 patch_workbook.py replace WB.twb --script nasa_neows.py --schema out/schema.json
  python3 patch_workbook.py replace WB.twbx --script s.py --extension "Table Extension" --out new.twbx

Create a new minimal workbook holding one Table Extension data source:
  python3 patch_workbook.py new --script nasa_neows.py --schema out/schema.json \\
      --caption "NASA Close Approaches" --out "NASA NeoWs.twb"

--schema is the schema.json written by run_local.py --out. With it, the
output columns Tableau has recorded (metadata-records + <column> types) are
rewritten to match, date/datetime columns are typed as such (the script
returns ISO strings), and sheets that use a dropped column are listed.
Without it only the script text changes; Tableau re-reads the schema on
Apply / Update Now.

Refuses to write if the script fails check_script.py, if any credential
literal remains anywhere in the output workbook, or if the source
workbook's old credential survives. The source file is never modified.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_script  # noqa: E402
import tabext_common as C  # noqa: E402

REMOTE = {"string": "129", "real": "5", "integer": "20", "boolean": "11", "date": "129", "datetime": "129"}
LOCAL = {"string": "string", "real": "real", "integer": "integer", "boolean": "boolean",
         "date": "string", "datetime": "string"}


def column_attrs(local, typ):
    if typ in ("real", "integer"):
        return {"datatype": typ, "name": local, "role": "measure", "type": "quantitative"}
    if typ in ("date", "datetime"):
        return {"datatype": typ, "datatype-customized": "true", "name": local,
                "role": "dimension", "type": "ordinal"}
    return {"datatype": typ, "name": local, "role": "dimension", "type": "nominal"}


def metadata_record(name, typ, ordinal, parent, local, distinct=None):
    mr = ET.Element("metadata-record", {"class": "column"})
    for tag, val in [("remote-name", name), ("remote-type", REMOTE[typ]), ("local-name", local),
                     ("parent-name", "[%s]" % parent), ("remote-alias", name), ("ordinal", str(ordinal)),
                     ("local-type", LOCAL[typ]),
                     ("aggregation", "Sum" if typ in ("real", "integer") else "Count")]:
        ET.SubElement(mr, tag).text = val
    if distinct:
        ET.SubElement(mr, "approx-count").text = str(distinct)
    ET.SubElement(mr, "contains-null").text = "true"
    if LOCAL[typ] == "string":
        ET.SubElement(mr, "collation", {"flag": "0", "name": "binary"})
    return mr


def field_ref(name):
    """Tableau field reference: [name], with ] doubled the way Tableau escapes it."""
    return "[%s]" % str(name).replace("]", "]]")


def load_schema(path):
    if not path:
        return None
    d = json.loads(Path(path).expanduser().read_text())
    if isinstance(d, dict) and not d.get("ok", True):
        raise SystemExit("schema.json comes from a run that violated the contract - fix the script first")
    cols = d["columns"] if isinstance(d, dict) else d
    for c in cols:
        if c["type"] not in C.SCHEMA_TYPES:
            raise SystemExit("schema.json: column %r has type %r" % (c["name"], c["type"]))
    return cols


def apply_schema(ds, node, cols, root, log, ext_caption="Table Extension"):
    """Rewrite the extension's output metadata-records and <column> types.

    Tableau names a field by its local-name. When an output column has the same
    name as a column of another table in the data source (typically the input
    table that drives lookups), Tableau gives it a disambiguated local-name such as
    [id (Table Extension 176BAB88)]. Existing local-names are therefore kept
    as they are (sheets reference them), and new columns get a local-name that
    collides with nothing."""
    out_rel = node.find("./output/relation")
    if out_rel is None:
        log.append("no <output><relation> on the extension - schema left for Tableau to discover")
        return []
    parent = out_rel.get("name")
    mrs_parent = ds.find("./connection/metadata-records")
    if mrs_parent is None:
        conn = ds.find("./connection")
        mrs_parent = ET.SubElement(conn, "metadata-records")
    mine = [mr for mr in list(mrs_parent) if mr.findtext("parent-name") == "[%s]" % parent]
    old_local = dict((mr.findtext("remote-name"), mr.findtext("local-name") or field_ref(mr.findtext("remote-name")))
                     for mr in mine)
    taken = set(mr.findtext("local-name") for mr in list(mrs_parent) if mr not in mine)
    taken |= set(c.get("name") for c in ds.findall("column")) - set(old_local.values())
    for mr in mine:
        mrs_parent.remove(mr)
    new_local = {}
    for i, c in enumerate(cols):
        loc = old_local.get(c["name"])
        if loc is None:
            loc = field_ref(c["name"])
            k = 1
            while loc in taken or loc in new_local.values():
                loc = "[%s (%s%s)]" % (c["name"], ext_caption, "" if k == 1 else " %d" % k)
                k += 1
        new_local[c["name"]] = loc
        mrs_parent.append(metadata_record(c["name"], c["type"], i, parent, loc, c.get("distinct")))
    old_names = list(old_local)
    new_names = [c["name"] for c in cols]
    dropped = [n for n in old_names if n not in new_names]
    # datasource-level <column> declarations (insert where the old ones were)
    ds_cols = {c.get("name"): c for c in ds.findall("column")}
    children = list(ds)
    insert_at = next((i for i, ch in enumerate(children) if ch.tag == "column"), None)
    if insert_at is None:
        insert_at = next((i + 1 for i, ch in enumerate(children) if ch.tag == "connection"), len(children))
    for n in dropped:
        el = ds_cols.get(old_local[n])
        if el is not None:
            ds.remove(el)
    for c in cols:
        loc = new_local[c["name"]]
        el = ds_cols.get(loc)
        want = column_attrs(loc, c["type"])
        if el is not None and el in list(ds):
            if el.get("datatype") != c["type"] and el.get("calculation") is None:
                for k in ("datatype", "role", "type", "datatype-customized"):
                    el.attrib.pop(k, None)
                el.attrib.update(want)
        elif c["type"] in ("date", "datetime", "real", "integer"):
            ds.insert(insert_at, ET.Element("column", want))
            insert_at += 1
    renamed = [n for n in new_names if new_local[n] != field_ref(n) and n not in old_local]
    # who used a dropped column?
    hit = {}
    for ws in root.iter("worksheet"):
        text = ET.tostring(ws, encoding="unicode")
        for n in dropped:
            if old_local[n] in text:
                hit.setdefault(n, []).append(ws.get("name"))
    log.append("output schema: %d columns (%d new, %d dropped)" % (
        len(cols), len([n for n in new_names if n not in old_names]), len(dropped)))
    for n in renamed:
        log.append("note: new column %r shares a name with another table's column; its field is %s"
                   % (n, new_local[n]))
    for n, sheets in hit.items():
        log.append("WARNING dropped column %r is used on sheet(s): %s - fix those sheets in Tableau" % (n, ", ".join(sheets)))
    return dropped


NEW_TEMPLATE = """<?xml version='1.0' encoding='utf-8' ?>
<workbook original-version='18.1' source-build='tableau-table-extension skill' source-platform='mac' version='18.1' xmlns:user='http://www.tableausoftware.com/xml/user'>
  <document-format-change-manifest>
    <AnalyticsExtensionsElodie />
    <ObjectModelEncapsulateLegacy />
    <ObjectModelTableType />
    <SchemaViewerObjectModel />
    <SheetIdentifierTracking />
  </document-format-change-manifest>
  <preferences />
  <datasources>
    <datasource caption='{caption}' inline='true' name='federated.{ds_id}' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='tableau-analytics-extension' name='tableau-analytics-extension.{conn_id}'>
            <connection class='tableau-analytics-extension' schema='public' workgroup-auth-mode='as-is' />
          </named-connection>
        </named-connections>
        <relation connection='tableau-analytics-extension.{conn_id}' name='{rel}' table='[public].[{rel}]' type='table' />
        <metadata-records />
      </connection>
      <aliases enabled='yes' />
      <column caption='{ext_caption}' datatype='table' name='[__tableau_internal_object_id__].[{obj_id}]' role='measure' type='quantitative' />
      <layout dim-ordering='alphabetic' measure-ordering='alphabetic' show-structure='true' />
      <object-graph>
        <objects>
          <object caption='{ext_caption}' id='{obj_id}'>
            <properties context=''>
              <analytics-extension-object-node>
                <script-code />
                <input />
                <output>
                  <relation connection='tableau-analytics-extension.{conn_id}' name='{rel}' table='[public].[{rel}]' type='table' />
                </output>
              </analytics-extension-object-node>
            </properties>
          </object>
        </objects>
      </object-graph>
    </datasource>
  </datasources>
  <worksheets>
    <worksheet name='Sheet 1'>
      <table>
        <view>
          <datasources>
            <datasource caption='{caption}' name='federated.{ds_id}' />
          </datasources>
          <aggregation value='true' />
        </view>
        <style />
        <panes>
          <pane selection-relaxation-option='selection-relaxation-allow'>
            <view>
              <breakdown value='auto' />
            </view>
            <mark class='Automatic' />
          </pane>
        </panes>
        <rows />
        <cols />
      </table>
    </worksheet>
  </worksheets>
  <windows>
    <window class='worksheet' maximized='true' name='Sheet 1'>
      <cards>
        <edge name='left'><strip size='160'><card type='pages' /><card type='filters' /><card type='marks' /></strip></edge>
        <edge name='top'><strip size='2147483647'><card type='columns' /></strip><strip size='2147483647'><card type='rows' /></strip><strip size='31'><card type='title' /></strip></edge>
      </cards>
    </window>
  </windows>
</workbook>
"""


def xml_attr(s):
    return (s.replace("&", "&amp;").replace("'", "&apos;").replace("<", "&lt;").replace(">", "&gt;"))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("replace")
    r.add_argument("workbook")
    r.add_argument("--extension", help="caption of the Table Extension (needed if there are several)")
    n = sub.add_parser("new")
    n.add_argument("--caption", default="Table Extension data source")
    n.add_argument("--extension-caption", default="Table Extension")
    for p in (r, n):
        p.add_argument("--script", required=True)
        p.add_argument("--schema", help="schema.json from run_local.py --out")
        p.add_argument("--out", help="output .twb/.twbx (default: '<name> (Table Extension).twb[x]')")
        p.add_argument("--uses-input", action="store_true")
        p.add_argument("--force", action="store_true",
                       help="write even if check_script reports errors (never for TE-SECRET or a "
                            "hand-edited runtime)")
        p.add_argument("--overwrite", action="store_true", help="replace --out if it already exists")
    a = ap.parse_args(argv)

    script = C.read_text_input(a.script).replace("\r\n", "\n")
    errs = [f for f in check_script.check(script, uses_input=a.uses_input or None) if f["level"] == "error"]
    if errs and not a.force:
        for f in errs:
            print("ERROR %s line %s: %s" % (f["rule"], f["line"], f["message"]), file=sys.stderr)
        print("check_script failed - not writing (fix, or --force for non-secret errors)", file=sys.stderr)
        return 2
    if any(f["rule"] == "TE-SECRET" for f in errs):
        print("refusing: the script contains a credential literal (even with --force)", file=sys.stderr)
        return 2
    if any(f["rule"] == "TE-RUNTIME-DRIFT" for f in errs):
        print("refusing: the TABEXT runtime block was edited by hand (even with --force). Run "
              "new_connector.py --refresh-runtime SCRIPT and put custom code outside the markers",
              file=sys.stderr)
        return 2
    cols = load_schema(a.schema)
    log = []

    if a.cmd == "replace":
        wb = C.Workbook(a.workbook)
        old_secret_spans = []
        exts = list(C.table_extensions(wb.root))
        if not exts:
            print("no Table Extension in %s - use `new`, or add one in Tableau (Data Source > New Table "
                  "Extension) and rerun" % wb.path.name, file=sys.stderr)
            return 2
        if a.extension:
            exts = [e for e in exts if (e[1].get("caption") or "") == a.extension]
        if len(exts) != 1:
            print("found %d matching Table Extensions; pass --extension CAPTION. Captions: %s" % (
                len(exts), [e[1].get("caption") for e in C.table_extensions(wb.root)]), file=sys.stderr)
            return 2
        ds, obj, node = exts[0]
        old = C.script_of(node)
        old_secret_spans = [old[s:e] for s, e in C.credential_spans(old)]
        sc = node.find("script-code")
        if sc is None:
            sc = ET.SubElement(node, "script-code")
        sc.text = script
        log.append("script of %r in %r replaced (%d -> %d lines)" % (
            obj.get("caption"), ds.get("caption") or ds.get("name"), old.count("\n") + 1, script.count("\n") + 1))
        if cols:
            apply_schema(ds, node, cols, wb.root, log, obj.get("caption") or "Table Extension")
        out = Path(a.out).expanduser() if a.out else wb.path.with_name("%s (Table Extension)%s" % (wb.path.stem, wb.path.suffix))
    else:
        ids = dict(ds_id=uuid.uuid4().hex[:28], conn_id=uuid.uuid4().hex[:28],
                   obj_id="_" + uuid.uuid4().hex.upper(),
                   rel="Tableau_7_%s_1_TableauTempExtSvc" % str(uuid.uuid4()).upper(),
                   caption=xml_attr(a.caption), ext_caption=xml_attr(a.extension_caption))
        tmp = Path(a.out or "%s.twb" % a.caption).expanduser()
        if tmp.suffix.lower() != ".twb":
            raise SystemExit("`new` writes a .twb (package it in Tableau if you need a .twbx)")
        text = NEW_TEMPLATE.format(**ids)
        wb = C.Workbook.__new__(C.Workbook)
        wb.path, wb.is_twbx, wb.inner, wb.raw, wb.comments = tmp, False, None, text, ""
        ET.register_namespace("user", "http://www.tableausoftware.com/xml/user")
        wb.root = ET.fromstring(text.encode("utf-8"))
        ds, obj, node = next(C.table_extensions(wb.root))
        node.find("script-code").text = script
        old_secret_spans = []
        if cols:
            apply_schema(ds, node, cols, wb.root, log, a.extension_caption)
        else:
            log.append("no --schema: open the workbook, Data Source tab > Update Now to discover columns")
        out = tmp

    # ---- validation ---------------------------------------------------------
    plain = wb.plain_text()
    problems = []
    if C.has_credential(plain, python=False):
        problems.append("a credential-looking literal is present in the output workbook XML")
    for m, k in (wb.member_credentials() if a.cmd == "replace" else []):
        if k > 0:
            problems.append("package member %s holds %d credential-looking literal(s); remove it in "
                            "Tableau (or repackage) first" % (m, k))
        elif k < 0:
            print("warning: package member %s is too large to scan for credentials; check it yourself"
                  % m, file=sys.stderr)
    if any(s and s in plain for s in old_secret_spans):
        problems.append("the old script's credential is still somewhere in the workbook")
    round_trip = ET.fromstring(wb.serialize().encode("utf-8"))
    got = [C.script_of(nd) for _d, _o, nd in C.table_extensions(round_trip)]
    if script.rstrip("\n") not in [g.rstrip("\n") for g in got]:
        problems.append("script did not survive XML round-trip unchanged")
    if problems:
        for p in problems:
            print("VALIDATION FAILED: " + p, file=sys.stderr)
        return 2
    if a.cmd == "replace" and out.exists() and os.path.samefile(out, wb.path):
        raise SystemExit("refusing to overwrite the source workbook; pass a different --out")
    if out.exists() and not a.overwrite:
        raise SystemExit("%s already exists; pass --overwrite to replace it, or a different --out" % out)
    written = wb.write(out)
    for line in log:
        print(line)
    if old_secret_spans:
        print("removed %d embedded credential(s) from the script - ROTATE them: they were readable by anyone "
              "with the workbook and are in TabPy logs" % len(old_secret_spans))
    print("wrote %s" % written)
    print("next: open in Tableau > Data Source tab > select the Table Extension > Apply > Update Now"
          "%s" % ("" if a.cmd == "replace" else " (pick your TabPy analytics-extension connection if asked)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
