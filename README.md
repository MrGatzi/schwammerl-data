# schwammerl-data

Precomputed map data for the Schwammerl app: mushroom chance zones, rain and a tree map for Austria, Germany and
Switzerland. Built by GitHub Actions, served from the Cloudflare R2 bucket `schwammerl-data` at
<https://pub-568205bcc18d4c72a0f8da2da3feebf3.r2.dev/> (public `r2.dev` URL, rate-limited).
The app only downloads finished vector tiles (PMTiles, HTTP range requests) for what is on screen.

## What runs when

| Workflow | When | What |
|---|---|---|
| `daily.yml` | 07:30 and 13:30 UTC, or by hand | Computes the weather with shroomzn.app's code (see [Weather](#weather)), builds chance zones for all 20 species, rain, tap cells and stations, writes `index.json` and uploads to R2. If `pilz_weather.json` → `generated` equals the live `index.json` → `weatherGenerated` (only possible when it fell back to shroomzn.app's files), the run stops without uploading; `force` publishes anyway. Scheduled runs also keep the schedule alive (see below). |
| `trees.yml` | by hand, when the site grids or the Upper Austria polygons change | Builds a new `trees-<ver>.pmtiles`, then runs the daily publish with `force` so it goes live. Later daily runs keep referencing the live trees file. |

A publish uploads only its new stamped files (~0.8 GB), then `index.json` last (`Cache-Control:
no-cache`; stamped files are `immutable`). Files it reuses (trees, rain while `rain_daily` is missing) are referenced,
not copied. Then it prunes: every object that neither the new nor the previous `index.json` references is deleted
(the app may still hold the previous index for a while). Before uploading it checks that every file in `index.json`
was built or is already in the bucket, no file is 500 MB or more, the upload is under 3 GB and the bucket stays under
8 GB (free tier: 10 GB, 1M writes/month). If a check or an upload fails, the run fails, `index.json` is not replaced,
nothing is deleted and the last good publish stays live.

GitHub turns off schedules after 60 days without a commit. The `keepalive` job of scheduled `daily` runs re-enables
the workflow through the API (`actions: write`), which resets that timer without commits.

Secrets: `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` (R2 API token, object read & write on the bucket). Account id,
bucket and public URL are plain `env` in `daily.yml`.

## Inputs

