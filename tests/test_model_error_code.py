"""Test mã lỗi ``model_*`` cho ``/chat/ask`` và việc che khoá trong thông điệp lỗi.

Lỗi của openai SDK thường bị LangChain hay pipeline bọc lại, nên phải nhận ra được qua chuỗi
``__cause__``/``__context__``.
"""

# Import sqlbot_xpack TRƯỚC để gỡ vòng import lẫn nhau của upstream.
import sqlbot_xpack  # noqa: F401  isort:skip

import httpx
import openai
import pytest
from fastapi import HTTPException

from apps.ai_model_config import hooks
from apps.ai_model_config.errors import (
    ModelConfigUnreadable,
    attach_model_error_code,
    classify_model_error,
    sanitize_secret,
)

REQUEST = httpx.Request("POST", "http://litellm:4000/v1/chat/completions")


def _status(cls, status, message="boom"):
    """Dựng lỗi HTTP của openai SDK."""
    return cls(message, response=httpx.Response(status, request=REQUEST), body=None)


@pytest.mark.parametrize(
    "exc, code",
    [
        (_status(openai.AuthenticationError, 401), "model_unauthorized"),
        (_status(openai.PermissionDeniedError, 403), "model_unauthorized"),
        (_status(openai.NotFoundError, 404), "model_not_found"),
        (_status(openai.RateLimitError, 429), "model_quota_exceeded"),
        (_status(openai.BadRequestError, 400, "Budget has been exceeded"), "model_quota_exceeded"),
        (_status(openai.InternalServerError, 500), "model_unavailable"),
        (openai.APIConnectionError(request=REQUEST), "model_unavailable"),
        (openai.APITimeoutError(request=REQUEST), "model_unavailable"),
        (ModelConfigUnreadable(1), "model_unavailable"),
        (_status(openai.BadRequestError, 400, "context too long"), None),
        (ValueError("x"), None),
    ],
)
def test_phan_loai(exc, code):
    assert classify_model_error(exc) == code


def test_lan_theo_chuoi_nguyen_nhan():
    try:
        try:
            raise _status(openai.AuthenticationError, 401)
        except Exception as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        assert classify_model_error(outer) == "model_unauthorized"


def test_attach_chi_them_code_khi_phan_loai_duoc():
    assert attach_model_error_code({"message": "m"}, ValueError()) == {"message": "m"}
    got = attach_model_error_code({"message": "m"}, _status(openai.NotFoundError, 404))
    assert got == {"message": "m", "code": "model_not_found"}


def test_sanitize_secret():
    text = "Invalid key sk-abc123DEF_456-xyz for model"
    assert sanitize_secret(text) == "Invalid key sk-*** for model"
    assert sanitize_secret("") == ""
    assert sanitize_secret("task-12") == "task-12"


def test_hook_validate_ds_models_tra_422_khong_lo_khoa():
    raw = {"llm": {"base_url": "ftp://x", "api_key": "sk-secret-1234567890", "model": "m"}}
    with pytest.raises(HTTPException) as info:
        hooks.validate_ds_models(session=None, oid=1, raw=raw)
    assert info.value.status_code == 422
    assert info.value.detail["code"] == "MODEL_CONFIG_INVALID"
    assert info.value.detail["field"] == "models.llm.base_url"
    assert "sk-secret" not in str(info.value.detail)
