# schwammerl-data

Precomputed map data for the Schwammerl app: mushroom chance zones, rain and a tree map for Austria, Germany and
Switzerland. Built by GitHub Actions, served by GitHub Pages at <https://mrgatzi.github.io/schwammerl-data/>.
The app only downloads finished vector tiles (PMTiles, HTTP range requests) for what is on screen.

## What runs when

| Workflow | When | What |
|---|---|---|
| `daily.yml` | 07:30 and 13:30 UTC (shroomzn.app runs at 06:17 / 12:17 UTC and can start late), or by hand | Fetches the weather from shroomzn.app. If `pilz_weather.json` → `generated` equals the live `index.json` → `weatherGenerated`, the run stops (nothing deployed). Otherwise it builds chance zones for all 20 species, rain, tap cells and stations, writes `index.json` and deploys the site to Pages. Run by hand with `force` to publish anyway. |
| `trees.yml` | by hand, when the site grids or the Upper Austria polygons change | Builds a new `trees-<ver>.pmtiles`, then runs the daily publish with `force` so it goes live. Later daily runs keep serving it (they copy the live trees file). |

Each deploy replaces the whole site. It contains the new files plus the stamped files of the previous publish
(the app may still hold the previous `index.json` for a while); anything older is dropped. Before deploying, the job
checks that every file in `index.json` exists, no file is 95 MB or more, and the site is under 950 MB. If a check
fails, the run fails and the last good site stays live.

## Inputs

- From <https://shroomzn.app/data/> (public): `pilz_weather.json/.bin` (1 km weather planes), `rain_daily.json/.bin`
  (14 daily rain grids), `stations.json`, `ch_stations.json`, `de_stations.json`, `cz_stations.json`.
- Release [`habitat-grids`](https://github.com/MrGatzi/schwammerl-data/releases/tag/habitat-grids) of this repo:
  - `AT.npz`, `CH.npz`, `DE_S.npz`, `DE_M.npz`, `DE_N.npz`: shroomzn.app's 100 m site grids (EPSG:3035), copied
    with permission. They change about once a year.
  - `ooe_forest.fgb`: our 10 m forest outline of Upper Austria (chance zones are clipped to it there).
  - `ooe_trees.fgb`: the same forest split by main genus (tree map in Upper Austria).

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
| `pipeline/model.py` | the chance model (re-implementation of shroomzn.app's model on his 100 m grids) |
| `pipeline/zones.py` | score field → smooth chance zone polygons → PMTiles |
| `pipeline/rain.py` | rain isobands, tap cells, stations |
| `pipeline/trees.py` | tree map; `export-ooe` makes `ooe_trees.fgb` from the app repo's 10 m forest blocks |
| `pipeline/publish.py` | the daily entry point (all of the above + `index.json` + checks) |

See [pipeline/README.md](pipeline/README.md) for the parity test against the reference model (not run in CI: it
needs a checkout of the private reference repo).

## Rebuild by hand

- Publish now even if the weather is unchanged: Actions → `daily` → Run workflow → `force`.
- New tree map: Actions → `trees` → Run workflow.
- New grids: replace the assets in the `habitat-grids` release, then run `trees` (it also publishes the zones).
- Locally (needs tippecanoe ≥ 2.17 on `PATH`, grids in `grids/`, `ooe_forest.fgb` in `grids/` or `out/`):

      python -m venv .venv && .venv/bin/pip install -r pipeline/requirements.txt
      .venv/bin/python pipeline/trees.py build grids grids/ooe_trees.fgb site     # optional: new tree map
      .venv/bin/python pipeline/publish.py --site site --force

## Rain until `rain_daily` is live

`rain_daily.json/.bin` comes from [shroomzn PR #1](https://github.com/OutFoxD/shroomzn/pull/1). Until it is merged
and live (the URL returns 404), `publish.py`:

1. reuses the previous publish's rain tiles, tap cells and stations (and its `rainDays`), or
2. if nothing has been published yet, builds them from `--rain-src URL|DIR` (or the `RAIN_SRC` environment
   variable), a place with a `rain_daily.json/.bin` made by running his `build_rain_daily()` locally, or
3. fails with a clear message.

The chance zones are always fresh. While rain is reused, the tap cards also keep the soil temperature and moisture
of that older publish. Once the file is live, every run builds fresh rain without any change here.

## Data sources and licences

Rain: GeoSphere Austria (SPARTACUS, klima-v2-1d, TAWES, CC BY 4.0), MeteoSwiss (RprelimD, SwissMetNet), DWD (HYRAS-DE-PR, stations), ČHMÚ. Soil/sun: Open-Meteo (CC BY 4.0). Site grids by shroomzn.app from ForestPaths (CC BY 4.0), BFW Waldkarte (CC BY 4.0), LFI (BAFU/WSL), geology GeoSphere Austria (CC BY 4.0), swisstopo, BGR (GÜK250), Copernicus DEM GLO-30 (© DLR, Airbus), SoilGrids © ISRIC (CC BY 4.0). Finds: GBIF. Model and species weights: shroomzn.app (with permission).