- Weather, computed in the workflow by `pipeline/weather.py` (shroomzn.app's code, see [Weather](#weather)) into
  `wx/`: `pilz_weather.json/.bin` (1 km weather planes), `rain_daily.json/.bin` (14 daily rain grids),
  `stations.json`, `ch_stations.json`, `de_stations.json`, `cz_stations.json`. The same files as on
  <https://shroomzn.app/data/>, which `publish.py` falls back to.
- Release [`habitat-grids`](https://github.com/MrGatzi/schwammerl-data/releases/tag/habitat-grids) of this repo:
  - `AT.npz`, `CH.npz`, `DE_S.npz`, `DE_M.npz`, `DE_N.npz`: shroomzn.app's 100 m site grids (EPSG:3035), copied
    with permission. They change about once a year.
  - `ooe_forest.fgb`: our 10 m forest outline of Upper Austria (chance zones are clipped to it there).
  - `ooe_trees.fgb`: the same forest split by main genus (tree map in Upper Austria).
  - `rain_daily.json`, `rain_daily.bin` (optional): a local run of his `build_rain_daily()`, a last fallback for a
    first publish without rain (see below).

  The workflow caches them; the cache key changes when an asset is replaced.

## Outputs

The format is the contract with the app: [docs/format.md](docs/format.md).

| File | Content |
|---|---|
| `index.json` | manifest: stamps, rain days, file names |
| `chance/<species>-<stamp>.pmtiles` | layer `chance`, polygons with `lv` 1 Möglich / 2 Gut / 3 Top, z5–z12 |
| `rain-<stamp>.pmtiles` | layers `rain` (bands for the 1/3/7/14-day sums) and `rain_lines` |
| `trees-<ver>.pmtiles` | layer `trees`, polygons with `g` = main genus, z7–z11 |
| `cells-<stamp>/<lon>_<lat>.json` | tap cards: 14 days of rain, soil temperature, moisture per ~1 km cell |
| `stations-<stamp>.json` | weather stations with 14 days of rain |

## Pipeline

| Script | Does |
|---|---|
| `pipeline/weather.py` | the weather files (runs his code in `pipeline/vendor/`) |
| `pipeline/model.py` | the chance model (re-implementation of shroomzn.app's model on his 100 m grids) |
| `pipeline/zones.py` | score field → smooth chance zone polygons → PMTiles |
| `pipeline/rain.py` | rain isobands, tap cells, stations |
| `pipeline/trees.py` | tree map; `export-ooe` makes `ooe_trees.fgb` from the app repo's 10 m forest blocks |
| `pipeline/publish.py` | the daily entry point (all of the above + `index.json` + checks + upload) |
| `pipeline/r2.py` | R2: what to upload, size guards, upload (index last), prune |

See [pipeline/README.md](pipeline/README.md) for the tests (the parity test against the reference model is not run
in CI: it needs a checkout of the private reference repo).

## Rebuild by hand

- Publish now even if the weather is unchanged: Actions → `daily` → Run workflow → `force`.
- New tree map: Actions → `trees` → Run workflow.
- New grids: replace the assets in the `habitat-grids` release, then run `trees` (it also publishes the zones).
- Locally (needs tippecanoe ≥ 2.17 on `PATH`, grids in `grids/`, `ooe_forest.fgb` in `grids/` or `out/`):

      python -m venv .venv && .venv/bin/pip install -r pipeline/requirements.txt
      .venv/bin/python pipeline/weather.py wx                                     # ~15-25 min
      .venv/bin/python pipeline/trees.py build grids grids/ooe_trees.fgb site     # optional: new tree map
      .venv/bin/python pipeline/publish.py --site site --src wx --force   # dry run: builds + checks, no upload

  Without `--upload` nothing changes online (with `R2_*` credentials set it checks against the bucket listing,
  otherwise against the public URL). With `--upload` and the credentials it publishes like the workflow; don't run
  it while a workflow publish is running.

## Weather

shroomzn.app's own pipeline (`precompute.py`, by OutFoxD) computes the weather its odds use, and publishes it on
<https://shroomzn.app/data/>. That host answers 403 to GitHub Actions runners, so the workflow runs his code itself,
with his permission: `pipeline/vendor/shroomzn_precompute.py` is a copy of his `pipeline/precompute.py` (commit and
the two changed lines in its header; border files in `pipeline/vendor/geo/`). Don't edit it; to update, copy his
file again and re-apply the marked lines.

`pipeline/weather.py OUT [--prev DIR]` calls only the parts of his `full` run whose files we read, in his order:
the four station files (+ the common "through" date), the AT / CH / DE daily rain grids, `rain_daily` and
`pilz_weather` (rain from the grids, soil temperature and moisture from Open-Meteo, sunshine and air temperature
from SPARTACUS), plus `meta.json` (what each part did). Left out: his history archive (CHMI backfill), the overlay
images and the last-24-hours files. It takes ~15-25 minutes, most of it waiting for Open-Meteo's free-tier rate
limit (~3300 points). It needs no secrets.

In the workflow:

1. `Restore previous weather` brings back the last successful run's output as `wx-prev/` (Actions cache). A
   country whose rain grid fails this run keeps its rain from there (his own fallback).
2. `Compute weather` writes `wx/` (`continue-on-error`). It fails only when `pilz_weather.json` was not written; a
   failed part otherwise shows in `meta.json` and the log.
3. `Publish` uses `--src wx` when `wx/pilz_weather.json` exists, else shroomzn.app/data (likely 403 on runners, so
   the run fails and the last good publish stays live).

Our weather always has a new `generated`, so every run publishes (twice a day). Comparing contents would not save
publishes: the soil planes are 7-day / 72-hour means up to the current hour and change between runs.

Rain, tap cells and stations come from `wx/` too. Without a `rain_daily` there, `publish.py`:

1. references the previous publish's rain tiles, tap cells and stations (and its `rainDays`; they stay in the
   bucket), or
2. if nothing has been published yet, builds them from `--rain-src URL|DIR` (or the `RAIN_SRC` environment
   variable), a place with a `rain_daily.json/.bin`. The workflow passes `--rain-src grids` when the
   `habitat-grids` release has these two files, or
3. fails with a clear message.

While rain is reused, the tap cards also keep the soil temperature and moisture of that older publish.

## Data sources and attribution

Weather pipeline: shroomzn.app by OutFoxD, used with permission.

- Rain (grids and stations), sunshine, air temperature, Austria: GeoSphere Austria (SPARTACUS v3/v2, klima-v2-1d),
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
- Rain, Switzerland (RprelimD / RhiresD, SwissMetNet): Source: MeteoSwiss.
- Rain, Germany (HYRAS-DE-PR, daily stations): Source: Deutscher Wetterdienst.
- Rain stations, Czechia (daily SRA): Source: ČHMÚ (Czech Hydrometeorological Institute).
- Soil temperature, soil moisture, sunshine outside Austria: [Open-Meteo](https://open-meteo.com/),
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); the free API is for non-commercial use.

Site grids by shroomzn.app from ForestPaths (CC BY 4.0), BFW Waldkarte (CC BY 4.0), LFI (BAFU/WSL), geology GeoSphere Austria (CC BY 4.0), swisstopo, BGR (GÜK250), Copernicus DEM GLO-30 (© DLR, Airbus), SoilGrids © ISRIC (CC BY 4.0). Finds: GBIF. Model and species weights: shroomzn.app (with permission).
