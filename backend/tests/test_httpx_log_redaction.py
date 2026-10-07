"""Log của httpx không được chứa chữ ký presigned URL."""

import logging

import httpx

from common.utils.utils import _StripUrlQueryFilter


def _record(*args) -> logging.LogRecord:
    return logging.LogRecord("httpx", logging.INFO, __file__, 1,
                             'HTTP Request: %s %s "%s %d %s"', args, None)


def test_cat_query_cua_url_httpx():
    url = httpx.URL("https://minio.local/bucket/a.xlsx?X-Amz-Signature=BIMAT&X-Amz-Expires=300")
    record = _record("GET", url, "HTTP/1.1", 200, "OK")

    assert _StripUrlQueryFilter().filter(record) is True
    message = record.getMessage()

    assert "BIMAT" not in message
    assert "https://minio.local/bucket/a.xlsx?<redacted>" in message
    assert '"HTTP/1.1 200 OK"' in message


def test_url_khong_query_giu_nguyen():
    url = httpx.URL("https://api.local/v1/chat/completions")
    record = _record("POST", url, "HTTP/1.1", 200, "OK")

    _StripUrlQueryFilter().filter(record)

    assert record.args[1] is url
