"""Curated proxy for the NATS HTTP monitoring endpoint (:8222/varz)."""
from __future__ import annotations

import asyncio
import json
import time
import urllib.request


async def varz(
    url: str, cache: dict, cache_s: float = 5.0, now: float | None = None
) -> dict:
    if now is None:
        now = time.time()
    if cache.get("val") is not None and now - cache.get("at", 0.0) < cache_s:
        return cache["val"]

    def _fetch() -> dict:
        with urllib.request.urlopen(url.rstrip("/") + "/varz", timeout=3) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        raw = await asyncio.to_thread(_fetch)
        val = {
            "ok": True,
            "server_version": raw.get("version"),
            "uptime": raw.get("uptime"),
            "connections": raw.get("connections"),
            "in_msgs": raw.get("in_msgs"),
            "out_msgs": raw.get("out_msgs"),
            "slow_consumers": raw.get("slow_consumers"),
            "mem": raw.get("mem"),
            "cpu": raw.get("cpu"),
        }
    except Exception as e:
        val = {"ok": False, "error": str(e)[:300]}
    cache["at"] = now
    cache["val"] = val
    return val
