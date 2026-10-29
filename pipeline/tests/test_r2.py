"""r2.py without network: plan/guards, prune selection, upload order (fake S3 client).
  .venv/bin/python -m pytest pipeline/tests/test_r2.py -q
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import r2  # noqa: E402


def index(stamp: str, rain_stamp: str | None = None, trees: str = "trees-1.pmtiles") -> dict:
    rs = rain_stamp or stamp
    return {"species": {"steinpilz": {"file": f"chance/steinpilz-{stamp}.pmtiles"}, "morchel": {"file": None}},
            "rain": {"file": f"rain-{rs}.pmtiles"}, "trees": {"file": trees},
            "cells": {"dir": f"cells-{rs}", "step": 1}, "stations": {"file": f"stations-{rs}.json"}}


def bucket_of(*idxs: dict, extra: tuple[str, ...] = ()) -> dict[str, int]:
    out = {"index.json": 1}
    for idx in idxs:
        for k in r2.stamped_files(idx):
            out[k] = 10
        out[f"{idx['cells']['dir']}/14_48.json"] = 5
        out[f"{idx['cells']['dir']}/15_48.json"] = 5
    for k in extra:
        out[k] = 1
    return out


def write_site(site: Path, idx: dict, size: int = 10):
    for k in r2.stamped_files(idx):
        (site / k).parent.mkdir(parents=True, exist_ok=True)
        (site / k).write_bytes(b"x" * size)
    (site / idx["cells"]["dir"]).mkdir(parents=True, exist_ok=True)
    (site / idx["cells"]["dir"] / "14_48.json").write_bytes(b"x" * 5)
    (site / "index.json").write_text("{}")


class FakeS3:
    def __init__(self, objects: dict[str, int], fail: str | None = None):
        self.objects = dict(objects)
        self.calls: list[tuple[str, str, dict]] = []
        self.fail = fail
        self.lock = threading.Lock()

    def upload_file(self, path, bucket, key, ExtraArgs=None, Config=None):
        if key == self.fail:
            raise OSError(f"upload of {key} failed")
        with self.lock:
            self.calls.append(("put", key, ExtraArgs or {}))
            self.objects[key] = Path(path).stat().st_size

    def list_objects_v2(self, Bucket, ContinuationToken=None, MaxKeys=2):     # 2 per page: tests the paging
        keys = sorted(self.objects)
        start = int(ContinuationToken or 0)
        page = keys[start:start + MaxKeys]
        more = start + MaxKeys < len(keys)
        return {"Contents": [{"Key": k, "Size": self.objects[k]} for k in page], "IsTruncated": more,
                **({"NextContinuationToken": str(start + MaxKeys)} if more else {})}

    def delete_objects(self, Bucket, Delete):
        for o in Delete["Objects"]:
            self.calls.append(("delete", o["Key"], {}))
            self.objects.pop(o["Key"])
        return {}


def test_prune_keeps_new_and_previous_index():
    old, prev, new = index("202610250730"), index("202610251330"), index("202610260730")
    objects = bucket_of(old, prev, new, extra=("site-junk.txt",))
    delete = r2.prune_keys(objects, [new, prev, None])
    assert set(delete) == ({"chance/steinpilz-202610250730.pmtiles", "rain-202610250730.pmtiles",
                            "stations-202610250730.json", "cells-202610250730/14_48.json",
                            "cells-202610250730/15_48.json", "site-junk.txt"})
    assert "index.json" not in delete and "trees-1.pmtiles" not in delete


def test_prune_keeps_reused_rain_of_an_older_publish():
    # rain_daily still 404: new and previous both reference the rain of an older publish
    prev, new = index("202610251330", "202610250730"), index("202610260730", "202610250730")
    objects = bucket_of(prev, new)
    assert r2.prune_keys(objects, [new, prev]) == []


def test_plan_reuses_bucket_objects_and_skips_same_size(tmp_path):
    prev = index("202610251330")
    new = index("202610260730", "202610251330")           # rain, cells, stations of the previous publish
    new["species"]["steinpilz"]["file"] = "chance/steinpilz-202610260730.pmtiles"
    (tmp_path / "chance").mkdir()
    (tmp_path / "chance/steinpilz-202610260730.pmtiles").write_bytes(b"x" * 10)
    (tmp_path / "trees-1.pmtiles").write_bytes(b"x" * 10)  # a trees file in site/ that is already up
    p = r2.plan(tmp_path, new, bucket_of(prev))
    assert p["errors"] == []
    assert p["upload"] == ["chance/steinpilz-202610260730.pmtiles"]


def test_plan_missing_file_and_guards(tmp_path, monkeypatch):
    new = index("202610260730")
    write_site(tmp_path, new)
    (tmp_path / "rain-202610260730.pmtiles").unlink()
    p = r2.plan(tmp_path, new, {})
    assert any("rain-202610260730.pmtiles" in e for e in p["errors"])
    write_site(tmp_path, new)
    assert r2.plan(tmp_path, new, {})["errors"] == []
    monkeypatch.setattr(r2, "MAX_FILE", 10)
    assert any("file too big" in e for e in r2.plan(tmp_path, new, {})["errors"])
    monkeypatch.setattr(r2, "MAX_FILE", 1000)
    monkeypatch.setattr(r2, "MAX_UPLOAD", 30)
    assert any("publish too big" in e for e in r2.plan(tmp_path, new, {})["errors"])
    monkeypatch.setattr(r2, "MAX_UPLOAD", 1000)
    monkeypatch.setattr(r2, "MAX_BUCKET", 100)
    assert any("bucket too big" in e for e in r2.plan(tmp_path, new, {"old.pmtiles": 90})["errors"])
    assert r2.plan(tmp_path, new, {"old.pmtiles": 90}, listed=False)["errors"] == []   # total unknown: no check


def test_plan_cells_must_exist_somewhere(tmp_path):
    new = index("202610260730", "202610250730")
    for k in r2.stamped_files(new):
        (tmp_path / k).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / k).write_bytes(b"x")
    assert any("cells dir" in e for e in r2.plan(tmp_path, new, {})["errors"])
    assert r2.plan(tmp_path, new, {"cells-202610250730/14_48.json": 5})["errors"] == []


def test_upload_index_last_with_headers(tmp_path):
    new = index("202610260730")
    write_site(tmp_path, new)
    s3 = FakeS3({})
    p = r2.plan(tmp_path, new, s3.objects)
    r2.upload(s3, "b", tmp_path, p["upload"])
    puts = [(k, x) for op, k, x in s3.calls if op == "put"]
    assert [k for k, _ in puts][-1] == "index.json"
    assert {k for k, _ in puts} == set(p["upload"]) | {"index.json"}
    hdr = dict(puts)
    assert hdr["index.json"]["CacheControl"] == r2.INDEX_CACHE
    assert hdr["index.json"]["ContentType"] == "application/json"
    assert hdr["rain-202610260730.pmtiles"] == {"ContentType": "application/vnd.pmtiles",
                                                "CacheControl": r2.IMMUTABLE}


def test_failed_upload_leaves_index_and_nothing_deleted(tmp_path):
    prev, new = index("202610251330"), index("202610260730")
    write_site(tmp_path, new)
    s3 = FakeS3(bucket_of(prev), fail="stations-202610260730.json")
    before = dict(s3.objects)
    p = r2.plan(tmp_path, new, s3.objects)
    try:
        r2.upload(s3, "b", tmp_path, p["upload"])
        r2.prune(s3, "b", [new, prev])
        raise AssertionError("upload should have failed")
    except OSError:
        pass
    assert "index.json" not in [k for _, k, _ in s3.calls]
    assert not [c for c in s3.calls if c[0] == "delete"]
    assert all(k in s3.objects for k in before)


def test_upload_then_prune(tmp_path):
    old, prev, new = index("202610250730"), index("202610251330"), index("202610260730")
    write_site(tmp_path, new)
    s3 = FakeS3(bucket_of(old, prev))
    assert r2.list_objects(s3, "b") == s3.objects                 # paged listing
    p = r2.plan(tmp_path, new, r2.list_objects(s3, "b"))
    r2.upload(s3, "b", tmp_path, p["upload"])
    deleted = r2.prune(s3, "b", [new, prev])
    first_delete = next(i for i, c in enumerate(s3.calls) if c[0] == "delete")
    assert s3.calls[first_delete - 1][1] == "index.json"         # prune only after index.json is up
    assert all(k.endswith("202610250730.pmtiles") or "202610250730" in k for k in deleted)
    assert set(s3.objects) == {"index.json", "trees-1.pmtiles"} | r2.index_keys(new, s3.objects) | \
        r2.index_keys(prev, s3.objects)
