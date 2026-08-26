"""Regression tests for the bounded, non-following NATS monitor proxy."""

from __future__ import annotations

import json
import urllib.request

import pytest

from hub import monitor


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.read_sizes: list[int] = []

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        del args

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self.body if size < 0 else self.body[:size]


class _Opener:
    def __init__(self, body: bytes) -> None:
        self.response = _Response(body)
        self.urls: list[tuple[str, int]] = []

    def open(self, url: str, timeout: int):
        self.urls.append((url, timeout))
        return self.response


@pytest.mark.asyncio
async def test_monitor_reads_only_one_byte_beyond_public_size_cap(monkeypatch):
    opener = _Opener(b"x" * (monitor.MAX_MONITOR_RESPONSE_BYTES + 1))
    monkeypatch.setattr(monitor, "_MONITOR_OPENER", opener)

    result = await monitor.varz("http://monitor.invalid/", {}, now=10.0)

    assert result == {"ok": False, "error": "monitor_unavailable:ValueError"}
    assert opener.response.read_sizes == [monitor.MAX_MONITOR_RESPONSE_BYTES + 1]
    assert opener.urls == [("http://monitor.invalid/varz", 3)]


@pytest.mark.asyncio
async def test_monitor_rejects_valid_json_that_is_not_an_object(monkeypatch):
    opener = _Opener(json.dumps([{"version": "must-not-pass"}]).encode())
    monkeypatch.setattr(monitor, "_MONITOR_OPENER", opener)

    result = await monitor.varz("http://monitor.invalid", {}, now=10.0)

    assert result == {"ok": False, "error": "monitor_unavailable:ValueError"}
    assert "must-not-pass" not in json.dumps(result)


@pytest.mark.asyncio
async def test_monitor_projects_only_allowlisted_fields_from_json_object(monkeypatch):
    opener = _Opener(
        json.dumps(
            {
                "version": "2.11.0",
                "uptime": "1h",
                "connections": 3,
                "in_msgs": 5,
                "out_msgs": 4,
                "slow_consumers": 0,
                "mem": 1024,
                "cpu": 0.5,
                "authorization": "must-not-be-proxied",
            }
        ).encode()
    )
    monkeypatch.setattr(monitor, "_MONITOR_OPENER", opener)

    result = await monitor.varz("http://monitor.invalid", {}, now=10.0)

    assert result == {
        "ok": True,
        "server_version": "2.11.0",
        "uptime": "1h",
        "connections": 3,
        "in_msgs": 5,
        "out_msgs": 4,
        "slow_consumers": 0,
        "mem": 1024,
        "cpu": 0.5,
    }
    assert "authorization" not in result


def test_monitor_opener_replaces_the_default_redirect_handler():
    redirect_handlers = [
        handler
        for handler in monitor._MONITOR_OPENER.handlers
        if isinstance(handler, urllib.request.HTTPRedirectHandler)
    ]

    assert len(redirect_handlers) == 1
    handler = redirect_handlers[0]
    assert type(handler) is monitor._NoRedirect
    assert (
        handler.redirect_request(None, None, 302, "Found", {}, "/elsewhere") is None
    )
