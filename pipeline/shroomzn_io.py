"""Readers for the public files of shroomzn.app/data (or a local directory with the same files).

A *source* is either a base URL (``https://shroomzn.app/data``) or a local directory; every reader
takes it as a parameter, so the daily job can switch between the live site and local copies.
"""
from __future__ import annotations

import json
import os
import urllib.request
import zlib

import numpy as np

SHROOMZN_DATA = "https://shroomzn.app/data"
USER_AGENT = "schwammerl-data pipeline (+https://github.com/MrGatzi/schwammerl-data)"

# the ~1 km lat/lon weather grid shared by pilz_weather and rain_daily (row 0 = north)
GRID_SOUTH, GRID_WEST, GRID_NORTH, GRID_EAST = 45.75, 5.8, 55.1, 17.3
GRID_NX, GRID_NY = 920, 1039
GRID_DLON, GRID_DLAT = 0.0125, 0.009

STATION_FILES = {"at": "stations.json", "ch": "ch_stations.json", "de": "de_stations.json", "cz": "cz_stations.json"}


def _is_url(src: str) -> bool:
    return src.startswith(("http://", "https://"))


def read_bytes(src: str, name: str, timeout: float = 120) -> bytes:
    if _is_url(src):
        req = urllib.request.Request(f"{src.rstrip('/')}/{name}", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    with open(os.path.join(src, name), "rb") as f:
        return f.read()


def read_json(src: str, name: str):
    return json.loads(read_bytes(src, name).decode("utf-8"))


def grid_centers():
    """1-D lon (west->east) and lat (north->south) of the cell centres."""
    lon = GRID_WEST + (np.arange(GRID_NX) + 0.5) * GRID_DLON
    lat = GRID_NORTH - (np.arange(GRID_NY) + 0.5) * GRID_DLAT
    return lon, lat


def _check_grid(meta: dict, name: str):
    if (meta.get("nx"), meta.get("ny")) != (GRID_NX, GRID_NY) or meta.get("bounds") != [[GRID_SOUTH, GRID_WEST], [GRID_NORTH, GRID_EAST]]:
        raise ValueError(f"{name}: unexpected grid {meta.get('bounds')} {meta.get('nx')}x{meta.get('ny')}")


def read_rain_daily(src: str = SHROOMZN_DATA):
    """-> (meta, days, mm) with mm float32 [days, ny, nx] in mm, NaN = no data, oldest day first."""
    meta = read_json(src, "rain_daily.json")
    _check_grid(meta, "rain_daily")
    days = list(meta["days"])
    raw = np.frombuffer(zlib.decompress(read_bytes(src, meta.get("file", "rain_daily.bin"))), dtype="<u2")
    raw = raw.reshape(len(days), GRID_NY, GRID_NX)
    mm = np.where(raw == 65535, np.nan, raw.astype(np.float32) / 10).astype(np.float32)
    return meta, days, mm


def read_pilz_weather(src: str = SHROOMZN_DATA):
    """-> (meta, {plane name: float32 [ny, nx], NaN = no data}); value = byte / scale - offset, 255 = no data."""
    meta = read_json(src, "pilz_weather.json")
    _check_grid(meta, "pilz_weather")
    raw = np.frombuffer(zlib.decompress(read_bytes(src, meta.get("file", "pilz_weather.bin"))), dtype=np.uint8)
    raw = raw.reshape(len(meta["planes"]), GRID_NY, GRID_NX)
    planes = {}
    for i, p in enumerate(meta["planes"]):
        b = raw[i]
        planes[p["name"]] = np.where(b == 255, np.nan, b.astype(np.float32) / p["scale"] - p["offset"]).astype(np.float32)
    return meta, planes


def read_station_files(src: str = SHROOMZN_DATA):
    """-> {country prefix: station doc} for the files that could be read (format: his precompute.station_doc)."""
    docs = {}
    for cc, name in STATION_FILES.items():
        try:
            docs[cc] = read_json(src, name)
        except Exception as e:                   # one missing country must not stop the others
            print(f"stations: {name} not read ({type(e).__name__}: {e})")
    return docs
