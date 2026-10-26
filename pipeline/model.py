"""Mushroom chance model on the 100 m site grids (numpy port of the shroomzn.app Pilz-Chance model).

Per forest cell: site (host trees x soil x terrain) x season x soil-temperature window x wetness
(rain + soil moisture), then each cell averaged with its forest neighbours (3 x 3). The arithmetic
follows the reference model term by term and in the same operand order, so the float results agree
with it to the last bits (see pipeline/tests/test_parity.py).

Inputs:
  grids/<REGION>.npz   uint8 class grids at 100 m, EPSG:3035, row 0 = north (forest 0 = none, 1..15)
  pilz_weather.json/.bin  ~1 km weather planes (zlib uint8, value = byte / scale - offset, 255 = none)
  pipeline/species.json   species parameters (cover() evaluated for classes 0..15)

CLI:
  python pipeline/model.py --region AT --species steinpilz --out work/fixtures/AT-steinpilz.npz
  python pipeline/model.py --bench            # all species x all regions, timings
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import json
import math
import time
import urllib.request
import zlib
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
GRIDS = ROOT / "grids"
WORK = ROOT / "work"
CACHE = WORK / "cache"
SPECIES_JSON = Path(__file__).resolve().parent / "species.json"
WEATHER_URL = "https://shroomzn.app/data/"

# order matters: where grids overlap, the first region with forest at a cell wins
REGIONS = ["AT", "CH", "DE_S", "DE_M", "DE_N"]
# AT's npz carries no grid vector: x0, y_top, res, width, height of its box
AT_GRID = (4_200_000, 2_940_000, 100, 7000, 4000)

GENERA = ["larix", "picea", "pinus", "fagus", "quercus", "conifer", "broadleaf"]
SHARE_W = [0.45, 0.57, 0.75, 0.92]
SLOPE_MID = [1.5, 5.5, 11.5, 18.5, 26, 35, 45]
STREAM_DM = [0, 0.002, 0.004, 0.008]
LEVELS = [0.2, 0.45, 0.7]          # Möglich, Gut, Top (level 0 = Kaum, below 0.2)
LEVEL_NAMES = ["Kaum", "Möglich", "Gut", "Top"]
SITE_KEYS = ("g1", "g2", "share", "forest", "slope", "elev15", "aspect", "wet", "rock", "ph", "texture", "stream")


# ------------------------------------------------------------------ inputs
def load_species(path: Path = SPECIES_JSON) -> dict:
    return json.loads(Path(path).read_text())


def fetch_weather(dst: Path = WORK / "wx") -> Path:
    dst.mkdir(parents=True, exist_ok=True)
    for name in ("pilz_weather.json", "pilz_weather.bin"):
        with urllib.request.urlopen(WEATHER_URL + name, timeout=60) as r:
            (dst / name).write_bytes(r.read())
    return dst / "pilz_weather.json"


class Weather:
    """Decoded pilz_weather planes (float64, NaN = no data), grid geometry as the reference uses it."""

    def __init__(self, json_path: Path):
        json_path = Path(json_path)
        self.doc = doc = json.loads(json_path.read_text())
        raw = np.frombuffer(zlib.decompress((json_path.parent / doc["file"]).read_bytes()), np.uint8)
        self.nx, self.ny = int(doc["nx"]), int(doc["ny"])
        n = self.nx * self.ny
        self.bytes = raw.reshape(-1, n)                       # raw uint8 planes, in the order of doc["planes"]
        self.planes: dict[str, np.ndarray] = {}
        for i, p in enumerate(doc["planes"]):
            b = raw[i * n:(i + 1) * n]
            v = b / float(p["scale"]) - float(p["offset"])          # byte / scale - offset (doubles, as in JS)
            self.planes[p["name"]] = np.where(b == 255, np.nan, v)
        (s, w), (nb, e) = doc["bounds"]
        self.s, self.w, self.n, self.e = s, w, nb, e
        self.dlon = (e - w) / self.nx
        self.dlat = (nb - s) / self.ny
        self.at_own_height = bool(doc.get("soil_at_own_height"))
        self.generated = doc.get("generated")
        lag, t0 = self.planes.get("rain_lag"), self.planes.get("soil_t0")
        self.valid = ~np.isnan(lag) & ~np.isnan(t0)               # cell() returns null without lag or soil t0

    def plane(self, name: str) -> np.ndarray:
        p = self.planes.get(name)
        return p if p is not None else np.full(self.nx * self.ny, np.nan)

    def key(self) -> dict:
        return {"bounds": self.doc["bounds"], "nx": self.nx, "ny": self.ny}

    def index(self, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
        """flat index of the ~1 km cell containing lat/lon (pilzWeatherAt), -1 outside"""
        i = np.floor((self.n - lat) / self.dlat)
        j = np.floor((lon - self.w) / self.dlon)
        ok = (i >= 0) & (j >= 0) & (i < self.ny) & (j < self.nx)
        k = np.where(ok, i * self.nx + j, -1)
        return k.astype(np.int64)


def load_grid(region: str | Path) -> tuple[str, dict, tuple]:
    path = Path(region) if str(region).endswith(".npz") else GRIDS / f"{region}.npz"
    d = np.load(path)
    reg = str(d["region"]) if "region" in d.files else path.stem
    g = tuple(int(v) for v in d["grid"]) if "grid" in d.files else AT_GRID
    f = {k: d[k] for k in SITE_KEYS}
    return reg, f, g


def grid_of(region: str) -> tuple:
    d = np.load(GRIDS / f"{region}.npz")
    return tuple(int(v) for v in d["grid"]) if "grid" in d.files else AT_GRID


# ------------------------------------------------------------------ overlap (DE bands, borders)
def owned_mask(region: str, forest: np.ndarray | None = None, grid: tuple | None = None,
               order: list[str] = REGIONS) -> np.ndarray:
    """bool (ny x nx): forest cells of `region` that no earlier region in `order` covers with forest.

    Same rule as the reference tiles step ("first one with forest wins where grids overlap"). All grids
    are 100 m on the same EPSG:3035 lattice, so cells line up exactly."""
    if forest is None or grid is None:
        _, f, grid = load_grid(region)
        forest = f["forest"]
    own = forest > 0
    x0, y1, res, w, h = grid
    for other in order[:order.index(region)]:
        ox0, oy1, ores, ow, oh = grid_of(other)
        assert ores == res and (ox0 - x0) % res == 0 and (y1 - oy1) % res == 0
        c0, r0 = (ox0 - x0) // res, (y1 - oy1) // res      # other's (0,0) in our cells
        a_r0, a_r1 = max(0, r0), min(h, r0 + oh)
        a_c0, a_c1 = max(0, c0), min(w, c0 + ow)
        if a_r0 >= a_r1 or a_c0 >= a_c1:
            continue
        of = np.load(GRIDS / f"{other}.npz")["forest"]
        sub = of[a_r0 - r0:a_r1 - r0, a_c0 - c0:a_c1 - c0] > 0
        own[a_r0:a_r1, a_c0:a_c1] &= ~sub
    return own


# ------------------------------------------------------------------ species tables (pilzCompile)
def irradiance(aspect: int, slope: int) -> float:
    if not aspect:
        return 1.0
    a, s = (aspect - 1) * 45 * math.pi / 180, SLOPE_MID[slope] * math.pi / 180
    return 1 - 0.55 * math.sin(s) * math.cos(a)


IRR_T = np.array([irradiance(a, sl) for a in range(9) for sl in range(7)])    # index aspect * 7 + slope


def season(sp: dict, date: dt.date) -> float:
    """pilzSeason: monthly values at mid-month, linear in between (local calendar date)"""
    m, d = date.month - 1, date.day
    days = calendar.monthrange(date.year, date.month)[1]
    t = (d - 15) / days
    a = sp["season"][m]
    b = sp["season"][(m + 1) % 12] if t >= 0 else sp["season"][(m + 11) % 12]
    return a + (b - a) * abs(t)


def compile_species(sp: dict) -> dict:
    tf = [sp["trees"].get(g, 0) for g in GENERA]
    fitT = np.array([SHARE_W[h] * tf[a] + (1 - SHARE_W[h]) * tf[b]
                     for a in range(7) for b in range(7) for h in range(4)])
    soilT = np.array([sp["ph"][p] * sp["rock"][r] * sp["tex"][t]
                      for p in range(8) for r in range(8) for t in range(4)])
    placeT = np.array([sp["slope"][sl] * sp["wet"][w] * sp["stream"][st] * sp["cover"][f]
                       for f in range(16) for st in range(4) for w in range(8) for sl in range(7)])
    return {"fitT": fitT, "soilT": soilT, "placeT": placeT, "lo": sp["t"][0], "hi": sp["t"][1],
            "norm": sp["norm"], "rain": sp["rain"], "peak": sp.get("peak") or 1}


def clamp(x, a=0.0, b=1.0):
    return np.maximum(a, np.minimum(b, x))


# ------------------------------------------------------------------ region precompute (species independent)
class Region:
    """Everything about a region's forest cells that doesn't depend on the species."""

    def __init__(self, region: str | Path, weather: Weather, cache: Path = CACHE):
        t = time.time()
        self.region, f, self.grid = load_grid(region)
        x0, y1, res, w, h = self.grid
        self.shape = (h, w)
        self.forest = f["forest"]
        flat = np.flatnonzero(self.forest.ravel() > 0)
        widx = self._widx(flat, weather, cache)
        ok = widx >= 0
        ok[ok] = weather.valid[widx[ok]]
        self.cells = cells = flat[ok]            # flat grid indices of the scored cells (forest + weather)
        self.widx = wk = widx[ok]
        g = {k: f[k].ravel()[cells].astype(np.int32) for k in SITE_KEYS}
        del f
        self.fit_i = (g["g1"] * 7 + g["g2"]) * 4 + g["share"]
        self.soil_i = (g["ph"] * 8 + g["rock"]) * 4 + g["texture"]
        self.place_i = ((g["forest"] * 4 + g["stream"]) * 8 + g["wet"]) * 7 + g["slope"]

        # weather + terrain terms (pilzRender, in its operand order)
        sun7 = weather.plane("sun7")[wk]
        sunK = np.where(np.isnan(sun7), 0.7, clamp(sun7 / 30, 0.25, 1.3))
        I = IRR_T[g["aspect"] * 7 + g["slope"]]
        dT = 3.5 * (I - 1) * sunK
        t0 = weather.plane("soil_t0")[wk]
        elev = g["elev15"] * 15
        self.soilT = ((t0 if weather.at_own_height else t0 - 0.005 * elev) + dT)
        dM = -0.25 * (I - 1) + (g["wet"] - 3.5) * 0.005 + np.array(STREAM_DM)[g["stream"]]
        moist = weather.plane("moist")[wk]
        self.moist_nan = np.isnan(moist)
        self.moistF = clamp((moist + dM - 0.18) / (0.33 - 0.18))     # NaN where no moisture (-> rainF)
        self.lag = weather.plane("rain_lag")[wk]

        # 3 x 3 forest-neighbour index (into the compact cell array; N = "no cell" -> 0)
        n = len(cells)
        pos = np.full(h * w, n, np.int32)
        pos[cells] = np.arange(n, dtype=np.int32)
        r, c = cells // w, cells % w
        self.nb = np.empty((9, n), np.int32)
        j = 0
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                rr, cc = r + dr, c + dc
                inside = (rr >= 0) & (rr < h) & (cc >= 0) & (cc < w)
                self.nb[j] = np.where(inside, pos[np.clip(rr, 0, h - 1) * w + np.clip(cc, 0, w - 1)], n)
                j += 1
        del pos, r, c, rr, cc, inside
        self.cnt = (self.nb < n).sum(axis=0).astype(np.float64)
        self.seconds = time.time() - t

    def _widx(self, flat: np.ndarray, weather: Weather, cache: Path) -> np.ndarray:
        """weather cell per forest cell (row-major order of forest > 0), cached per region"""
        npy, meta = cache / f"{self.region}-widx.npy", cache / f"{self.region}-widx.json"
        key = {**weather.key(), "grid": list(self.grid), "cells": int(len(flat))}
        if npy.exists() and meta.exists() and json.loads(meta.read_text()) == key:
            return np.load(npy).astype(np.int64)
        import pyproj
        x0, y1, res, w, h = self.grid
        x = x0 + (flat % w + 0.5) * res
        y = y1 - (flat // w + 0.5) * res
        tr = pyproj.Transformer.from_crs("EPSG:3035", "EPSG:4326", always_xy=True)
        lon, lat = tr.transform(x, y)
        widx = weather.index(np.asarray(lat), np.asarray(lon))
        cache.mkdir(parents=True, exist_ok=True)
        np.save(npy, widx.astype(np.int32))
        meta.write_text(json.dumps(key))
        return widx

    # -------------------------------------------------------------- per species
    def raw(self, sp: dict, date: dt.date, dtype=np.float32) -> np.ndarray:
        """per-cell score before the neighbour mean, one value per self.cells; float32 like pilzRender's
        raw[] (float64 = pilzScore's own double)"""
        T, s = compile_species(sp), season(sp, date)
        prod = T["fitT"][self.fit_i] * T["soilT"][self.soil_i] * T["placeT"][self.place_i]
        site = np.minimum(1, prod / T["norm"])
        st, lo, hi = self.soilT, T["lo"], T["hi"]
        zt = np.where(st < lo, (lo - st) / 3, np.where(st > hi, (st - hi) / 3, 0.0))
        temp = np.exp(-(zt ** 2))
        rainF = clamp((self.lag - 5) / (T["rain"] - 5))
        moistF = np.where(self.moist_nan, rainF, self.moistF)
        wetness = 0.55 * rainF + 0.45 * moistF
        return clamp(site * s * temp * wetness * T["peak"]).astype(dtype)

    def smooth(self, raw: np.ndarray) -> np.ndarray:
        """3 x 3 mean over scored neighbours (pilzRender: sum in double, row by row, then / count)"""
        v = np.append(raw.astype(np.float64), 0.0)
        acc = np.zeros(len(raw))
        for j in range(9):
            acc = acc + v[self.nb[j]]
        return (acc / self.cnt).astype(np.float32)

    def to_grid(self, values: np.ndarray) -> np.ndarray:
        out = np.full(self.shape[0] * self.shape[1], np.nan, np.float32)
        out[self.cells] = values
        return out.reshape(self.shape)

    def score(self, sp: dict, date: dt.date, smooth: bool = True) -> np.ndarray:
        r = self.raw(sp, date)
        return self.to_grid(self.smooth(r) if smooth else r)


# ------------------------------------------------------------------ API
_species_cache: dict | None = None


def score_region(npz_path: str | Path, weather: Weather, species_id: str, date: dt.date,
                 region: Region | None = None) -> np.ndarray:
    """float32 (ny x nx) chance after the 3 x 3 forest-neighbour mean, NaN where no forest / no weather.
    Pass a prebuilt `region` (Region(npz_path, weather)) to score many species without redoing the
    species-independent part."""
    global _species_cache
    if _species_cache is None:
        _species_cache = load_species()
    reg = region or Region(npz_path, weather)
    return reg.score(_species_cache[species_id], date)


def levels(score: np.ndarray) -> np.ndarray:
    """uint8: 0 Kaum (< 0.2, also NaN), 1 Möglich (>= 0.2), 2 Gut (>= 0.45), 3 Top (>= 0.7)"""
    s = np.nan_to_num(score, nan=-1)
    return ((s >= LEVELS[0]).astype(np.uint8) + (s >= LEVELS[1]) + (s >= LEVELS[2])).astype(np.uint8)


def today_vienna() -> dt.date:
    from zoneinfo import ZoneInfo
    return dt.datetime.now(ZoneInfo("Europe/Vienna")).date()


def _weather(path: str | None) -> Weather:
    p = Path(path) if path else WORK / "wx" / "pilz_weather.json"
    if not p.exists():
        p = fetch_weather(p.parent)
    return Weather(p)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", default="AT")
    ap.add_argument("--species", default="steinpilz")
    ap.add_argument("--date", help="YYYY-MM-DD (default: today in Vienna)")
    ap.add_argument("--weather", help="path to pilz_weather.json (default: work/wx, downloaded if missing)")
    ap.add_argument("--out", help="write score/grid/forest npz here")
    ap.add_argument("--bench", action="store_true", help="all species x all regions, print timings")
    a = ap.parse_args()
    date = dt.date.fromisoformat(a.date) if a.date else today_vienna()
    w = _weather(a.weather)
    species = load_species()

    if a.bench:
        import resource
        t_all = time.time()
        for r in REGIONS:
            t = time.time()
            reg = Region(r, w)
            t_pre = time.time() - t
            hist = np.zeros(4, np.int64)
            for sid, sp in species.items():
                lv = levels(reg.smooth(reg.raw(sp, date)))
                hist += np.bincount(lv, minlength=4)
            print(f"{r}: {len(reg.cells)} cells, precompute {t_pre:.1f}s, 20 species {time.time() - t - t_pre:.1f}s, "
                  f"levels {hist.tolist()}", flush=True)
            del reg
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        rss_mb = rss / 1e6 if rss > 1e7 else rss / 1e3          # macOS: bytes, Linux: KiB
        print(f"total {time.time() - t_all:.1f}s, peak RSS {rss_mb:.0f} MB")
        return

    t = time.time()
    reg = Region(a.region, w)
    score = reg.score(species[a.species], date)
    print(f"{reg.region} {a.species} {date}: {len(reg.cells)} cells, {time.time() - t:.1f}s, "
          f"levels {np.bincount(levels(score)[~np.isnan(score)], minlength=4).tolist()}")
    if a.out:
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(out, score=score, grid=np.array(reg.grid, np.int64), forest=reg.forest,
                            region=np.array(reg.region), species=np.array(a.species), date=np.array(str(date)),
                            weather_generated=np.array(str(w.generated)))
        print("wrote", out)


if __name__ == "__main__":
    main()
