"""Chance zones: 100 m score field (W1, 3x3 forest-neighbour mean) -> smooth disjoint band polygons -> PMTiles.

Pipeline per region (all geometry work in EPSG:3035, output EPSG:4326):
  1. score NaN outside forest is filled from the nearest forest cell <= FILL_M (so bilinear edges don't dip
     towards 0 and our finer OÖ forest gets a score where his 100 m mask has none); beyond that -> no zone
  2. bilinear upsample x UP (25 m), marching squares (contourpy) at 0.2 / 0.45 / 0.7 -> nested areas
  3. forest outline: his forest mask (forest > 0) upsampled and contoured at 0.5 the same way; inside
     Upper Austria our 10 m forest polygons (out/ooe_forest.fgb) instead
  4. simplify ~10 m + Chaikin, nested areas & forest -> disjoint bands lv 1/2/3, reproject, geojsonseq
  5. tippecanoe -> PMTiles, layer "chance", prop lv (docs/format.md), z5-z13

The region is cut into BLOCK x BLOCK cell blocks (processed in parallel, each with a margin, clipped back to
its core). Regions are combined by writing one geojsonseq each and passing them all to to_pmtiles().

usage:
  python -I pipeline/zones.py ooe-forest SRC_DIR BOUNDARY.geojson OUT.fgb   our 10 m forest -> FlatGeobuf (once)
  python -I pipeline/zones.py standin GRID.npz OUT.npz                      smooth stand-in score (dev)
  python -I pipeline/zones.py build SCORE.npz REGION OUT.geojsonseq [--ooe FGB] [--forest-out F.geojsonseq]
  python -I pipeline/zones.py tiles OUT.pmtiles IN.geojsonseq... [--maxzoom 12]
  python -I pipeline/zones.py preview SCORE.npz ZONES.geojsonseq FOREST.geojsonseq PNG lon lat [w_km h_km]
"""
from __future__ import annotations

import json
import math
import multiprocessing as mp
import os
import subprocess
import sys
import time
from pathlib import Path

import contourpy
import numpy as np
import pyogrio
import pyogrio.raw
import pyproj
import shapely
from scipy import ndimage
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from shapely.geometry import box

LEVELS = (0.2, 0.45, 0.7)            # PILZ_LEVELS: Möglich >= 0.2, Gut >= 0.45, Top >= 0.7 (Kaum not drawn)
UP = 4                               # 100 m -> 25 m
FILL_M = 200.0                       # nearest-forest fill reach
SIMPLIFY_M = 10.0
CHAIKIN_ITER = 2
MIN_PART_M2 = 1_000.0                # drop band parts < 0.1 ha
BLOCK = 250                          # cells per block side (25 km)
MARGIN = 4                           # cells; >= fill reach (2) + bilinear support (1) + smoothing slack
MINZOOM, MAXZOOM = 5, 12
COORD_DIGITS = 6                     # ~0.1 m
# his grid boxes (habitat/build.py REGIONS); AT.npz in the release has no 'grid' key
GRID_FALLBACK = {"AT": [4_200_000, 2_940_000, 100, 7000, 4000]}
REGION_ORDER = ("AT", "CH", "DE_S", "DE_M", "DE_N")     # first region with forest wins where grids overlap

_TO_WGS = pyproj.Transformer.from_crs("EPSG:3035", "EPSG:4326", always_xy=True)
_TO_LAEA = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3035", always_xy=True)


# ---------------------------------------------------------------- grids
def load_grid(npz: str | Path) -> tuple[np.ndarray, str]:
    """[x0, y_top, res, width, height] and region name of a site grid / score npz."""
    d = np.load(npz, allow_pickle=False)
    region = str(d["region"]) if "region" in d.files else Path(npz).stem.split("-")[0].upper()
    if "grid" in d.files:
        return np.asarray(d["grid"], dtype=np.int64), region
    return np.asarray(GRID_FALLBACK[region], dtype=np.int64), region


