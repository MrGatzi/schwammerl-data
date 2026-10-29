"""Tree map: main genus per forest area -> smooth polygons -> trees-<ver>.pmtiles (docs/format.md).

Outside Upper Austria from his 100 m site grids (g1 = main genus, forest > 0, ownership rule of zones.py),
smoothed like the chance zones (no pixel look):
  1. per genus a share field: Gaussian of "main genus == g" over the forest cells, divided by the forest density
  2. d_g = share_g - max(other shares), bilinear upsample x4 (25 m), contour d_g > 0 (where g wins)
  3. simplify + Chaikin, clip to the smoothed forest outline (as zones.py), make the genera disjoint
Inside Upper Austria our 10 m forest polygons with genus (out/ooe_trees.fgb, see export-ooe).

Genus keys (prop `g`) = his GENERA (habitat/build.py, ForestPaths classes 0..6) = the app's GENERA keys
(src/scoring/species.ts, used by treeColorExpression): larix picea pinus fagus quercus conifer broadleaf.

usage:
  python -I pipeline/trees.py export-ooe SRC_DIR OOE_FOREST.fgb OUT.fgb     our genus polygons -> FlatGeobuf (once)
  python -I pipeline/trees.py build GRIDS_DIR OOE_TREES.fgb OUT_DIR [--ver V] [--jobs N]
"""
from __future__ import annotations

import datetime as dt
import json
import multiprocessing as mp
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pyogrio.raw
import shapely
from scipy import ndimage
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from shapely.geometry import box

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zones as Z  # noqa: E402

GENERA = ["larix", "picea", "pinus", "fagus", "quercus", "conifer", "broadleaf"]   # his g1 codes 0..6
SIGMA = 0.7                   # cells: share-field smoothing (removes single-cell speckle)
MIN_PART_M2 = 5_000.0         # genus parts < 0.5 ha are dropped (grid part)
MINZOOM, MAXZOOM = 7, 11          # z12 would be ~140 MB for little gain; the app overzooms
TIPPECANOE_ARGS = ["-l", "trees", "-y", "g", "--coalesce", "--reorder", "--detect-shared-borders",
                   "--no-tiny-polygon-reduction", "--coalesce-smallest-as-needed", "--simplification=4",
                   "--simplification-at-maximum-zoom=2", "--hilbert", "-P", "--force", "-q",
                   "--name", "trees", "--attribution", "Trees: ForestPaths (CC BY 4.0) via shroomzn.app, BFW"]


# ---------------------------------------------------------------- our Upper Austria polygons with genus
_E: dict = {}


def _export_block(key):
    x0, y0 = key
    raw, step, close_m, simplify_m = _E["raw"], _E["step"], _E["close_m"], _E["simplify_m"]
    core = box(x0, y0, x0 + step, y0 + step)
    ctx = core.buffer(4 * close_m, join_style="mitre")
    gs, ks = [], []
    for dx in (-step, 0, step):
        for dy in (-step, 0, step):
            if (x0 + dx, y0 + dy) in raw:
                g, k = raw[(x0 + dx, y0 + dy)]
                gs.append(g)
                ks.append(k)
    g, k = np.concatenate(gs), np.concatenate(ks)
    hit = shapely.intersects(g, ctx)
    g, k = shapely.intersection(g[hit], ctx), k[hit]
    by = {gen: Z._polygonal(shapely.union_all(g[k == gen])) for gen in GENERA if (k == gen).any()}
    out = []
    for gen, u in by.items():
        # close the slits between neighbouring polygons of the same genus (they were smoothed one by one),
        # but never grow into another genus' original area: the genera stay disjoint
        c = shapely.buffer(shapely.buffer(u, close_m, quad_segs=2), -close_m, quad_segs=2)
        others = [v for o, v in by.items() if o != gen]
        if others:
            c = Z._op("difference", c, shapely.union_all(others))
        c = Z._polygonal(Z._valid(shapely.simplify(c, simplify_m, preserve_topology=True)))
        for part in shapely.get_parts(Z._op("intersection", c, core)):
            if part.geom_type == "Polygon" and part.area >= 500:
                holes = [r for r in part.interiors if shapely.Polygon(r).area >= Z.MIN_PART_M2]
                out.append((shapely.to_wkb(shapely.Polygon(part.exterior, holes)), gen))
    return out


