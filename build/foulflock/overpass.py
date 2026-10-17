"""Overpass access: mirror rotation with backoff, the response cache, and
the ALPR camera query."""

from __future__ import annotations

import json
import sys
import time
import urllib.request

from . import config


# --------------------------------------------------------------- fetch ----

def http_post_overpass(data: str, retries: int = 10,
                       timeout: int = 90) -> bytes:
    """POST to Overpass, rotating across mirrors with backoff on 429/504."""
    for attempt in range(retries):
        url = config.OVERPASS_URLS[attempt % len(config.OVERPASS_URLS)]
        try:
            req = urllib.request.Request(
                url,
                data=("data=" + urllib.parse.quote(data)).encode(),
                headers={"User-Agent": "alpr-siting-compliance-map/0.1 (advocacy research tool)"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
            # A timed-out or out-of-memory query still answers 200, with the
            # failure in a top-level "remark" beside a partial result.
            remark = json.loads(body).get("remark", "")
            if "runtime error" in remark:
                raise RuntimeError("Overpass runtime error (partial result)")
            return body
        except Exception as e:  # noqa: BLE001
            if attempt == retries - 1:
                raise
            wait = min(15 * (attempt + 1), 45)
            host = urllib.parse.urlparse(url).netloc
            print(f"    {host} failed ({e}); retrying in {wait}s")
            time.sleep(wait)
    raise RuntimeError("unreachable")


def overpass_query(cache_key: str, query: str, offline: bool,
                   refresh_scope: str | tuple[str, ...] | None = "cameras",
                   required: bool = True, timeout: int = 90) -> dict | None:
    """Run an Overpass query with on-disk caching.

    refresh_scope: the --refresh scope(s) that invalidate this cache entry
    (None = never; used for historical as-of snapshots, which can't change).
    required: when False, an offline cache miss returns None instead of
    exiting, for optional guideline data.
    timeout: client read timeout in seconds (heavy queries need more).
    """
    cache_file = config.CACHE_DIR / f"{cache_key}.json"
    scopes = ({refresh_scope} if isinstance(refresh_scope, str)
              else set(refresh_scope or ()))
    refresh = bool(scopes & set(config.REFRESH_SCOPES))
    if cache_file.exists() and not refresh:
        print(f"    cache hit: {cache_file.name}")
        return json.loads(cache_file.read_text(encoding="utf-8"))
    if offline:
        if not required:
            print(f"    no cache for {cache_key} (offline); skipping")
            return None
        sys.exit(f"--offline set but no cache for {cache_key}")
    print(f"    querying Overpass: {cache_key} ...")
    raw = http_post_overpass(query, timeout=timeout)
    data = json.loads(raw)
    config.CACHE_DIR.mkdir(exist_ok=True)
    cache_file.write_text(json.dumps(data), encoding="utf-8")
    time.sleep(10)  # be polite to the public endpoints
    return data


def fetch_cameras(state_code: str, offline: bool) -> list[dict]:
    """ALPR camera nodes in a state (DeFlock tagging convention)."""
    q = f"""
[out:json][timeout:180];
area["ISO3166-2"="US-{state_code}"][admin_level=4]->.st;
node["man_made"="surveillance"]["surveillance:type"="ALPR"](area.st);
out meta;
"""
    data = overpass_query(f"cameras_{state_code}", q, offline)
    cams = []
    for el in data.get("elements", []):
        if el.get("type") != "node":
            continue
        tags = el.get("tags", {})
        cams.append({
            "camera_id": f"osm-node-{el['id']}",
            "osm_id": el["id"],
            "lat": el["lat"],
            "lon": el["lon"],
            "operator": tags.get("operator"),
            "brand": tags.get("brand") or tags.get("manufacturer"),
            "direction": tags.get("camera:direction") or tags.get("direction"),
            # UTC date of the node's last OSM edit; caches from before
            # `out meta` lack it until the next cameras refresh.
            "last_edited": (el.get("timestamp") or "")[:10] or None,
            "state_code": state_code,
        })
    return cams