def ownership(grids: dict[str, tuple[np.ndarray, np.ndarray]], order=REGION_ORDER) -> dict[str, np.ndarray]:
    """bool mask per region: cells it owns = it has forest there and no earlier region (order) has forest at the
    same 100 m cell (his tiles rule). grids: region -> (grid, forest). All grids are EPSG:3035 100 m aligned."""
    out = {}
    seen: list[tuple[np.ndarray, np.ndarray]] = []
    for r in [r for r in order if r in grids]:
        g, f = grids[r]
        own = f > 0
        x0, yt, res, w, h = (int(v) for v in g)
        for g2, f2 in seen:
            a0, at, _, aw, ah = (int(v) for v in g2)
            c0, r0 = (a0 - x0) // res, (yt - at) // res          # offset of the earlier grid in this grid
            cs, ce = max(0, c0), min(w, c0 + aw)
            rs, re = max(0, r0), min(h, r0 + ah)
            if cs < ce and rs < re:
                own[rs:re, cs:ce] &= ~(f2[rs - r0:re - r0, cs - c0:ce - c0] > 0)
        seen.append((g, f))
        out[r] = own
    return out


# ---------------------------------------------------------------- geometry helpers
def _chaikin_ring(c: np.ndarray) -> np.ndarray:
    p = c[:-1]
    for _ in range(CHAIKIN_ITER):
        if len(p) < 4:
            break
        q = np.roll(p, -1, axis=0)
        n = np.empty((2 * len(p), 2))
        n[0::2], n[1::2] = 0.75 * p + 0.25 * q, 0.25 * p + 0.75 * q
        p = n
    return np.vstack([p, p[:1]])


def smooth(g):
    """simplify ~10 m, Chaikin, valid (Multi)Polygon; small parts dropped."""
    if g is None or g.is_empty:
        return shapely.Polygon()
    g = shapely.simplify(g, SIMPLIFY_M, preserve_topology=True)
    polys = []
    for p in shapely.get_parts(g):
        if p.geom_type != "Polygon" or p.area < MIN_PART_M2:
            continue
        holes = [_chaikin_ring(np.asarray(r.coords)) for r in p.interiors if shapely.Polygon(r).area >= MIN_PART_M2]
        polys.append(shapely.Polygon(_chaikin_ring(np.asarray(p.exterior.coords)), holes))
    if not polys:
        return shapely.Polygon()
    out = _valid(shapely.MultiPolygon(polys)) if len(polys) > 1 else _valid(polys[0])
    return _polygonal(out)


def _valid(g):
    """make_valid that keeps holes as holes ('structure': shells unioned, holes subtracted). The default
    'linework' method can turn a hole that touches its shell after reprojection/rounding into filled area."""
    try:
        return shapely.make_valid(g, method="structure", keep_collapsed=False)
    except shapely.errors.GEOSException:
        try:
            return shapely.buffer(g, 0)
        except shapely.errors.GEOSException:
            return shapely.make_valid(g)


def _polygonal(g):
    if g is None or g.is_empty:
        return shapely.Polygon()
    if g.geom_type in ("Polygon", "MultiPolygon"):
        return g
    parts = [p for p in shapely.get_parts(g) if p.geom_type in ("Polygon", "MultiPolygon")]
    if not parts:
        return shapely.Polygon()
    return shapely.union_all(parts) if len(parts) > 1 else parts[0]


def _op(name: str, a, b):
    """polygonal overlay that tolerates empty / mixed inputs (GEOS refuses empty GeometryCollections)."""
    a, b = _polygonal(a), _polygonal(b)
    if name == "union":
        return b if a.is_empty else a if b.is_empty else _polygonal(shapely.union(a, b))
    if a.is_empty or (name == "intersection" and b.is_empty):
        return shapely.Polygon()
    if b.is_empty:
        return a
    return _polygonal(getattr(shapely, name)(a, b))