def export_ooe_trees(src_dir: str | Path, ooe_forest: str | Path, out: str | Path, simplify_m: float = 4.0,
                     close_m: float = 8.0, jobs: int | None = None) -> dict:
    """our 10 m forest polygons with genus (geojsonseq blocks <x0>_<y0> of 10 km, EPSG:4326) -> per block and genus
    dissolved, closed and simplified FlatGeobuf in EPSG:3035 with a spatial index. Features: kind='boundary' (the OÖ
    outline, copied from ooe_forest.fgb) and kind='forest' with genus (cut at the 10 km block edges)."""
    t0 = time.time()
    b = Z._read_geoms(ooe_forest, where="kind = 'boundary'")[0]
    src = {tuple(int(v) for v in p.stem.split("_")): p for p in Path(src_dir).glob("*.geojsonseq")}
    raw, n_in, unknown = {}, 0, {}
    for key, p in src.items():
        if not p.stat().st_size:
            continue
        g, f = Z._read_geoms(p, columns=["genus"])
        n_in += len(g)
        k = np.asarray(f["genus"], dtype=object)
        for u in set(k) - set(GENERA):
            unknown[u] = unknown.get(u, 0) + int((k == u).sum())
        ok = np.isin(k, GENERA)
        g = shapely.transform(g[ok], lambda c: np.column_stack(Z._TO_LAEA.transform(c[:, 0], c[:, 1])))
        raw[key] = (Z._valid(g), k[ok].astype(str))
    _E.update(raw=raw, step=10_000, close_m=close_m, simplify_m=simplify_m)
    geoms, kinds, genus = [shapely.to_wkb(b[0])], ["boundary"], [""]
    with mp.get_context("fork").Pool(jobs or os.cpu_count()) as pool:
        for feats in pool.imap_unordered(_export_block, list(raw), chunksize=1):
            for w, gen in feats:
                geoms.append(w)
                kinds.append("forest")
                genus.append(gen)
    _E.clear()
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    if Path(out).exists():
        Path(out).unlink()
    pyogrio.raw.write(str(out), np.asarray(geoms, dtype=object), field_data=[np.asarray(kinds), np.asarray(genus)],
                      fields=["kind", "genus"], geometry_type="Polygon", crs="EPSG:3035", driver="FlatGeobuf",
                      SPATIAL_INDEX="YES")
    return {"blocks": len(src), "features_in": n_in, "features_out": len(geoms) - 1, "unknown_genus": unknown,
            "bytes": Path(out).stat().st_size, "seconds": round(time.time() - t0, 1)}


class LocalTrees:
    def __init__(self, fgb: str | Path):
        self.path = str(fgb)
        self.area = shapely.union_all(Z._read_geoms(self.path, where="kind = 'boundary'")[0])
        shapely.prepare(self.area)

    def by_genus(self, bbox) -> dict:
        g, f = Z._read_geoms(self.path, bbox=bbox, where="kind = 'forest'", columns=["kind", "genus"])
        k = np.asarray(f["genus"], dtype=object) if len(g) else np.array([], dtype=object)
        return {gen: Z._polygonal(Z._valid(shapely.union_all(g[k == gen]))) for gen in GENERA if (k == gen).any()}


# ---------------------------------------------------------------- grid part, per block
_S: dict = {}


def _block(task):
    r0, c0 = task
    g1, f_all, grid = _S["g1"], _S["forest"], _S["grid"]
    x0, yt, res = float(grid[0]), float(grid[1]), float(grid[2])
    H, W = f_all.shape
    M = Z.MARGIN
    r1, c1 = min(r0 + Z.BLOCK, H), min(c0 + Z.BLOCK, W)
    core = box(x0 + c0 * res, yt - r1 * res, x0 + c1 * res, yt - r0 * res)
    lt: LocalTrees | None = _S.get("local")
    local_hit = lt is not None and lt.area.intersects(core)
    rs, re, cs, ce = r0 - M, r1 + M, c0 - M, c1 + M
    pad = ((max(0, -rs), max(0, re - H)), (max(0, -cs), max(0, ce - W)))
    sl = (slice(max(0, rs), min(H, re)), slice(max(0, cs), min(W, ce)))
    f = np.pad(f_all[sl], pad)
    if not f.any() and not local_hit:
        return [], []
    g = np.pad(g1[sl], pad)
    h, w = f.shape
    step = res / Z.UP
    xs = x0 + cs * res + (np.arange(w * Z.UP) + 0.5) * step
    ys = ((yt - rs * res) - (np.arange(h * Z.UP) + 0.5) * step)[::-1]
    ctx = core.buffer(M * res * 0.5, join_style="mitre")

    areas: dict[str, object] = {}
    if f.any():
        forest_g = Z.smooth(Z._contour_area(Z._bilinear_up(f.astype(np.float32))[::-1], xs, ys, 0.5))
        if local_hit:
            forest_g = Z._op("difference", forest_g, lt.area)
        forest_g = Z._op("intersection", forest_g, ctx)
        den = ndimage.gaussian_filter(f.astype(np.float32), SIGMA, mode="constant")
        present = [k for k in range(len(GENERA)) if ((g == k) & f).any()]
        share = {k: ndimage.gaussian_filter(((g == k) & f).astype(np.float32), SIGMA, mode="constant")
                 / np.maximum(den, 1e-3) for k in present}
        taken = shapely.Polygon()
        for k in present:
            others = [share[o] for o in present if o != k]
            d = share[k] - (np.maximum.reduce(others) if others else 0.0)
            d[den < 1e-3] = -1.0
            a = Z._op("intersection", Z.smooth(_contour(Z._bilinear_up(d)[::-1], xs, ys)), forest_g)
            a = Z._op("difference", a, taken)
            if not a.is_empty:
                areas[GENERA[k]] = a
                taken = Z._op("union", taken, a)
    if local_hit:
        for gen, a in lt.by_genus(ctx.bounds).items():
            a = Z._op("intersection", a, Z._op("intersection", lt.area, ctx))
            areas[gen] = Z._op("union", areas.get(gen, shapely.Polygon()), a)

    lines, edges = [], []
    rim = core.exterior
    shapely.prepare(rim)
    for gen, a in areas.items():
        for p in shapely.get_parts(Z._op("intersection", a, core)):
            if p.geom_type != "Polygon" or p.area < (Z.MIN_PART_M2 if local_hit else MIN_PART_M2):
                continue
            if rim.intersects(p):
                edges.append((gen, shapely.to_wkb(p)))
            else:
                lines.append(_feature(p, gen))
    return lines, edges


