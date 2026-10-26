"""Rain view + tap data from shroomzn.app's daily rain grid (docs/format.md).

Writes into ``out_dir``:
  rain-<stamp>.pmtiles   layer ``rain``: filled isobands {p, mm, to} for the 1/3/7/14-day sums (z4-z10),
                         layer ``rain_lines``: isolines {p, mm} for labels (z6-z10)
  cells-<stamp>/         1 deg x 1 deg chunks <lon>_<lat>.json: per ~1 km cell 14 daily mm + soil_t0 + moist
  stations-<stamp>.json  AT/CH/DE/CZ stations with the same 14 days

The contours follow the app's ``src/data/contour.ts`` (marching squares + Chaikin x3, no-data masked), on the
full 1 km grid after a light no-data-aware Gaussian (stands in for the app's block averaging).

Usage:  python pipeline/rain.py OUT_DIR [--stamp YYYYMMDDHHmm] [--rain SRC] [--weather SRC] [--stations SRC]
A SRC is a base URL (default https://shroomzn.app/data) or a local directory with the same file names.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shroomzn_io as sio  # noqa: E402

PERIODS = (1, 3, 7, 14)
LEVELS = (0, 2, 5, 10, 15, 20, 30, 40, 60, 80, 150)   # = RAIN_RAMP_LIGHT keys in the app
TOP = 9999                                              # `to` of the last band
SMOOTH_SIGMA = 1.0          # cells; light pre-smoothing of the sums (nearest-neighbour resampled source grids)
CHAIKIN_PASSES = 3          # as contour.ts
SIMPLIFY_DEG = 0.0002       # ~20 m, after Chaikin (tippecanoe simplifies further per zoom)
MINZOOM, MAXZOOM, LINES_MINZOOM = 4, 10, 6
CELL_CHUNK_DEG = 1


# ----------------------------------------------------------------------------------- contours --

def period_sums(mm: np.ndarray) -> dict[int, np.ndarray]:
    """Sum of the most recent N days; NaN where any of them has no data."""
    return {p: mm[-p:].sum(axis=0) for p in PERIODS}


def smooth_valid(v: np.ndarray, sigma: float = SMOOTH_SIGMA) -> np.ndarray:
    """Gaussian over cells with data only (no-data stays NaN and does not pull values down)."""
    from scipy.ndimage import gaussian_filter
    ok = np.isfinite(v)
    if sigma <= 0:
        return v
    num = gaussian_filter(np.where(ok, v, 0).astype(np.float64), sigma, mode="nearest")
    den = gaussian_filter(ok.astype(np.float64), sigma, mode="nearest")
    return np.where(ok, num / np.maximum(den, 1e-9), np.nan)


def chaikin_closed(ring: np.ndarray, passes: int = CHAIKIN_PASSES) -> np.ndarray:
    p = ring[:-1] if len(ring) > 1 and np.array_equal(ring[0], ring[-1]) else ring
    for _ in range(passes):
        q = np.roll(p, -1, axis=0)
        p = np.stack([0.75 * p + 0.25 * q, 0.25 * p + 0.75 * q], axis=1).reshape(-1, 2)
    return np.vstack([p, p[:1]])


def chaikin_open(line: np.ndarray, passes: int = CHAIKIN_PASSES) -> np.ndarray:
    p = line
    for _ in range(passes):
        if len(p) <= 2:
            break
        a, b = p[:-1], p[1:]
        cut = np.stack([0.75 * a + 0.25 * b, 0.25 * a + 0.75 * b], axis=1).reshape(-1, 2)
        p = np.vstack([p[:1], cut[1:-1], p[-1:]])       # end points kept (as smoothOpen)
    return p


def _rnd(coords) -> list:
    return np.round(np.asarray(coords, dtype=np.float64), 5).tolist()


def _generator(field: np.ndarray):
    import contourpy
    lon, lat = sio.grid_centers()
    # contourpy wants ascending y: flip rows so lat runs south -> north
    z = np.ma.masked_invalid(field[::-1].astype(np.float64))
    return contourpy.contour_generator(lon, lat[::-1], z, name="serial", corner_mask=True,
                                       fill_type=contourpy.FillType.OuterOffset,
                                       line_type=contourpy.LineType.Separate)


def _polygons(g):
    if g.geom_type == "Polygon":
        return [g]
    return [p for part in getattr(g, "geoms", []) for p in _polygons(part)]


def band_features(field: np.ndarray, period: int):
    """Disjoint filled bands [LEVELS[i], LEVELS[i+1]) (the last one open to TOP), one feature per polygon."""
    from shapely.geometry import Polygon
    from shapely import set_precision
    from shapely.validation import make_valid
    gen = _generator(field)
    bounds = list(LEVELS) + [TOP]
    for i, lo in enumerate(LEVELS):
        hi = bounds[i + 1]
        # the lowest band has no lower edge (values are >= 0 anyway); the top band no upper edge
        points, offsets = gen.filled(-1.0 if i == 0 else float(lo), 1e9 if hi == TOP else float(hi))
        for pts, offs in zip(points, offsets):
            rings = [chaikin_closed(pts[offs[k]:offs[k + 1]]) for k in range(len(offs) - 1)]
            rings = [r for r in rings if len(r) >= 4]
            if not rings:
                continue
            geom = Polygon(rings[0], rings[1:]).simplify(SIMPLIFY_DEG, preserve_topology=True)
            # snap to the 1e-5 deg output grid first, so rounding can't create self-intersections
            geom = set_precision(geom, 1e-5)
            if not geom.is_valid:                 # Chaikin can make a hole touch its shell (rare)
                geom = make_valid(geom)
            for poly in _polygons(geom):
                if poly.is_empty or poly.area == 0:
                    continue
                coords = [_rnd(poly.exterior.coords)] + [_rnd(h.coords) for h in poly.interiors]
                yield {"type": "Feature", "properties": {"p": period, "mm": lo, "to": hi},
                       "geometry": {"type": "Polygon", "coordinates": coords}}


def line_features(field: np.ndarray, period: int):
    """Isolines at every level above 0; they end where the data ends (masked cells)."""
    from shapely.geometry import LineString
    gen = _generator(field)
    for lv in LEVELS[1:]:
        for seg in gen.lines(float(lv)):
            if len(seg) < 2:
                continue
            closed = len(seg) > 3 and np.allclose(seg[0], seg[-1])
            sm = chaikin_closed(seg) if closed else chaikin_open(seg)
            ls = LineString(sm).simplify(SIMPLIFY_DEG, preserve_topology=False)
            if ls.is_empty or ls.length < 0.01:  # drop specks under ~1 km, too short for a label
                continue
            yield {"type": "Feature", "properties": {"p": period, "mm": lv},
                   "tippecanoe": {"minzoom": LINES_MINZOOM},
                   "geometry": {"type": "LineString", "coordinates": _rnd(ls.coords)}}


def build_rain_tiles(mm: np.ndarray, out_path: str, work_dir: str) -> dict:
    sums = period_sums(mm)
    bands_p = os.path.join(work_dir, "rain_bands.geojsonl")
    lines_p = os.path.join(work_dir, "rain_lines.geojsonl")
    n_bands = n_lines = 0
    with open(bands_p, "w") as fb, open(lines_p, "w") as fl:
        for p, s in sums.items():
            field = smooth_valid(s)
            for f in band_features(field, p):
                fb.write(json.dumps(f, separators=(",", ":")) + "\n")
                n_bands += 1
            for f in line_features(field, p):
                fl.write(json.dumps(f, separators=(",", ":")) + "\n")
                n_lines += 1
    cmd = ["tippecanoe", "-o", out_path, "--force", "--quiet", f"-Z{MINZOOM}", f"-z{MAXZOOM}",
           "-L", json.dumps({"file": bands_p, "layer": "rain"}),
           "-L", json.dumps({"file": lines_p, "layer": "rain_lines"}),
           "--read-parallel", "--no-feature-limit", "--no-tile-size-limit",
           "--no-tiny-polygon-reduction",
           "--name", "rain", "--attribution", "Rain: GeoSphere Austria, MeteoSwiss, DWD via shroomzn.app"]
    subprocess.run(cmd, check=True)
    return {"bands": n_bands, "lines": n_lines,
            "geojson_mb": round((os.path.getsize(bands_p) + os.path.getsize(lines_p)) / 1e6, 1)}


# -------------------------------------------------------------------------------------- cells --

def _num(v: float, nd: int):
    """Compact JSON number: 1 decimal place etc., whole numbers without '.0', NaN -> None."""
    if not math.isfinite(v):
        return None
    r = round(float(v), nd)
    return int(r) if r == int(r) else r


def _app_row(lat: float) -> int:
    """Row the app computes for `lat` (docs/format.md)."""
    return math.floor((sio.GRID_NORTH - lat) / sio.GRID_DLAT)


def _app_col(lon: float) -> int:
    return math.floor((lon - sio.GRID_WEST) / sio.GRID_DLON)


def build_cells(days: list[str], mm: np.ndarray, planes: dict, out_dir: str) -> dict:
    """One file per 1 deg chunk. A chunk holds every cell the app can look up from a tap inside it
    (cells straddling a degree line are in both chunks)."""
    os.makedirs(out_dir, exist_ok=True)
    ny, nx = sio.GRID_NY, sio.GRID_NX
    st = planes.get("soil_t0", np.full((ny, nx), np.nan))
    mo = planes.get("moist", np.full((ny, nx), np.nan))
    have = np.isfinite(mm).any(axis=0) | np.isfinite(st) | np.isfinite(mo)
    files = cells = 0
    total = 0
    for lat0 in range(math.floor(sio.GRID_SOUTH), math.floor(sio.GRID_NORTH) + 1, CELL_CHUNK_DEG):
        r0 = max(0, _app_row(lat0 + CELL_CHUNK_DEG) if lat0 + CELL_CHUNK_DEG < sio.GRID_NORTH else 0)
        r1 = min(ny - 1, _app_row(lat0))
        if r0 > r1:
            continue
        for lon0 in range(math.floor(sio.GRID_WEST), math.floor(sio.GRID_EAST) + 1, CELL_CHUNK_DEG):
            c0 = max(0, _app_col(lon0))
            c1 = min(nx - 1, _app_col(lon0 + CELL_CHUNK_DEG))
            if c0 > c1:
                continue
            sub = have[r0:r1 + 1, c0:c1 + 1]
            if not sub.any():
                continue
            out = {}
            for dr, dc in zip(*np.nonzero(sub)):
                r, c = r0 + int(dr), c0 + int(dc)
                out[f"{r}_{c}"] = {"r": [_num(v, 1) for v in mm[:, r, c]], "st": _num(st[r, c], 1), "m": _num(mo[r, c], 3)}
            path = os.path.join(out_dir, f"{lon0}_{lat0}.json")
            with open(path, "w") as f:
                json.dump({"days": days, "cells": out}, f, separators=(",", ":"))
            files += 1
            cells += len(out)
            total += os.path.getsize(path)
    return {"files": files, "cells": cells, "mb": round(total / 1e6, 1)}


# ----------------------------------------------------------------------------------- stations --

def build_stations(docs: dict, days: list[str], out_path: str) -> dict:
    """The four country files condensed to `days` (normally the rain grid's 14 days, so dots and tap cards
    show the same days). Stations without any value in those days are left out."""
    stations = []
    for cc, doc in docs.items():
        idx = {d: i for i, d in enumerate(doc.get("dates", []))}
        for s in doc.get("stations", []):
            rr = s.get("rr") or []
            r = [(_num(rr[idx[d]], 1) if d in idx and idx[d] < len(rr) and rr[idx[d]] is not None else None) for d in days]
            if all(v is None for v in r):
                continue
            stations.append({"id": f"{cc}:{s['id']}", "name": s.get("name"), "lat": s.get("lat"), "lon": s.get("lon"),
                             "alt": s.get("alt"), "r": r})
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"days": days, "stations": stations}, f, ensure_ascii=False, separators=(",", ":"))
    by_cc = {}
    for s in stations:
        by_cc[s["id"].split(":")[0]] = by_cc.get(s["id"].split(":")[0], 0) + 1
    return {"stations": len(stations), "by_country": by_cc, "kb": round(os.path.getsize(out_path) / 1e3, 1)}


# -------------------------------------------------------------------------------------- entry --

def build_rain(rain_src: str = sio.SHROOMZN_DATA, weather_src: str = sio.SHROOMZN_DATA,
               stations_src: str = sio.SHROOMZN_DATA, out_dir: str = "out", stamp: str | None = None) -> dict:
    """Builds rain-<stamp>.pmtiles, cells-<stamp>/ and stations-<stamp>.json in out_dir.
    Returns the paths (relative to out_dir), the rain days and what was built, for index.json."""
    stamp = stamp or dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M")
    os.makedirs(out_dir, exist_ok=True)
    report = {"stamp": stamp}
    t = time.time()
    rain_meta, days, mm = sio.read_rain_daily(rain_src)
    w_meta, planes = sio.read_pilz_weather(weather_src)
    st_docs = sio.read_station_files(stations_src)
    report["read_s"] = round(time.time() - t, 1)
    report["rainDays"] = [days[0], days[-1]]
    report["days"] = days
    report["rainGenerated"] = rain_meta.get("generated")
    report["weatherGenerated"] = w_meta.get("generated")

    rain_file = f"rain-{stamp}.pmtiles"
    t = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        report["tiles"] = build_rain_tiles(mm, os.path.join(out_dir, rain_file), tmp)
    report["tiles"].update(file=rain_file, mb=round(os.path.getsize(os.path.join(out_dir, rain_file)) / 1e6, 2),
                           s=round(time.time() - t, 1))

    cells_dir = f"cells-{stamp}"
    t = time.time()
    if os.path.isdir(os.path.join(out_dir, cells_dir)):
        shutil.rmtree(os.path.join(out_dir, cells_dir))
    report["cells"] = {"dir": cells_dir, **build_cells(days, mm, planes, os.path.join(out_dir, cells_dir)),
                       "s": round(time.time() - t, 1)}

    st_file = f"stations-{stamp}.json"
    report["stations"] = {"file": st_file, **build_stations(st_docs, days, os.path.join(out_dir, st_file))}

    # the index.json "rain" entry (docs/format.md)
    report["index"] = {"rainDays": report["rainDays"],
                       "rain": {"file": rain_file, "periods": list(PERIODS), "levels": list(LEVELS)},
                       "cells": {"dir": cells_dir, "step": CELL_CHUNK_DEG}, "stations": {"file": st_file}}
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_dir")
    ap.add_argument("--stamp")
    ap.add_argument("--rain", default=sio.SHROOMZN_DATA, help="source of rain_daily.json/.bin")
    ap.add_argument("--weather", default=sio.SHROOMZN_DATA, help="source of pilz_weather.json/.bin")
    ap.add_argument("--stations", default=sio.SHROOMZN_DATA, help="source of the four station files")
    a = ap.parse_args()
    t = time.time()
    rep = build_rain(a.rain, a.weather, a.stations, a.out_dir, a.stamp)
    rep.pop("days")
    rep["total_s"] = round(time.time() - t, 1)
    print(json.dumps(rep, indent=1))


if __name__ == "__main__":
    main()