def _contour_area(z: np.ndarray, xs: np.ndarray, ys: np.ndarray, level: float):
    """{z >= level} as polygons (marching squares, linear interpolation); xs/ys ascending."""
    cg = contourpy.contour_generator(xs, ys, z, name="serial", fill_type=contourpy.FillType.OuterOffset)
    pts_list, offs_list = cg.filled(level, 1e9)
    polys = []
    for pts, offs in zip(pts_list, offs_list):
        rings = [pts[offs[i]:offs[i + 1]] for i in range(len(offs) - 1)]
        rings = [r for r in rings if len(r) >= 4]
        if rings:
            polys.append(shapely.Polygon(rings[0], rings[1:]))
    if not polys:
        return shapely.Polygon()
    return _valid(shapely.MultiPolygon(polys))


def _bilinear_up(a: np.ndarray) -> np.ndarray:
    """cell-centred bilinear upsample x UP (edges clamp)."""
    h, w = a.shape
    ry = (np.arange(h * UP) + 0.5) / UP - 0.5
    rx = (np.arange(w * UP) + 0.5) / UP - 0.5
    yy, xx = np.meshgrid(ry, rx, indexing="ij")
    return ndimage.map_coordinates(a, [yy, xx], order=1, mode="nearest").astype(np.float32)


def _read_geoms(path, **kw) -> tuple[np.ndarray, dict]:
    """geometries (shapely array) + attribute columns of a vector file (pyogrio raw API, no geopandas)."""
    meta, _, wkb, fields = pyogrio.raw.read(str(path), **kw)
    return shapely.from_wkb(wkb), {n: v for n, v in zip(meta["fields"], fields)}


# ---------------------------------------------------------------- our Upper Austria forest (release asset)
def export_ooe_forest(src_dir: str | Path, boundary: str | Path, out: str | Path, simplify_m: float = 3.0,
                      close_m: float = 8.0) -> dict:
    """our 10 m forest polygons (geojsonseq blocks <x0>_<y0> of 10 km, EPSG:4326) -> dissolved, lightly simplified
    FlatGeobuf in EPSG:3035 with a spatial index. Features: kind='boundary' (the OÖ outline, one feature) and
    kind='forest' (polygons cut at the exact 10 km block edges, so neighbours union seamlessly).
    Genus/exposition are dropped (zones only)."""
    t0 = time.time()
    src = {tuple(int(v) for v in p.stem.split("_")): p for p in Path(src_dir).glob("*.geojsonseq")}
    b = shapely.union_all(_read_geoms(boundary)[0])
    b = shapely.transform(b, lambda c: np.column_stack(_TO_LAEA.transform(c[:, 0], c[:, 1])))
    b = shapely.simplify(b, 20.0)
    geoms, kinds = [b], ["boundary"]
    raw, n_in = {}, 0
    for key, p in src.items():
        g = _read_geoms(p)[0] if p.stat().st_size else np.array([])
        n_in += len(g)
        if len(g):
            raw[key] = _valid(shapely.transform(g, lambda c: np.column_stack(_TO_LAEA.transform(c[:, 0], c[:, 1]))))
    step = 10_000
    for (x0, y0), g in raw.items():
        core = box(x0, y0, x0 + step, y0 + step)
        ctx = core.buffer(4 * close_m, join_style="mitre")
        nb = [raw[(x0 + dx, y0 + dy)] for dx in (-step, 0, step) for dy in (-step, 0, step)
              if (x0 + dx, y0 + dy) in raw]
        u = shapely.union_all(shapely.intersection(np.concatenate(nb), ctx))
        # closing: the genus polygons were simplified/smoothed one by one, so neighbours leave slits of a few
        # metres; +/- close_m merges them. Done with the neighbours' polygons, then cut at the exact block edge.
        u = shapely.buffer(shapely.buffer(u, close_m, quad_segs=2), -close_m, quad_segs=2)
        u = _polygonal(_valid(shapely.simplify(u, simplify_m, preserve_topology=True)))
        for part in shapely.get_parts(_op("intersection", u, core)):
            if part.geom_type == "Polygon" and part.area >= 500:
                holes = [r for r in part.interiors if shapely.Polygon(r).area >= MIN_PART_M2]
                geoms.append(shapely.Polygon(part.exterior, holes))
                kinds.append("forest")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    if Path(out).exists():
        Path(out).unlink()
    pyogrio.raw.write(str(out), shapely.to_wkb(np.asarray(geoms, dtype=object)), field_data=[np.asarray(kinds)],
                  fields=["kind"], geometry_type="Polygon", crs="EPSG:3035", driver="FlatGeobuf", SPATIAL_INDEX="YES")
    return {"blocks": len(src), "features_in": n_in, "features_out": len(geoms) - 1,
            "bytes": Path(out).stat().st_size, "seconds": round(time.time() - t0, 1)}


