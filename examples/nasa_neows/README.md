# Example: NASA NeoWs close approaches

A rebuild of the Table Extension in the "NASA Close Approaches API" workbook.

| | Original script | `nasa_neows.py` |
|---|---|---|
| API key | literal in the script (so in the workbook and in TabPy's INFO log) | `get_secret("nasa.api_key")` on the TabPy host |
| Key transport | `?api_key=` query string (also in TabPy's urllib3 DEBUG log) | `X-Api-Key` header (api.data.gov accepts it) |
| Timeouts | none (`requests.get` can hang to TabPy's 30 s kill) | connect 10 s / read ≤ 20 s, 25 s total budget |
| Retries / rate limit | none | 429/5xx with `Retry-After`, within budget |
| Window | single request | `date_window` pager in 7-day slices (the API's max per request) |
| Types | 5 numeric columns (velocity, miss distance) came back as strings, so Tableau typed them `string` | `real` / `boolean` / `datetime` (UTC from epoch ms) |
| `check_script` | 2 errors (TE-SECRET, TE-TIMEOUT) + 2 warnings | clean |

Grain: one row per close approach of a near-Earth object in the next 7 days
(about 40–150 rows). Columns: `id, name, nasa_jpl_url, absolute_magnitude_h,
estimated_diameter_min_meters, estimated_diameter_max_meters,
is_potentially_hazardous_asteroid, is_sentry_object, close_approach_datetime,
relative_velocity_km_sec, relative_velocity_km_hour,
miss_distance_astronomical, miss_distance_lunar, miss_distance_km, orbiting_body`.

```bash
S=~/.claude/skills/tableau-table-extension
python3 $S/scripts/set_secret.py nasa.api_key          # hidden dialog; or DEMO_KEY for a trial
python3 $S/scripts/check_script.py $S/examples/nasa_neows/nasa_neows.py
python3 $S/scripts/run_local.py   $S/examples/nasa_neows/nasa_neows.py --out /tmp/neows
python3 $S/scripts/patch_workbook.py replace "NASA Close Approaches API.twb" \
    --script $S/examples/nasa_neows/nasa_neows.py --schema /tmp/neows/schema.json
```
Measured here: 42 rows × 15 columns in about 1.4 s in-process and about 1.0 s through TabPy 2.16.

If the original key was ever saved in the workbook, **rotate it** at
api.nasa.gov. It's in every copy of the workbook and in the logs of the TabPy that ran it.
