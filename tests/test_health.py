"""Test route /healthz và /readyz: liveness không làm I/O, readiness phản ánh dependency và ngân sách thời gian."""

import asyncio
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from common.core import health
from common.core.config import settings
from common.utils.whitelist import whiteUtils


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(health.router)
    return TestClient(app)


def _fail():
    raise RuntimeError("postgresql://user:secret@host/db")


def test_probe_bodies_are_not_wrapped_in_the_api_envelope(monkeypatch):
    from common.core.response_middleware import ResponseMiddleware

    app = FastAPI()
    app.add_middleware(ResponseMiddleware)
    app.include_router(health.router)
    monkeypatch.setattr(health, "readiness_checks", lambda: {"database": lambda: health.CheckResult(ok=True)})
    probe = TestClient(app)
    assert probe.get("/healthz").json() == {"status": "ok"}
    assert probe.get("/readyz").json()["status"] == "ok"


def test_healthz_does_no_io(client, monkeypatch):
    monkeypatch.setattr(health, "readiness_checks", lambda: {"database": _fail})
    response = client.get("/healthz")
    assert (response.status_code, response.json()) == (200, {"status": "ok"})


def test_readyz_ok_when_every_check_passes(client, monkeypatch):
    monkeypatch.setattr(health, "readiness_checks", lambda: {"database": lambda: health.CheckResult(ok=True)})
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["checks"]["database"] == {"ok": True, "detail": None}


def test_readyz_503_names_the_failure_without_leaking_the_message(client, monkeypatch):
    monkeypatch.setattr(health, "readiness_checks", lambda: {"database": _fail})
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["database"] == {"ok": False, "detail": "RuntimeError"}
    assert "secret" not in response.text


def test_readyz_503_when_a_check_overruns_the_budget(client, monkeypatch):
    async def slow():
        await asyncio.sleep(5)

    monkeypatch.setattr(health, "READINESS_BUDGET_SECONDS", 0.05)
    monkeypatch.setattr(health, "readiness_checks", lambda: {"redis": slow})
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["redis"]["detail"] == "Timeout"


@pytest.mark.parametrize("cache_type, expected", [("redis", {"database", "redis"}), ("memory", {"database"})])
def test_redis_is_checked_only_when_the_cache_uses_it(monkeypatch, cache_type, expected):
    monkeypatch.setattr(settings, "CACHE_TYPE", cache_type)
    assert set(health.readiness_checks()) == expected


def test_probe_paths_skip_the_token_middleware():
    assert whiteUtils.is_whitelisted("/healthz") and whiteUtils.is_whitelisted("/readyz")


def test_quiet_filter_drops_only_successful_probe_hits():
    def record(path, status):
        return logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                                 ("1.2.3.4:5", "GET", path, "1.1", status), None)

    quiet = health.QuietProbeAccessLog()
    assert not quiet.filter(record("/healthz", 200))
    assert quiet.filter(record("/readyz", 503))
    assert quiet.filter(record("/api/v1/user", 200))