class LocalForest:
    """our forest polygons + the area where they replace his mask (lazy, per block bbox reads)."""

    def __init__(self, fgb: str | Path):
        self.path = str(fgb)
        self.area = shapely.union_all(_read_geoms(self.path, where="kind = 'boundary'")[0])
        shapely.prepare(self.area)

    def forest(self, bbox: tuple[float, float, float, float]):
        g = _read_geoms(self.path, bbox=bbox, where="kind = 'forest'")[0]
        return shapely.union_all(g) if len(g) else shapely.Polygon()


# ---------------------------------------------------------------- per block
_S: dict = {}


def _block(task: tuple[int, int]) -> tuple[list[str], list[str], list[tuple[int, bytes]], dict, bytes | None]:
    r0, c0 = task
    score, forest, own, grid = _S["score"], _S["forest"], _S["own"], _S["grid"]
    x0, yt, res = float(grid[0]), float(grid[1]), float(grid[2])
    H, W = score.shape
    r1, c1 = min(r0 + BLOCK, H), min(c0 + BLOCK, W)
    core = box(x0 + c0 * res, yt - r1 * res, x0 + c1 * res, yt - r0 * res)
    stats = {"cells": 0}
    lf: LocalForest | None = _S.get("local")
    local_hit = lf is not None and lf.area.intersects(core)
    rs, re, cs, ce = r0 - MARGIN, r1 + MARGIN, c0 - MARGIN, c1 + MARGIN
    # window with margin, padded beyond the region edge
    pad = ((max(0, -rs), max(0, re - H)), (max(0, -cs), max(0, ce - W)))
    sl = (slice(max(0, rs), min(H, re)), slice(max(0, cs), min(W, ce)))
    f = (forest[sl] > 0) & own[sl]
    s = score[sl]
    valid = f & np.isfinite(s)
    if not valid.any():
        return [], [], [], stats, None                            # (our forest without a score within reach too)
    f, s, valid = np.pad(f, pad), np.pad(s, pad, constant_values=np.nan), np.pad(valid, pad)
    stats["cells"] = int(valid[MARGIN:-MARGIN, MARGIN:-MARGIN].sum())
    dist, (iy, ix) = ndimage.distance_transform_edt(~valid, return_indices=True)
    filled = s[iy, ix]
    filled[dist * res > FILL_M + 1e-6] = -1.0
    h, w = filled.shape
    step = res / UP
    # upsampled cell centres, ascending y for contourpy
    xs = x0 + cs * res + (np.arange(w * UP) + 0.5) * step
    ys = (yt - rs * res) - (np.arange(h * UP) + 0.5) * step
    ys = ys[::-1]
    if not _S["forest_out"] and not (filled >= LEVELS[0]).any():
        return [], [], [], stats, None                            # nothing to draw (out of season)
    U = _bilinear_up(filled)[::-1]

    # forest outline (species independent: cached per region block when the caller passes a cache)
    cache, new_forest = _S.get("fcache"), None
    if cache is not None and task in cache:
        forest_g = shapely.from_wkb(cache[task])
    else:
        ctx = core.buffer(MARGIN * res * 0.5, join_style="mitre")
        his = smooth(_contour_area(_bilinear_up(f.astype(np.float32))[::-1], xs, ys, 0.5))
        if local_hit:
            ours = smooth_local(lf.forest(ctx.bounds))
            forest_g = _op("union", _op("difference", his, lf.area), _op("intersection", ours, lf.area))
        else:
            forest_g = his
        forest_g = _op("intersection", forest_g, ctx)
        if cache is not None:
            new_forest = shapely.to_wkb(forest_g)

    nested, prev = [], forest_g
    for lv in LEVELS:
        a = _op("intersection", smooth(_contour_area(U, xs, ys, lv)), prev)
        nested.append(a)
        prev = a
    bands = [_op("difference", nested[0], nested[1]), _op("difference", nested[1], nested[2]), nested[2]]

    # parts touching the block edge are merged across blocks in the main process (no seams)
    lines, flines, edges = [], [], []
    rim = core.exterior
    shapely.prepare(rim)
    for lv, g in enumerate(bands, start=1):
        for p in shapely.get_parts(_op("intersection", g, core)):
            if p.geom_type != "Polygon" or p.area < MIN_PART_M2:
                continue
            stats[f"lv{lv}_m2"] = stats.get(f"lv{lv}_m2", 0.0) + p.area
            if rim.intersects(p):
                edges.append((lv, shapely.to_wkb(p)))
            else:
                lines.append(_feature(p, {"lv": lv}))
    if _S.get("forest_out"):
        for p in shapely.get_parts(_op("intersection", forest_g, core)):
            if p.geom_type == "Polygon":
                flines.append(_feature(p, {}))          # debug/preview only: seams don't matter
    return lines, flines, edges, stats, new_forest


