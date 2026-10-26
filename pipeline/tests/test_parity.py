"""Parity of pipeline/model.py with the reference model (shroomzn habitat/pilz.js, run in Node).

Needs node and the reference checkout ($SHROOMZN_DIR, with habitat/pilz.js); skipped without them.
  .venv/bin/python -m pytest pipeline/tests/test_parity.py -s
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import model  # noqa: E402

DEFAULT_REF = ("/private/tmp/claude-501/-Users-martingatzweiler-Developer-Schwammerl/"
               "c7047b06-8223-4110-9fde-2f19ceea38f4/scratchpad/shroomzn")
REF = Path(os.environ.get("SHROOMZN_DIR", DEFAULT_REF))
PILZ_JS = REF / "habitat" / "pilz.js"
DATES = [dt.date(2026, 10, 26), dt.date(2026, 4, 12), dt.date(2026, 7, 31)]
CELLS = {"AT": 3000, "CH": 3000, "DE_S": 2000, "DE_M": 2000, "DE_N": 2000}     # 12 000 random forest cells
TOL = 1e-6

pytestmark = pytest.mark.skipif(not PILZ_JS.exists() or shutil.which("node") is None,
                                reason="reference pilz.js ($SHROOMZN_DIR) or node missing")


def pack(f: dict, idx) -> np.ndarray:
    """the reference tile layout (habitat/build.py FORMAT): 6 byte planes A.R A.G A.B B.R B.G B.B"""
    g = {k: f[k][idx].astype(np.uint8) for k in model.SITE_KEYS}
    return np.stack([g["g1"] | (g["g2"] << 3) | (g["share"] << 6), g["forest"] | (g["slope"] << 4), g["elev15"],
                     g["aspect"] | (g["wet"] << 4), g["rock"] | (g["ph"] << 3) | (g["texture"] << 6), g["stream"]])


def b64(a: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(a, np.uint8).tobytes()).decode()


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("parity")
    w = model._weather(None)
    species = model.load_species()
    ids = list(species)
    rng = np.random.default_rng(7)
    cell_site, cell_wx, ours_cells, patches, ours_patch = [], [], [], [], []
    for region, n in CELLS.items():
        reg = model.Region(region, w)
        _, f, _ = model.load_grid(region)
        f = {k: v.ravel() for k, v in f.items()}
        h, wd = reg.shape
        # random cells
        pick = rng.choice(len(reg.cells), n, replace=False)
        cell_site.append(pack(f, reg.cells[pick]))
        cell_wx.append(w.bytes[:, reg.widx[pick]])
        ours_cells.append({(d, sid): reg.raw(species[sid], d, np.float64)[pick] for d in DATES for sid in ids})
        # one 256 x 256 window around a random forest cell
        k = reg.cells[rng.integers(len(reg.cells))]
        r0 = int(np.clip(k // wd - 128, 0, h - 256)); c0 = int(np.clip(k % wd - 128, 0, wd - 256))
        win = ((np.arange(256)[:, None] + r0) * wd + (np.arange(256)[None, :] + c0)).ravel()
        pos = np.searchsorted(reg.cells, win)
        inreg = (pos < len(reg.cells)) & (reg.cells[np.minimum(pos, len(reg.cells) - 1)] == win)
        wx = np.full((len(w.planes), 65536), 255, np.uint8)
        wx[:, inreg] = w.bytes[:, reg.widx[pos[inreg]]]
        patches.append({"site": b64(pack(f, win)), "wx": b64(wx)})
        res = {}
        for d in DATES:
            for sid in ids:
                raw = reg.raw(species[sid], d)
                res[(d, sid)] = (reg.to_grid(raw).ravel()[win], reg.to_grid(reg.smooth(raw)).ravel()[win])
        ours_patch.append(res)
        del reg, f
    nc = sum(CELLS.values())
    site = np.zeros((6, 65536), np.uint8)
    site[:, :nc] = np.concatenate(cell_site, axis=1)
    inp = {"planes": w.doc["planes"], "atOwnHeight": w.at_own_height, "dates": [[d.year, d.month, d.day] for d in DATES],
           "species": ids, "cells": {"n": nc, "site": b64(site), "wx": b64(np.concatenate(cell_wx, axis=1))},
           "patches": patches}
    (tmp / "in.json").write_text(json.dumps(inp))
    out = tmp / "out"
    out.mkdir()
    subprocess.run(["node", str(HERE / "parity_harness.mjs"), str(PILZ_JS), str(tmp / "in.json"), str(out)],
                   check=True, timeout=600)
    return {"out": out, "ids": ids, "nc": nc, "cells": ours_cells, "patch": ours_patch}


def test_species_json_matches_reference(tmp_path):
    dump = tmp_path / "species.json"
    subprocess.run(["node", str(REF / "habitat" / "species_dump.mjs"), str(PILZ_JS), str(dump)], check=True,
                   capture_output=True)
    assert json.loads(dump.read_text()) == model.load_species(), "pipeline/species.json is out of date"
    assert len(model.load_species()) == 20


def test_cells_score_and_level(run):
    out, worst, nonzero = run["out"], 0.0, 0
    ours = {key: np.concatenate([c[key] for c in run["cells"]]) for key in run["cells"][0]}
    for (d, sid), mine in ours.items():
        js = np.fromfile(out / f"score_{d}_{sid}.f64", np.float64)
        lv = np.fromfile(out / f"level_{d}_{sid}.u8", np.uint8)
        assert len(js) == run["nc"] >= 10_000
        diff = float(np.max(np.abs(js - mine)))
        worst = max(worst, diff)
        nonzero += int((js > 0).sum())
        assert diff <= TOL, f"{sid} {d}: max diff {diff}"
        assert np.array_equal(3 - lv.astype(np.int16), model.levels(mine)), f"{sid} {d}: levels differ"
    print(f"\ncells: {run['nc']} x {len(run['ids'])} species x {len(DATES)} dates, "
          f"{nonzero} scores > 0, max |diff| {worst:.3g}")


def test_patches_raw_and_neighbour_mean(run):
    out, worst_raw, worst_mean, n = run["out"], 0.0, 0.0, 0
    inner = np.zeros((256, 256), bool)
    inner[1:-1, 1:-1] = True                       # the tile edge has no neighbours outside in the reference
    inner = inner.ravel()
    for pi, res in enumerate(run["patch"]):
        for (d, sid), (raw, mean) in res.items():
            jr = np.fromfile(out / f"patch{pi}_{d}_{sid}.raw.f32", np.float32)
            js = np.fromfile(out / f"patch{pi}_{d}_{sid}.shown.f32", np.float32)
            jl = np.fromfile(out / f"patch{pi}_{d}_{sid}.lv.u8", np.uint8)
            have = jr >= 0
            assert np.array_equal(have, ~np.isnan(raw)), f"patch {pi} {sid}: scored cells differ"
            m = have & inner
            n += int(m.sum())
            worst_raw = max(worst_raw, float(np.max(np.abs(jr[have] - raw[have]), initial=0)))
            worst_mean = max(worst_mean, float(np.max(np.abs(js[m] - mean[m]), initial=0)))
            assert worst_raw <= TOL and worst_mean <= TOL, f"patch {pi} {sid} {d}"
            assert np.array_equal(jl[m].astype(np.int16) - 1, model.levels(mean[m])), f"patch {pi} {sid} {d}: levels"
    assert n > 0
    print(f"\npatches: {len(run['patch'])} x 256^2, {n} inner forest cell-evaluations, "
          f"max |diff| raw {worst_raw:.3g}, 3x3 mean {worst_mean:.3g}")
