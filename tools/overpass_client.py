"""Shared Overpass POST with public mirror fallback.

Public instances overload often (504 / read timeout). Callers should cache
successful JSON under ``data/raw/``. Override order with env
``AUTOROAD_OVERPASS_URLS`` (comma-separated interpreter URLs).
"""
from __future__ import annotations

import os
import time
from typing import Any

import requests

DEFAULT_URLS: tuple[str, ...] = (
    "https://overpass.private.coffee/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.openstreetmap.fr/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)

DEFAULT_HEADERS = {
    "User-Agent": "beamng_autoroad/0.1 (local test)",
    "Accept": "application/json",
}


def overpass_urls() -> list[str]:
    raw = (os.environ.get("AUTOROAD_OVERPASS_URLS") or "").strip()
    if raw:
        return [u.strip() for u in raw.split(",") if u.strip()]
    return list(DEFAULT_URLS)


def overpass_json(
    query: str,
    *,
    timeout_s: float = 120.0,
    pause_s: float = 2.0,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """POST Overpass QL; try mirrors until one returns JSON. Raise on total failure."""
    hdrs = dict(DEFAULT_HEADERS)
    if headers:
        hdrs.update(headers)
    last_err: Exception | None = None
    urls = overpass_urls()
    for i, url in enumerate(urls):
        try:
            print(f"Overpass try {i + 1}/{len(urls)}: {url}")
            r = requests.post(url, data={"data": query}, headers=hdrs, timeout=timeout_s)
            r.raise_for_status()
            data = r.json()
            print(f"Overpass OK ({len(r.content)} bytes)")
            return data
        except Exception as ex:  # noqa: BLE001
            last_err = ex
            print(f"Overpass failed at {url}: {ex}")
            if pause_s > 0 and i + 1 < len(urls):
                time.sleep(pause_s)
    raise RuntimeError(f"Overpass failed on all mirrors: {last_err}")