def _merge(task: tuple[int, list[bytes]]) -> list[str]:
    lv, ws = task
    g = shapely.from_wkb(ws)
    merged = _polygonal(shapely.union_all(g)) if len(g) > 1 else g[0]
    return [_feature(p, {"lv": lv}) for p in shapely.get_parts(merged) if p.geom_type == "Polygon"]


def smooth_local(g):
    """our polygons are already smooth (12 m simplify + Chaikin); just make them valid."""
    return _polygonal(_valid(g))


def _minzoom(area_m2: float, maxzoom: int = MAXZOOM) -> int:
    """first zoom where the part spans >= ~1.5 screen px (256 px tiles, ~48 deg N): drops slivers at low zoom."""
    px_z0 = 104_750.0 * 1.5
    return int(min(maxzoom, max(MINZOOM, math.ceil(math.log2(px_z0 / max(math.sqrt(area_m2), 1.0))))))


def _feature(p, props: dict, maxzoom: int = MAXZOOM) -> str:
    g = shapely.transform(p, lambda c: np.column_stack(_TO_WGS.transform(c[:, 0], c[:, 1])))
    g = shapely.set_precision(g, 10 ** -COORD_DIGITS, mode="pointwise")
    if not g.is_valid:                     # rounding can pinch narrow Chaikin necks
        g = _polygonal(_valid(g))
    tip = f',"tippecanoe":{{"minzoom":{_minzoom(p.area, maxzoom)}}}' if props else ""
    return (f'{{"type":"Feature","properties":{json.dumps(props, separators=(",", ":"))}{tip},'
            f'"geometry":{shapely.to_geojson(g)}}}')


