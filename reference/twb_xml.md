# Table Extension XML in a .twb

From a workbook made by Tableau 2025.x/2026.x (document `version='18.1'`).
`inspect_workbook.py` and `patch_workbook.py` rely on this structure.

```xml
<datasource caption='… (Multiple Connections)' inline='true' name='federated.<id>' version='18.1'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='tableau-analytics-extension' name='tableau-analytics-extension.<conn-id>'>
        <connection class='tableau-analytics-extension' schema='public' workgroup-auth-mode='as-is' />
      </named-connection>
    </named-connections>
    <relation connection='tableau-analytics-extension.<conn-id>'
              name='Tableau_7_<GUID>_1_TableauTempExtSvc'
              table='[public].[Tableau_7_<GUID>_1_TableauTempExtSvc]' type='table' />
    <metadata-records>
      <metadata-record class='column'>          <!-- one per output column -->
        <remote-name>miss_distance_km</remote-name>
        <remote-type>5</remote-type>            <!-- 129 string, 5 real, 20 integer, 11 boolean -->
        <local-name>[miss_distance_km]</local-name>
        <parent-name>[Tableau_7_<GUID>_1_TableauTempExtSvc]</parent-name>
        <remote-alias>miss_distance_km</remote-alias>
        <ordinal>13</ordinal>
        <local-type>real</local-type>
        <aggregation>Sum</aggregation>
        <contains-null>true</contains-null>
      </metadata-record>
    </metadata-records>
  </connection>
  <column caption='Table Extension' datatype='table'
          name='[__tableau_internal_object_id__].[_<OBJID>]' role='measure' type='quantitative' />
  <column datatype='datetime' datatype-customized='true' name='[close_approach_datetime]'
          role='dimension' type='ordinal' />      <!-- user changed string -> datetime -->
  …
  <object-graph><objects>
    <object caption='Table Extension' id='_<OBJID>'>
      <properties context=''>
        <analytics-extension-object-node>
          <script-code>…the script, XML-escaped; CR as &#13;…</script-code>
          <input />                               <!-- or <input><relation …/></input> for an input table -->
          <output>
            <relation connection='tableau-analytics-extension.<conn-id>'
                      name='Tableau_7_<GUID>_1_TableauTempExtSvc' … type='table' />
          </output>
        </analytics-extension-object-node>
      </properties>
    </object>
  </objects></object-graph>
</datasource>
```

Facts that matter when editing:
- The **script** is the text of `<script-code>`. Tableau saves CRLF as `&#13;\n`.
  `Workbook.serialize()` preserves it, and `script_of()` normalizes to LF for checks.
- **Column types Tableau recorded** live in two places: the `metadata-record`
  (`remote-type`/`local-type`, i.e. what the script returned) and an optional
  datasource-level `<column datatype=…>` (what the user or Tableau set). A date or
  datetime returned as a string shows up as remote 129 / local string, plus
  `<column datatype='datetime' datatype-customized='true'>`.
  `patch_workbook --schema` writes exactly that.
- Sheets refer to fields as `[federated.<id>].[none:<col>:nk]` etc. Renaming or
  dropping an output column breaks those references. `patch_workbook` lists
  the affected sheets but doesn't rewrite them.
- The analytics-extension **connection details** (host, port, user) aren't in
  the workbook. Desktop uses the machine's configured analytics extension, and
  Server/Cloud use the site's. A downloaded workbook may carry `xml:base` = the
  Server/Cloud URL it came from.
- `.twbx` = zip with one top-level `.twb` (plus `Data/`, `Image/`…).
  `Workbook.write()` rewrites only the `.twb` member and copies everything else.
- **Update Now** in Tableau re-runs the script and refreshes the metadata
  records itself. Patching them is a convenience so the workbook opens
  with the right types, and so sheets don't break before the first refresh.
- [Unconfirmed] The minimum Tableau version that opens a workbook built by
  `patch_workbook new` (the template is document version 18.1, i.e. 2025.x-era).
  Opening in Tableau Desktop hasn't been verified by the skill's tests.
