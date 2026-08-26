"""Curated proxy for the NATS HTTP monitoring endpoint (:8222/varz)."""
from __future__ import annotations

import asyncio
import json
import time
import urllib.request


MAX_MONITOR_RESPONSE_BYTES = 64 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None


_MONITOR_OPENER = urllib.request.build_opener(_NoRedirect)


async def varz(
    url: str, cache: dict, cache_s: float = 5.0, now: float | None = None
) -> dict:
    if now is None:
        now = time.time()
    if cache.get("val") is not None and now - cache.get("at", 0.0) < cache_s:
        return cache["val"]

    def _fetch() -> dict:
        with _MONITOR_OPENER.open(url.rstrip("/") + "/varz", timeout=3) as resp:
            body = resp.read(MAX_MONITOR_RESPONSE_BYTES + 1)
            if len(body) > MAX_MONITOR_RESPONSE_BYTES:
                raise ValueError("monitor response exceeds public proxy bound")
            value = json.loads(body.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("monitor response is not an object")
            return value

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
    except Exception as exc:
        # Monitoring output is browser-visible; retain a stable failure class,
        # never raw URLs, paths, credentials, or exception text.
        val = {"ok": False, "error": f"monitor_unavailable:{type(exc).__name__}"}
    cache["at"] = now
    cache["val"] = val
    return val