def _contour(z, xs, ys):
    return Z._contour_area(z, xs, ys, 1e-6)


def _feature(p, gen: str) -> str:
    return Z._feature(p, {"g": gen}, MAXZOOM)     # minzoom capped at our max zoom (else dropped)


def _merge(task):
    gen, ws = task
    g = shapely.from_wkb(ws)
    merged = Z._polygonal(shapely.union_all(g)) if len(g) > 1 else g[0]
    return [_feature(p, gen) for p in shapely.get_parts(merged) if p.geom_type == "Polygon"]


def build_region(npz: Path, own: np.ndarray, fh, local: LocalTrees | None, jobs: int) -> dict:
    t0 = time.time()
    d = np.load(npz)
    grid, region = Z.load_grid(npz)
    forest = (d["forest"] > 0) & own
    _S.clear()
    _S.update(g1=d["g1"], forest=forest, grid=grid)
    if local is not None and region == "AT":
        _S["local"] = local
    H, W = forest.shape
    B, M = Z.BLOCK, Z.MARGIN
    tasks = [(r, c) for r in range(0, H, B) for c in range(0, W, B)
             if forest[max(0, r - M):r + B + M, max(0, c - M):c + B + M].any()
             or ("local" in _S and _S["local"].area.intersects(
                 box(grid[0] + c * grid[2], grid[1] - min(r + B, H) * grid[2],
                     grid[0] + min(c + B, W) * grid[2], grid[1] - r * grid[2])))]
    n = 0
    with mp.get_context("fork").Pool(jobs) as pool:
        edges: dict[str, list] = {}
        for lines, ed in pool.imap(_block, tasks, chunksize=1):
            for ln in lines:
                fh.write(ln + "\n")
            n += len(lines)
            for k, w in ed:
                edges.setdefault(k, []).append(w)
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
    _S.clear()
    return {"region": region, "blocks": len(tasks), "features": n, "seconds": round(time.time() - t0, 1)}


def build_trees(grids_dir: str | Path, ooe_trees: str | Path | None, out_dir: str | Path, ver: str | None = None,
                jobs: int | None = None, regions=Z.REGION_ORDER) -> dict:
    """-> out_dir/trees-<ver>.pmtiles (layer trees, prop g, z7..z11). Returns stats incl. the file name."""
    t0 = time.time()
    ver = ver or dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M")
    jobs = jobs or os.cpu_count() or 2
    grids_dir, out_dir = Path(grids_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    G = {}
    for r in regions:
        p = grids_dir / f"{r}.npz"
        G[r] = (Z.load_grid(p)[0], np.load(p)["forest"])
    own = Z.ownership(G)
    del G
    local = None
    if ooe_trees:
        if not Path(ooe_trees).exists():
            raise FileNotFoundError(f"ooe_trees not found: {ooe_trees}")
        local = LocalTrees(ooe_trees)
    rep: dict = {"regions": []}
    work = Path(__file__).resolve().parent.parent / "work"
    work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work) as tmp:
        seq = Path(tmp) / "trees.geojsonseq"
        with open(seq, "w") as fh:
            for r in regions:
                rep["regions"].append(build_region(grids_dir / f"{r}.npz", own[r], fh, local, jobs))
                print(json.dumps(rep["regions"][-1]), flush=True)
        rep["geojson_mb"] = round(seq.stat().st_size / 1e6, 1)
        name = f"trees-{ver}.pmtiles"
        t1 = time.time()
        subprocess.run(["tippecanoe", "-o", str(out_dir / name), "-Z", str(MINZOOM), "-z", str(MAXZOOM),
                        *TIPPECANOE_ARGS, str(seq)], check=True)
        rep["tiles_s"] = round(time.time() - t1, 1)
    rep.update(file=name, mb=round((out_dir / name).stat().st_size / 1e6, 1), seconds=round(time.time() - t0, 1))
    return rep


def _main(argv):
    cmd = argv[0] if argv else ""
    if cmd == "export-ooe":
        print(json.dumps(export_ooe_trees(argv[1], argv[2], argv[3])))
    elif cmd == "build":
        args = argv[1:]
        opt = {}
        for k in ("--ver", "--jobs"):
            if k in args:
                i = args.index(k)
                opt[k] = args[i + 1]
                del args[i:i + 2]
        print(json.dumps(build_trees(args[0], args[1] or None, args[2], opt.get("--ver"),
                                     int(opt["--jobs"]) if "--jobs" in opt else None)))
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    _main(sys.argv[1:])
