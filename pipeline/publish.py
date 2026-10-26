"""Daily publish: chance zones for all species + rain + tap cells + stations -> site/ with index.json (docs/format.md).

usage:
  python pipeline/publish.py --site site/ [--force] [--rain-src SRC]

Steps:
  1. fetch pilz_weather from shroomzn.app, read the live index.json (404 on the first run);
     same `weatherGenerated` as the live site -> print "skip", GitHub output skip=true, exit 0 (unless --force)
  2. download the previous publish's stamped files into site/ (the app may still hold the old index.json)
  3. rain: build from rain_daily (shroomzn.app). Not there yet (404): reuse the previous publish's rain, cells and
     stations (and its rainDays); no previous publish: build from --rain-src / $RAIN_SRC; else fail
  4. per species: score all regions (model.py), zones (zones.py), PMTiles z5-z12 -> site/chance/<id>-<stamp>.pmtiles
  5. trees: a trees-*.pmtiles already in site/ before this run (built by trees.py) wins, else the live one
  6. write index.json, validate (files exist, < 95 MB each, < 950 MB total, keys) -> non-zero exit on failure, so the
     deploy step does not run and the last good site stays live
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import model  # noqa: E402
import rain  # noqa: E402
import shroomzn_io as sio  # noqa: E402
import zones  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LIVE = "https://mrgatzi.github.io/schwammerl-data"
MAX_FILE = 95 * 1024 * 1024        # Pages: 100 MB per file
MAX_SITE = 950 * 1024 * 1024       # Pages: 1 GB per site
BOUNDS = [sio.GRID_WEST, sio.GRID_SOUTH, sio.GRID_EAST, sio.GRID_NORTH]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def gh_output(**kv):
    p = os.environ.get("GITHUB_OUTPUT")
    if p:
        with open(p, "a") as f:
            for k, v in kv.items():
                f.write(f"{k}={v}\n")


# ---------------------------------------------------------------- live site
def http_get(url: str, timeout: float = 300) -> bytes | None:
    """bytes, or None on 404 (anything else raises: better no publish than one that drops the live files)."""
    req = urllib.request.Request(url, headers={"User-Agent": sio.USER_AGENT})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt == 2 or e.code < 500:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(5 * (attempt + 1))
    return None


def live_index(live: str) -> dict | None:
    b = http_get(f"{live.rstrip('/')}/index.json?t={int(time.time())}", timeout=60)
    return json.loads(b) if b else None


def cell_chunk_names() -> list[str]:
    lons = range(math.floor(sio.GRID_WEST), math.floor(sio.GRID_EAST) + 1)
    lats = range(math.floor(sio.GRID_SOUTH), math.floor(sio.GRID_NORTH) + 1)
    return [f"{lon}_{lat}.json" for lat in lats for lon in lons]


def stamped_files(idx: dict) -> list[str]:
    """the single files an index.json references (cells dir separately)"""
    out = [v["file"] for v in idx.get("species", {}).values() if v.get("file")]
    for k in ("rain", "trees", "stations"):
        if (idx.get(k) or {}).get("file"):
            out.append(idx[k]["file"])
    return out


def fetch_previous(live: str, idx: dict, site: Path) -> dict:
    """download every file the live index references into site/ (same relative paths)."""
    t0 = time.time()
    paths = stamped_files(idx)
    cells = (idx.get("cells") or {}).get("dir")
    if cells:
        paths += [f"{cells}/{n}" for n in cell_chunk_names()]
    base = live.rstrip("/")

    def one(rel: str):
        dst = site / rel
        if dst.exists():
            return rel, dst.stat().st_size, "kept"
        b = http_get(f"{base}/{rel}")
        if b is None:
            return rel, 0, "missing"
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".part")
        tmp.write_bytes(b)
        tmp.rename(dst)
        return rel, len(b), "ok"

    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(one, paths))
    missing = [r for r, _, s in res if s == "missing" and not r.startswith(f"{cells}/")]
    for r in missing:
        log(f"previous publish: {r} not found on the live site")
    return {"files": sum(1 for _, _, s in res if s == "ok"), "mb": round(sum(n for _, n, _ in res) / 1e6, 1),
            "missing": missing, "s": round(time.time() - t0, 1)}


# ---------------------------------------------------------------- inputs
def fetch_weather(src: str, dst: Path) -> Path:
    dst.mkdir(parents=True, exist_ok=True)
    meta = sio.read_bytes(src, "pilz_weather.json", timeout=60)
    doc = json.loads(meta)
    (dst / doc.get("file", "pilz_weather.bin")).write_bytes(sio.read_bytes(src, doc.get("file", "pilz_weather.bin")))
    (dst / "pilz_weather.json").write_bytes(meta)
    return dst / "pilz_weather.json"


def rain_available(src: str) -> bool:
    try:
        sio.read_json(src, "rain_daily.json")
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------- chance zones
def build_species(site: Path, stamp: str, weather: model.Weather, grids: Path, ooe: Path, date: dt.date,
                  jobs: int, work: Path) -> tuple[dict, dict]:
    species = model.load_species()
    rep: dict = {"date": str(date), "regions": {}, "species": {}}
    t0 = time.time()
    G, regs = {}, {}
    for r in zones.REGION_ORDER:
        p = grids / f"{r}.npz"
        G[r] = (zones.load_grid(p)[0], np.load(p)["forest"])
    own = zones.ownership(G)
    for r in zones.REGION_ORDER:
        regs[r] = model.Region(grids / f"{r}.npz", weather)
        rep["regions"][r] = {"cells": int(len(regs[r].cells)), "s": round(regs[r].seconds, 1)}
    rep["regions_s"] = round(time.time() - t0, 1)
    fcache = {r: {} for r in zones.REGION_ORDER}
    (site / "chance").mkdir(parents=True, exist_ok=True)
    out: dict = {}
    pending = None            # (sid, Popen, seqs, out path, t) of the tippecanoe run in the background

    def finish(p):
        sid, proc, seqs, path, t = p
        if proc.wait() != 0:
            raise RuntimeError(f"tippecanoe failed for {sid}")
        for s in seqs:
            s.unlink()
        rep["species"][sid].update(tiles_wall_s=round(time.time() - t, 1), mb=round(path.stat().st_size / 1e6, 1))

    with tempfile.TemporaryDirectory(dir=work) as tmp:
        for sid, sp in species.items():
            t = time.time()
            seqs, feats = [], 0
            for r in zones.REGION_ORDER:
                grid, forest = G[r]
                seq = Path(tmp) / f"{sid}-{r}.geojsonseq"
                st = zones.build_zones(regs[r].score(sp, date), grid, forest, r, seq, own=own[r],
                                       ooe_forest=ooe if r == "AT" else None, forest_cache=fcache[r], jobs=jobs)
                feats += st["features"]
                seqs.append(seq)
            rep["species"][sid] = {"zones_s": round(time.time() - t, 1), "features": feats}
            if pending:
                finish(pending)
                pending = None
            if all(s.stat().st_size == 0 for s in seqs):
                for s in seqs:
                    s.unlink()
                out[sid] = {"file": None}
                rep["species"][sid]["mb"] = 0
            else:
                rel = f"chance/{sid}-{stamp}.pmtiles"
                proc = subprocess.Popen(zones.tippecanoe_cmd(seqs, site / rel))
                pending = (sid, proc, seqs, site / rel, time.time())
                out[sid] = {"file": rel}
            log(f"{sid}: {json.dumps(rep['species'][sid])}")
        if pending:
            finish(pending)
    rep["s"] = round(time.time() - t0, 1)
    return out, rep


# ---------------------------------------------------------------- index + validation
def validate(site: Path, idx: dict, species_ids: list[str]) -> dict:
    errors = []
    for k in ("v", "generated", "weatherGenerated", "rainDays", "bounds", "species", "rain", "trees", "cells", "stations"):
        if k not in idx or idx[k] in (None, "", []):
            errors.append(f"index.json: missing {k}")
    if len(idx.get("rainDays") or []) != 2:
        errors.append("index.json: rainDays must be [from, to]")
    for sid in species_ids:
        if sid not in idx.get("species", {}) or "file" not in idx["species"][sid]:
            errors.append(f"index.json: species {sid} missing")
    for k, sub in (("rain", ("file", "periods", "levels")), ("trees", ("file",)), ("stations", ("file",)),
                   ("cells", ("dir", "step"))):
        for s in sub:
            if not (idx.get(k) or {}).get(s):
                errors.append(f"index.json: {k}.{s} missing")
    for rel in stamped_files(idx):
        if not (site / rel).is_file():
            errors.append(f"referenced file missing: {rel}")
    cells = (idx.get("cells") or {}).get("dir")
    if cells and not any((site / cells).glob("*.json")):
        errors.append(f"cells dir empty or missing: {cells}")
    total, n, big = 0, 0, []
    for p in site.rglob("*"):
        if p.is_file():
            sz = p.stat().st_size
            total += sz
            n += 1
            if sz >= MAX_FILE:
                big.append(f"{p.relative_to(site)} {sz / 1e6:.1f} MB")
    errors += [f"file too big (>= 95 MB): {b}" for b in big]
    if total >= MAX_SITE:
        errors.append(f"site too big: {total / 1e6:.1f} MB (>= 950 MB)")
    return {"errors": errors, "files": n, "mb": round(total / 1e6, 1)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site", required=True, help="output directory (deployed as the whole Pages site)")
    ap.add_argument("--force", action="store_true", help="publish even if the weather has not changed")
    ap.add_argument("--src", default=sio.SHROOMZN_DATA, help="shroomzn.app data source (URL or dir)")
    ap.add_argument("--live", default=os.environ.get("LIVE_URL", LIVE), help="the published site (previous index)")
    ap.add_argument("--rain-src", default=os.environ.get("RAIN_SRC"),
                    help="rain_daily source used when shroomzn.app has none and there is no previous publish")
    ap.add_argument("--grids", default=str(ROOT / "grids"), help="dir with AT/CH/DE_S/DE_M/DE_N.npz")
    ap.add_argument("--ooe", help="ooe_forest.fgb (default: <grids>/ooe_forest.fgb, else out/ooe_forest.fgb)")
    ap.add_argument("--date", help="scoring date YYYY-MM-DD (default: today in Vienna)")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    a = ap.parse_args(argv)
    T0 = time.time()
    site, grids = Path(a.site).resolve(), Path(a.grids).resolve()
    work = ROOT / "work"
    work.mkdir(parents=True, exist_ok=True)
    ooe = Path(a.ooe) if a.ooe else next((p for p in (grids / "ooe_forest.fgb", ROOT / "out" / "ooe_forest.fgb")
                                          if p.exists()), grids / "ooe_forest.fgb")
    rep: dict = {}

    # 1. weather + live index
    t = time.time()
    wx_dir = work / "wx"
    weather = model.Weather(fetch_weather(a.src, wx_dir))
    prev = live_index(a.live)
    rep["inputs_s"] = round(time.time() - t, 1)
    rep["weatherGenerated"] = weather.generated
    rep["previous"] = prev.get("generated") if prev else None
    if prev and prev.get("weatherGenerated") == weather.generated and not a.force:
        print(f"skip: weather {weather.generated} already published ({prev.get('generated')})")
        gh_output(skip="true")
        return 0
    gh_output(skip="false")
    for need in [grids / f"{r}.npz" for r in zones.REGION_ORDER] + [ooe]:
        if not need.exists():
            raise SystemExit(f"input missing: {need}")

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M")
    site.mkdir(parents=True, exist_ok=True)
    own_trees = sorted(site.glob("trees-*.pmtiles"), key=lambda p: p.stat().st_mtime)   # before the downloads

    # 2. previous publish
    if prev:
        rep["previous_files"] = fetch_previous(a.live, prev, site)

    # 3. rain + cells + stations
    t = time.time()
    if rain_available(a.src):
        rr = rain.build_rain(a.src, str(wx_dir), a.src, str(site), stamp)
        rain_idx, rep["rain"] = rr["index"], {"from": "shroomzn", "generated": rr.get("rainGenerated")}
    elif prev and prev.get("rain") and prev.get("cells") and prev.get("stations"):
        rain_idx = {k: prev[k] for k in ("rainDays", "rain", "cells", "stations")}
        rep["rain"] = {"from": "previous publish", "rainDays": prev["rainDays"]}
        log("rain: rain_daily not on shroomzn.app yet, reusing the previous publish's rain, cells and stations")
    elif a.rain_src:
        rr = rain.build_rain(a.rain_src, str(wx_dir), a.src, str(site), stamp)
        rain_idx, rep["rain"] = rr["index"], {"from": a.rain_src, "generated": rr.get("rainGenerated")}
    else:
        raise SystemExit("rain: rain_daily.json is not on shroomzn.app (404) and there is no previous publish to "
                         "reuse; pass --rain-src URL|DIR (or RAIN_SRC) with a rain_daily.json/.bin")
    rep["rain"]["s"] = round(time.time() - t, 1)

    # 4. chance zones
    date = dt.date.fromisoformat(a.date) if a.date else model.today_vienna()
    species, rep["chance"] = build_species(site, stamp, weather, grids, ooe, date, a.jobs, work)

    # 5. trees
    if own_trees:
        trees = {"file": own_trees[-1].name}
        rep["trees"] = {"from": "built", "file": trees["file"]}
    elif prev and (prev.get("trees") or {}).get("file"):
        trees = {"file": prev["trees"]["file"]}
        rep["trees"] = {"from": "previous publish", "file": trees["file"]}
    else:
        raise SystemExit("trees: no trees-*.pmtiles in the site and none published yet; run the trees workflow")

    # 6. index + validation
    idx = {"v": 1, "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "weatherGenerated": weather.generated, "rainDays": rain_idx["rainDays"], "bounds": BOUNDS,
           "species": species, "rain": rain_idx["rain"], "trees": trees, "cells": rain_idx["cells"],
           "stations": rain_idx["stations"]}
    (site / "index.json").write_text(json.dumps(idx, ensure_ascii=False, indent=1))
    v = validate(site, idx, list(model.load_species()))
    rep["site"] = {"files": v["files"], "mb": v["mb"], "stamp": stamp}
    rep["total_s"] = round(time.time() - T0, 1)
    print(json.dumps(rep, indent=1))
    if v["errors"]:
        for e in v["errors"]:
            log(f"VALIDATION: {e}")
        (site / "index.json").unlink()       # never deploy a broken index
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
