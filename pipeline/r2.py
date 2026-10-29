"""Cloudflare R2 (S3 API) for publish.py: what a publish uploads, the guards, upload (index.json last), prune.

Environment: R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY (required to upload), R2_ACCOUNT_ID or R2_ENDPOINT, R2_BUCKET.
Objects sit at the bucket root under the paths of docs/format.md (index.json, chance/<id>-<stamp>.pmtiles, ...).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ACCOUNT = "32744ef90c5fd4c66c8f20de5dd99e60"
BUCKET = "schwammerl-data"
INDEX = "index.json"
IMMUTABLE = "public, max-age=31536000, immutable"   # stamped files never change under their name
INDEX_CACHE = "no-cache"                             # the app must see a new index.json right away
MAX_FILE = 500 * 1000 ** 2       # sanity cap per file (largest today: trees ~95 MB)
MAX_UPLOAD = 3 * 1000 ** 3       # new files of one publish (normal: ~0.8 GB)
MAX_BUCKET = 8 * 1000 ** 3       # bucket total while old and new are both there (free tier: 10 GB)
CONTENT_TYPES = {".pmtiles": "application/vnd.pmtiles", ".json": "application/json"}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def has_credentials() -> bool:
    return bool(os.environ.get("R2_ACCESS_KEY_ID") and os.environ.get("R2_SECRET_ACCESS_KEY"))


def bucket_name() -> str:
    return os.environ.get("R2_BUCKET") or BUCKET


def client():
    import boto3
    from botocore.config import Config
    if not has_credentials():
        raise SystemExit("R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY not set")
    endpoint = os.environ.get("R2_ENDPOINT") or \
        f"https://{os.environ.get('R2_ACCOUNT_ID') or ACCOUNT}.r2.cloudflarestorage.com"
    # R2 rejects some of the newer default checksum headers of boto3; only send them where the API needs them
    cfg = Config(retries={"max_attempts": 5, "mode": "standard"}, max_pool_connections=16,
                 request_checksum_calculation="when_required", response_checksum_validation="when_required")
    return boto3.client("s3", endpoint_url=endpoint, region_name="auto", config=cfg,
                        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"])


# ---------------------------------------------------------------- what an index references
def stamped_files(idx: dict) -> list[str]:
    """the single files an index.json references (cells dir separately)"""
    out = [v["file"] for v in idx.get("species", {}).values() if v.get("file")]
    for k in ("rain", "trees", "stations"):
        if (idx.get(k) or {}).get("file"):
            out.append(idx[k]["file"])
    return out


def cells_dir(idx: dict) -> str | None:
    return (idx.get("cells") or {}).get("dir")


def index_keys(idx: dict, objects) -> set[str]:
    """bucket keys an index references: its files plus every object under its cells dir"""
    keys = set(stamped_files(idx))
    cells = cells_dir(idx)
    if cells:
        keys |= {k for k in objects if k.startswith(f"{cells}/")}
    return keys


def local_files(site: Path, idx: dict) -> dict[str, int]:
    """{key: size} of the referenced files that this run built into site/ (index.json not included)"""
    out = {rel: (site / rel).stat().st_size for rel in stamped_files(idx) if (site / rel).is_file()}
    cells = cells_dir(idx)
    if cells and (site / cells).is_dir():
        out |= {f"{cells}/{p.name}": p.stat().st_size for p in (site / cells).glob("*.json")}
    return out


# ---------------------------------------------------------------- bucket state
def list_objects(s3, bucket: str) -> dict[str, int]:
    out, token = {}, None
    while True:
        r = s3.list_objects_v2(Bucket=bucket, **({"ContinuationToken": token} if token else {}))
        for o in r.get("Contents", []):
            out[o["Key"]] = o["Size"]
        if not r.get("IsTruncated"):
            return out
        token = r["NextContinuationToken"]


def read_index(s3, bucket: str, objects: dict) -> dict | None:
    if INDEX not in objects:
        return None
    return json.loads(s3.get_object(Bucket=bucket, Key=INDEX)["Body"].read())


def head_size(url: str, user_agent: str) -> int | None:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return int(r.headers.get("Content-Length") or 0)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def probe_live(live: str, keys: list[str], cell_keys: list[str], user_agent: str) -> dict[str, int]:
    """dry run without credentials: {key: size} of keys found on the public URL (HEAD), instead of a bucket
    listing. Of cell_keys only the first one found is probed (enough to know the cells dir is there)."""
    base, out = live.rstrip("/"), {}
    for k in keys:
        if (n := head_size(f"{base}/{k}", user_agent)) is not None:
            out[k] = n
    for k in cell_keys:
        if (n := head_size(f"{base}/{k}", user_agent)) is not None:
            out[k] = n
            break
    return out


# ---------------------------------------------------------------- plan + guards
def plan(site: Path, idx: dict, objects: dict[str, int], listed: bool = True) -> dict:
    """What to upload and whether the publish is safe. objects = what is in the bucket (listed=False: only the
    referenced keys probed on the public URL, so the bucket total is unknown and not checked)."""
    local = local_files(site, idx)
    errors = []
    for rel in stamped_files(idx):
        if rel not in local and rel not in objects:
            errors.append(f"referenced file missing (not built, not in the bucket): {rel}")
    cells = cells_dir(idx)
    if cells and not any(k.startswith(f"{cells}/") for k in (*local, *objects)):
        errors.append(f"cells dir empty or missing: {cells}")
    errors += [f"file too big (>= {MAX_FILE / 1e6:.0f} MB): {k} {n / 1e6:.1f} MB"
               for k, n in sorted(local.items()) if n >= MAX_FILE]
    upload = sorted(k for k, n in local.items() if objects.get(k) != n)    # same key and size: already there
    up_bytes = sum(local[k] for k in upload)
    if up_bytes >= MAX_UPLOAD:
        errors.append(f"publish too big: {up_bytes / 1e6:.1f} MB to upload (>= {MAX_UPLOAD / 1e6:.0f} MB)")
    peak = None
    if listed:      # old and new objects are both in the bucket until the prune
        peak = sum(n for k, n in objects.items() if k not in local) + sum(local.values())
        if peak >= MAX_BUCKET:
            errors.append(f"bucket too big after the upload: {peak / 1e6:.1f} MB (>= {MAX_BUCKET / 1e6:.0f} MB)")
    refs = index_keys(idx, {**objects, **local})
    return {"upload": upload, "upload_mb": round(up_bytes / 1e6, 1), "files": len(refs),
            "reused": len(refs) - len(upload), "bucket_mb": None if peak is None else round(peak / 1e6, 1),
            "errors": errors}


def prune_keys(objects, keep_indexes: list[dict]) -> list[str]:
    """keys to delete: everything not referenced by one of keep_indexes (never index.json)"""
    keep = {INDEX}
    for idx in keep_indexes:
        if idx:
            keep |= index_keys(idx, objects)
    return sorted(k for k in objects if k not in keep)


# ---------------------------------------------------------------- upload + prune
def upload(s3, bucket: str, site: Path, keys: list[str], jobs: int = 8):
    """stamped files (parallel, multipart for big ones) first, index.json last; raises on the first failure, in
    which case index.json is not uploaded and the live index keeps pointing at the old files."""
    from boto3.s3.transfer import TransferConfig
    tc = TransferConfig(multipart_threshold=64 * 1024 ** 2, multipart_chunksize=32 * 1024 ** 2, max_concurrency=4)

    def one(key: str, cache: str):
        extra = {"ContentType": CONTENT_TYPES.get(Path(key).suffix, "application/octet-stream"), "CacheControl": cache}
        s3.upload_file(str(site / key), bucket, key, ExtraArgs=extra, Config=tc)

    with ThreadPoolExecutor(jobs) as ex:
        list(ex.map(lambda k: one(k, IMMUTABLE), [k for k in keys if k != INDEX]))
    one(INDEX, INDEX_CACHE)


def prune(s3, bucket: str, keep_indexes: list[dict]) -> list[str]:
    """after index.json is up: delete what neither the new nor the previous index references"""
    delete = prune_keys(list_objects(s3, bucket), keep_indexes)
    for i in range(0, len(delete), 1000):
        r = s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in delete[i:i + 1000]],
                                                     "Quiet": True})
        for e in r.get("Errors", []):
            log(f"prune: {e.get('Key')} not deleted: {e.get('Code')} {e.get('Message')}")
    return delete
