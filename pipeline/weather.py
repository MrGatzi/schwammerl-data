"""Our own copy of shroomzn.app's weather files, computed with his code (pipeline/vendor/shroomzn_precompute.py).

shroomzn.app/data answers 403 to GitHub Actions runners, so the daily workflow builds the files publish.py reads
itself, with the parts of his `precompute.py full` that produce them (same order, same logging, same meta.json):

  stations.json, ch_stations.json,     daily station rain, AT / CH / DE / CZ (+ the common "through" date
  de_stations.json, cz_stations.json   and regions.json, as his run writes them)
  rain_daily.json/.bin                 14 days of daily rain on the ~1 km weather grid (AT + CH + DE grids)
  pilz_weather.json/.bin               rain_lag, soil_t0, moist, rain7, sun7, air7 planes (Open-Meteo soil)
  meta.json                            what each part produced, and any errors

Left out (nothing here reads them): the history archive (hist_*.json, CHMI backfill), the overlay PNGs/isolines
(render_grid) and the last-24-hours files (de_now, cz_now).

usage:
  python pipeline/weather.py OUT_DIR [--prev DIR]

--prev: a previous OUT_DIR. pilz_weather keeps a country's rain from it when that country's grid fails this run,
and the Swiss stations fill gaps from it; without it a failed grid leaves that country without rain.
Exit code 1 when pilz_weather.json was not written (publish.py then cannot use OUT_DIR); a failed part otherwise
only shows in meta.json and on stderr.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import shroomzn_precompute as pc  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_dir")
    ap.add_argument("--prev", help="previous OUT_DIR (fallbacks for failed parts)")
    a = ap.parse_args(argv)
    out_dir = os.path.abspath(a.out_dir)
    prev_dir = os.path.abspath(a.prev) if a.prev and os.path.isdir(a.prev) else None
    if prev_dir == out_dir:
        ap.error("--prev must be another directory than OUT_DIR")
    os.makedirs(out_dir, exist_ok=True)
    for name in ("pilz_weather.json", "rain_daily.json", "meta.json", *pc.STATION_FILES.values()):
        if os.path.exists(os.path.join(out_dir, name)):
            os.remove(os.path.join(out_dir, name))   # a stale file must not pass as this run's result
    pw = os.path.join(out_dir, "pilz_weather.json")
    t0 = time.time()
    report = {"ran": pc.now_iso(), "mode": "full", "by": "schwammerl-data pipeline/weather.py",
              "prev": bool(prev_dir)}

    for name, fn in (("stations", pc.build_stations), ("ch_stations", pc.build_ch_stations),
                     ("de_stations", pc.build_de_stations), ("cz_stations", pc.build_cz_stations)):
        pc.run(report, name, fn, out_dir, prev_dir)
    pc.run(report, "alignment", pc.align_station_files, out_dir, prev_dir)

    grids = {}
    for c, fn in (("AT", pc.fetch_at_grid), ("CH", pc.fetch_ch_grid), ("DE", pc.fetch_de_grid)):
        try:
            grids[c] = fn()
            report[f"fetch_{c}_grid"] = {"last": grids[c]["last"].isoformat(), "days": len(grids[c]["days"])}
        except Exception as e:
            report[f"fetch_{c}_grid"] = {"error": f"{type(e).__name__}: {e}"}
        print(f"fetch_{c}_grid: {report[f'fetch_{c}_grid']}")
    if grids:
        common = min(g["last"] for g in grids.values())
        report["grid_common_last_day"] = common.isoformat()
        pc.run(report, "rain_daily", pc.build_rain_daily, grids, common, out_dir)
    pc.run(report, "pilz_weather", pc.build_pilz_weather, grids.get("AT"), out_dir, grids.get("CH"), grids.get("DE"),
           prev_dir)

    report["total_s"] = round(time.time() - t0, 1)
    pc.write_json(out_dir, "meta.json", report, compact=False)
    print(f"weather: {report['total_s']} s")
    if not os.path.exists(pw):
        print("weather: pilz_weather.json was not written", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
