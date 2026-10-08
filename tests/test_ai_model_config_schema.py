"""Test schema cấu hình model (``apps/ai_model_config/schemas.py``).

Trọng tâm: lỗi chỉ nêu đường dẫn trường, không bao giờ mang giá trị gửi lên (body có ``api_key``),
và khối nào dùng khoá URL nào.
"""

# Import sqlbot_xpack TRƯỚC để gỡ vòng import lẫn nhau của upstream.
import sqlbot_xpack  # noqa: F401  isort:skip

import json

import pytest

from apps.ai_model_config.errors import CODE_INVALID, ModelConfigError
from apps.ai_model_config.schemas import mask_key, parse_model_set

KEY = "sk-abcdefghijklmnop1234"


def _block(**over):
    """Khối llm/embedding hợp lệ, cho phép ghi đè từng trường."""
    block = {"base_url": "http://litellm:4000/v1", "api_key": KEY, "model": "mc-1"}
    block.update(over)
    return block


def _error(raw) -> ModelConfigError:
    """Parse và trả lỗi; fail nếu không lỗi."""
    with pytest.raises(ModelConfigError) as info:
        parse_model_set(raw)
    return info.value


def test_none_va_chuoi_rong_la_bo_trong():
    assert parse_model_set(None).is_empty()
    assert parse_model_set("  ").is_empty()
    assert parse_model_set({}).is_empty()


def test_doc_du_ba_khoi_tu_chuoi_json():
    raw = json.dumps({
        "llm": {"base_url": "http://gw:4000/v1/", "api_key": KEY, "model": "mc-1"},
        "rerank": {"url": "http://gw:4000/v2/rerank", "api_key": KEY, "model": "mc-3"},
    })
    model_set = parse_model_set(raw)
    blocks = dict(model_set.blocks())
    assert list(blocks) == ["llm", "rerank"]
    # Dấu "/" cuối bị bỏ để so sánh idempotent ổn định.
    assert blocks["llm"].endpoint == "http://gw:4000/v1"
    assert blocks["rerank"].endpoint == "http://gw:4000/v2/rerank"


def test_truong_la_bi_bo_qua():
    model_set = parse_model_set({"llm": _block(provider_model="x"), "khac": 1})
    assert dict(model_set.blocks())["llm"].model == "mc-1"


@pytest.mark.parametrize(
    "raw, field",
    [
        ({"llm": _block(api_key="")}, "models.llm.api_key"),
        ({"llm": _block(api_key="   ")}, "models.llm.api_key"),
        ({"llm": _block(base_url="ftp://gw/v1")}, "models.llm.base_url"),
        ({"llm": _block(base_url="http:///v1")}, "models.llm.base_url"),
        ({"embedding": _block(model="")}, "models.embedding.model"),
        # Rerank phải dùng ``url``, không nhận ``base_url``.
        ({"rerank": _block()}, "models.rerank.url"),
        ({"llm": "x"}, "models.llm"),
        ("[1]", "models"),
        ("{không phải json", "models"),
    ],
)
def test_loi_chi_neu_ten_truong(raw, field):
    err = _error(raw)
    assert err.code == CODE_INVALID and err.status == 422
    assert err.field == field


def test_loi_khong_mang_gia_tri_khoa():
    err = _error({"llm": _block(base_url="ftp://bad")})
    detail = err.to_http().detail
    assert KEY not in str(detail)
    # Không giữ ValidationError gốc (chứa ``input``) trong chuỗi nguyên nhân.
    assert err.__cause__ is None and err.__suppress_context__


def test_repr_khong_lo_khoa():
    model_set = parse_model_set({"llm": _block()})
    assert KEY not in repr(model_set)


def test_mask_key():
    assert mask_key(KEY) == "1234"
    assert mask_key("sk-short") == ""