# ---------------------------------------------------------------- API
def build_zones(score: np.ndarray, grid, forest: np.ndarray, region: str, out_geojsonseq: str | Path,
                own: np.ndarray | None = None, ooe_forest: str | Path | None = None,
                forest_out: str | Path | None = None, jobs: int | None = None,
                forest_cache: dict | None = None) -> dict:
    """score: float32 (ny, nx) level-ready score (3x3 forest mean, NaN outside forest), row 0 = north;
    grid: [x0, y_top, res, width, height] EPSG:3035; forest: uint8 his forest classes (0 = none);
    own: optional bool mask of cells this region owns (see ownership()); ooe_forest: out/ooe_forest.fgb, used
    where its boundary covers the region (AT). Writes one feature per band polygon {lv} (+ tippecanoe minzoom).
    forest_cache: pass the same dict for every species of one region (same own/ooe_forest) to compute the forest
    outline only once. Returns stats."""
    t0 = time.time()
    grid = np.asarray(grid, dtype=np.int64)
    assert score.shape == forest.shape == (int(grid[4]), int(grid[3])), (score.shape, forest.shape, grid)
    _S.clear()
    _S.update(score=score.astype(np.float32, copy=False), forest=forest, grid=grid,
              own=own if own is not None else np.ones(forest.shape, bool), forest_out=bool(forest_out),
              fcache=forest_cache)
    if ooe_forest:
        if not Path(ooe_forest).exists():
            raise FileNotFoundError(f"ooe_forest not found: {ooe_forest}")
        _S["local"] = LocalForest(ooe_forest)
    H, W = score.shape
    tasks = [(r, c) for r in range(0, H, BLOCK) for c in range(0, W, BLOCK)]
    tasks = [(r, c) for r, c in tasks
             if (forest[max(0, r - MARGIN):r + BLOCK + MARGIN, max(0, c - MARGIN):c + BLOCK + MARGIN] > 0).any()]
    jobs = jobs or os.cpu_count() or 2
    out_geojsonseq, n = Path(out_geojsonseq), 0
    out_geojsonseq.parent.mkdir(parents=True, exist_ok=True)
    tot: dict = {}
    ff = open(forest_out, "w") if forest_out else None
    with open(out_geojsonseq, "w") as fh, mp.get_context("fork").Pool(jobs) as pool:
        edges: dict[int, list] = {}
        for task, (lines, flines, ed, st, fw) in zip(tasks, pool.imap(_block, tasks, chunksize=1)):
            if fw is not None:
                forest_cache[task] = fw
            for ln in lines:
                fh.write(ln + "\n")
            n += len(lines)
            if ff:
                for ln in flines:
                    ff.write(ln + "\n")
            for k, w in ed:
                edges.setdefault(k, []).append(w)
            for k, v in st.items():
                tot[k] = tot.get(k, 0) + v
        t1 = time.time()                                 # dissolve parts cut by block edges, per connected group
        groups = []
        for k, ws in edges.items():
            g = shapely.from_wkb(ws)
            i, j = shapely.STRtree(g).query(g, predicate="intersects")
            _, lab = connected_components(coo_matrix((np.ones(len(i)), (i, j)), shape=(len(g), len(g))), directed=False)
            for c in np.unique(lab):
                groups.append((k, [ws[q] for q in np.flatnonzero(lab == c)]))
        groups.sort(key=lambda t: -len(t[1]))
        for lines in pool.imap_unordered(_merge, groups, chunksize=1):
            for ln in lines:
                fh.write(ln + "\n")
            n += len(lines)
        tot["seam_seconds"] = round(time.time() - t1, 1)
    if ff:
        ff.close()
    _S.clear()
    return {"region": region, "blocks": len(tasks), "features": n, "bytes": out_geojsonseq.stat().st_size,
            "seconds": round(time.time() - t0, 1),
            **{k: (round(v / 1e6, 1) if k.endswith("_m2") else v) for k, v in tot.items()}}


# coalesce + reorder: neighbouring parts with the same lv become one feature per tile; shared borders between
# bands are simplified identically (no gaps); slivers are dropped at low zoom via the per-feature minzoom
# (_minzoom) instead of tippecanoe's tiny-polygon dithering (which draws pixel squares)
TIPPECANOE_ARGS = ["-l", "chance", "-y", "lv", "--coalesce", "--reorder", "--detect-shared-borders",
                   "--no-tiny-polygon-reduction", "--coalesce-smallest-as-needed", "--simplification=4",
                   "--simplification-at-maximum-zoom=2", "--hilbert", "-P", "--force", "-q"]


