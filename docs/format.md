# Published format (contract between pipeline and app)

Base URL: `https://mrgatzi.github.io/schwammerl-data/` (GitHub Pages, HTTP Range + CORS verified 2026-10-26).
All paths below are relative to the base URL. Stamps are `YYYYMMDDHHmm` (UTC) of the publish run.

Each publish keeps the stamped files of the previous publish online (the app may hold an `index.json` for up to 3 h; runs are ~6 h apart), and deletes anything older.

## index.json

```json
{
  "v": 1,
  "generated": "2026-10-26T07:42:00Z",
  "weatherGenerated": "2026-10-26T06:31:12+00:00",
  "rainDays": ["2026-10-12", "2026-10-25"],
  "bounds": [5.8, 45.75, 17.3, 55.1],
  "species": { "steinpilz": { "file": "chance/steinpilz-202610260742.pmtiles" } },
  "rain": { "file": "rain-202610260742.pmtiles", "periods": [1, 3, 7, 14],
            "levels": [0, 2, 5, 10, 15, 20, 30, 40, 60, 80, 150] },
  "trees": { "file": "trees-1.pmtiles" },
  "cells": { "dir": "cells-202610260742", "step": 1 },
  "stations": { "file": "stations-202610260742.json" }
}
```

`bounds` = [west, south, east, north] in degrees. Species ids are his `PILZ_IDS` (lowercase, as in `pilz.js`).

## Vector tiles (PMTiles, EPSG:3857 tiles, MVT)

| File | Layer | Geometry | Properties | Zooms |
|---|---|---|---|---|
| `chance/<id>-<stamp>.pmtiles` | `chance` | Polygon | `lv` int 1 = Möglich, 2 = Gut, 3 = Top (disjoint bands) | z5–z13 |
| `rain-<stamp>.pmtiles` | `rain` | Polygon | `p` int period days (1/3/7/14), `mm` lower bound, `to` upper bound (number, last band `to` = 9999); a band covers `mm < value ≤ to` | z4–z10 |
| same file | `rain_lines` | LineString | `p`, `mm` (isoline value, for labels) | z6–z10 |
| `trees-<ver>.pmtiles` | `trees` | Polygon | `g` string genus key, same keys as the app's `treeColorExpression` (`src/map/constants.ts`) | z7–z13 |

## cells-<stamp>/<lon>_<lat>.json (tap cards)

One file per 1°×1° chunk, named by the integer floor of the south-west corner, e.g. `14_48.json` covers lon 14–15, lat 48–49.
The grid is the ~1 km weather grid of `pilz_weather` (bounds [[45.75,5.8],[55.1,17.3]], nx 920, ny 1039, row 0 = north).

```json
{ "days": ["2026-10-12", "…", "2026-10-25"],
  "cells": { "<row>_<col>": { "r": [0.0, 1.2, …14 values mm, null = no data], "st": 9.4, "m": 0.31 } } }
```

`st` = `soil_t0` °C, `m` = `moist`. Cells without any data are left out. The app finds the cell by
`row = floor((55.1 - lat) / 0.009)`, `col = floor((lon - 5.8) / 0.0125)` (grid steps from pilz_weather.json; row 0..1038, col 0..919). A cell that straddles a degree line is written to both chunks.

## stations-<stamp>.json

```json
{ "days": ["…14 dates…"], "stations": [ { "id": "at:11035", "name": "Wien Hohe Warte", "lat": 48.25, "lon": 16.36, "alt": 198, "r": [14 values mm or null] } ] }
```
