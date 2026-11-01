"""Mỗi request một dòng; request bị từ chối mang mã lỗi trên chính dòng đó; lỗi lạ có một dòng ERROR kèm stack."""

import logging

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from common.core.response_middleware import exception_handler
from common.telemetry import RequestLineMiddleware


@pytest.fixture
def client():
    app = FastAPI()
    app.add_middleware(RequestLineMiddleware, logger_name="main.request")
    app.add_exception_handler(StarletteHTTPException, exception_handler.http_exception_handler)
    app.add_exception_handler(Exception, exception_handler.global_exception_handler)

    @app.get("/api/v1/datasource/get/{ds_id}")
    def get_ds(ds_id: int):
        return {"id": ds_id}

    @app.get("/api/v1/denied")
    def denied():
        raise HTTPException(status_code=403, detail="No permission")

    @app.get("/api/v1/boom")
    def boom():
        raise RuntimeError("secret detail")

    @app.get("/healthz")
    def healthz():
        return {}

    return TestClient(app, raise_server_exceptions=False)


def _lines(caplog):
    return [r for r in caplog.records if r.name == "main.request"]


def test_a_request_is_one_sentence_line_with_its_id(client, caplog):
    with caplog.at_level(logging.INFO):
        client.get("/api/v1/datasource/get/7")

    (line,) = _lines(caplog)
    assert line.levelno == logging.INFO
    assert "ds_id 7" in line.getMessage()
    assert getattr(line, "http.response.status_code") == 200


def test_a_rejected_request_is_a_warning_with_its_code(client, caplog):
    with caplog.at_level(logging.INFO):
        client.get("/api/v1/denied")

    (line,) = _lines(caplog)
    assert line.levelno == logging.WARNING
    assert (line.error_code, line.error_message) == ("http_error", "No permission")


def test_an_unexpected_error_has_one_error_line_with_a_stack_and_never_the_message(client, caplog):
    with caplog.at_level(logging.INFO):
        client.get("/api/v1/boom")

    (line,) = _lines(caplog)
    assert (line.levelno, getattr(line, "http.response.status_code")) == (logging.ERROR, 500)
    stacks = [r for r in caplog.records if r.name != "main.request" and r.exc_info]
    assert len(stacks) == 1
    assert stacks[0].error == "RuntimeError"
    assert "secret detail" not in stacks[0].getMessage()


def test_a_probe_has_no_line(client, caplog):
    with caplog.at_level(logging.INFO):
        client.get("/healthz")

    assert _lines(caplog) == []