def tippecanoe_cmd(paths: list[str | Path], out: str | Path, maxzoom: int = MAXZOOM, extra: list[str] | None = None) -> list[str]:
    return ["tippecanoe", "-o", str(out), "-Z", str(MINZOOM), "-z", str(maxzoom), *TIPPECANOE_ARGS, *(extra or []),
            *[str(p) for p in paths]]


def to_pmtiles(paths: list[str | Path], out: str | Path, maxzoom: int = MAXZOOM, extra: list[str] | None = None) -> dict:
    """one or more region geojsonseq files (e.g. AT, CH, DE_S, DE_M, DE_N of one species) -> one PMTiles
    (layer 'chance', prop lv, z5..maxzoom; the map overzooms beyond)."""
    t0 = time.time()
    if all(Path(p).stat().st_size == 0 for p in paths):
        # nothing above Möglich anywhere (out of season): tippecanoe refuses empty input; index.json gets file null
        return {"file": None, "bytes": 0, "seconds": 0.0}
    subprocess.run(tippecanoe_cmd(paths, out, maxzoom, extra), check=True)
    return {"file": str(out), "bytes": Path(out).stat().st_size, "seconds": round(time.time() - t0, 1)}


# ---------------------------------------------------------------- dev: stand-in score
def standin(grid_npz: str | Path, out: str | Path) -> None:
    """smooth function of the site factors with W1's fixture format (score, grid, forest); 3x3 forest mean."""
    d = np.load(grid_npz)
    grid, region = load_grid(grid_npz)
    fo = d["forest"]
    share = d["share"].astype(np.float32) / 3
    elev = d["elev15"].astype(np.float32) * 15
    slope = d["slope"].astype(np.float32) / 6
    wet = d["wet"].astype(np.float32) / 7
    asp = d["aspect"].astype(np.float32)
    north = np.where(asp == 0, 0, np.cos((asp - 1) * np.pi / 4))
    raw = (0.25 + 0.35 * share + 0.35 * np.exp(-((elev - 900) / 450) ** 2) + 0.12 * wet + 0.08 * north
           - 0.15 * slope)
    noise = ndimage.gaussian_filter(np.random.default_rng(1).standard_normal(fo.shape).astype(np.float32), 30)
    raw = np.clip(raw - 0.25 + noise / noise.std() * 0.12, 0, 1).astype(np.float32)
    f = (fo > 0).astype(np.float32)
    num = ndimage.uniform_filter(raw * f, 3, mode="constant")
    den = ndimage.uniform_filter(f, 3, mode="constant")
    score = np.where(fo > 0, num / np.maximum(den, 1e-6), np.nan).astype(np.float32)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out, score=score, grid=grid, forest=fo, region=region)


# ---------------------------------------------------------------- preview
LEVEL_COLORS = ["#f2c14e", "#e8743b", "#c2362f"]


def preview(score_npz, zones, forest_geo, png, lon, lat, w_km=20.0, h_km=15.0, title=""):
    import matplotlib  # noqa: PLC0415
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: PLC0415
    from matplotlib.colors import ListedColormap  # noqa: PLC0415
    from shapely.plotting import plot_polygon  # noqa: PLC0415

    d = np.load(score_npz)
    score, forest = d["score"], d["forest"]
    grid, _ = load_grid(score_npz)
    x0, yt, res = (float(v) for v in grid[:3])
    cx, cy = _TO_LAEA.transform(lon, lat)
    bx = (cx - w_km * 500, cy - h_km * 500, cx + w_km * 500, cy + h_km * 500)
    c0, c1 = int((bx[0] - x0) // res), int((bx[2] - x0) // res)
    r0, r1 = int((yt - bx[3]) // res), int((yt - bx[1]) // res)
    s, fo = score[r0:r1, c0:c1], forest[r0:r1, c0:c1]
    lvl = np.zeros(s.shape, np.uint8)
    for i, t in enumerate(LEVELS, start=1):
        lvl[np.nan_to_num(s, nan=-1) >= t] = i
    pix = np.where(fo > 0, lvl, 255).astype(np.float32)
    pix[pix == 255] = np.nan
    ext = (x0 + c0 * res, x0 + c1 * res, yt - r1 * res, yt - r0 * res)
    wbb = _TO_WGS.transform_bounds(*bx)

    def read(p):
        g, df = _read_geoms(p, bbox=wbb)
        g = shapely.transform(g, lambda c: np.column_stack(_TO_LAEA.transform(c[:, 0], c[:, 1])))
        return df, g

    zdf, zg = read(zones)
    _, fg = read(forest_geo)
    fig, axs = plt.subplots(1, 2, figsize=(16, 6.4), dpi=110)
    cmap = ListedColormap(["#d9e7d3"] + LEVEL_COLORS)
    axs[0].imshow(pix, extent=ext, cmap=cmap, vmin=-0.5, vmax=3.5, interpolation="nearest")
    axs[0].set_title("100 m pixels (his look): forest + levels")
    clipbox = box(*bx)
    for g in fg:
        g = _polygonal(g.intersection(clipbox))
        if not g.is_empty:
            plot_polygon(g, ax=axs[1], add_points=False, facecolor="#d9e7d3", edgecolor="#4d7a45", linewidth=0.5)
    for g, lv in zip(zg, zdf["lv"].astype(int)):
        g = _polygonal(g.intersection(clipbox))
        if not g.is_empty:
            plot_polygon(g, ax=axs[1], add_points=False, facecolor=LEVEL_COLORS[lv - 1], edgecolor="none", alpha=0.85)
    axs[1].set_title("zones (25 m contours, simplified + Chaikin) + forest outline")
    for a in axs:
        a.set_xlim(bx[0], bx[2])
        a.set_ylim(bx[1], bx[3])
        a.set_aspect("equal")
        a.set_xticks([])
        a.set_yticks([])
    fig.suptitle(title or f"{Path(score_npz).name} around {lon:.2f}E {lat:.2f}N, {w_km:g} x {h_km:g} km")
    fig.tight_layout()
    Path(png).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(png)
    plt.close(fig)


# ---------------------------------------------------------------- cli
def _main(argv: list[str]) -> None:
    cmd = argv[0] if argv else ""
    if cmd == "ooe-forest":
        print(json.dumps(export_ooe_forest(argv[1], argv[2], argv[3])))
    elif cmd == "standin":
        standin(argv[1], argv[2])
    elif cmd == "build":
        args = argv[1:]
        opt = {}
        for k in ("--ooe", "--forest-out"):
            if k in args:
                i = args.index(k)
                opt[k] = args[i + 1]
                del args[i:i + 2]
        d = np.load(args[0])
        grid, _ = load_grid(args[0])
        print(json.dumps(build_zones(d["score"], grid, d["forest"], args[1], args[2],
                                     ooe_forest=opt.get("--ooe"), forest_out=opt.get("--forest-out"))))
    elif cmd == "tiles":
        args, mz = argv[1:], MAXZOOM
        if "--maxzoom" in args:
            i = args.index("--maxzoom")
            mz = int(args[i + 1])
            del args[i:i + 2]
        print(json.dumps(to_pmtiles(args[1:], args[0], mz)))
    elif cmd == "preview":
        lon, lat = float(argv[5]), float(argv[6])
        wk, hk = (float(argv[7]), float(argv[8])) if len(argv) > 8 else (20.0, 15.0)
        preview(argv[1], argv[2], argv[3], argv[4], lon, lat, wk, hk)
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    _main(sys.argv[1:])
