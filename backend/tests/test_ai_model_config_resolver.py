"""Test chọn LLM cho lượt hỏi (``apps/ai_model_config/resolver.py``) trên SQLite trong RAM.

Trọng tâm: thứ tự ưu tiên (trợ lý > datasource > workspace khi chưa có datasource > model chung),
datasource cũ không mượn cấu hình workspace, và fail-closed khi khoá không giải mã được.
"""

# Import sqlbot_xpack TRƯỚC để gỡ vòng import lẫn nhau của upstream.
import sqlbot_xpack  # noqa: F401  isort:skip

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlmodel import Session, create_engine

import apps.ai_model_config.crud as crud
import apps.ai_model_config.crypto as crypto
import apps.ai_model_config.resolver as resolver
from apps.ai_model_config.errors import ModelConfigUnreadable
from apps.ai_model_config.models import AiModelConfig
from apps.ai_model_config.schemas import parse_model_set
from apps.chat.models.chat_model import Chat
from apps.datasource.models.datasource import CoreDatasource

OID = 1
GW = "http://litellm:4000"
DEFAULT = object()


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(_type, _compiler, **_kw):
    """SQLite không có JSONB; cột này không dùng trong test nên dịch thành JSON."""
    return "JSON"


def _llm(key, model, host=GW):
    """Bộ model chỉ có khối llm."""
    return parse_model_set({"llm": {"base_url": f"{host}/v1", "api_key": key, "model": model}})


@pytest.fixture(autouse=True)
def stable_secret(monkeypatch):
    """Giả lập ``SECRET_KEY`` đã đặt tường minh."""
    monkeypatch.setattr(crud, "settings", SimpleNamespace(model_fields_set={"SECRET_KEY"}))


@pytest.fixture(autouse=True)
def fake_default(monkeypatch):
    """Thay ``get_default_config`` bằng bản ghi lại tham số, trả về một giá trị đánh dấu."""
    calls = []

    async def _fake(model_id=None):
        calls.append(model_id)
        return DEFAULT

    monkeypatch.setattr(resolver, "get_default_config", _fake)
    return calls


@pytest.fixture()
def session():
    """SQLite có workspace OID: ds 10 (có cấu hình llm), ds 11 (không có), ds 20 thuộc workspace khác."""
    engine = create_engine("sqlite://")
    for table in (AiModelConfig, Chat, CoreDatasource):
        table.__table__.create(engine)
    with Session(engine) as s:
        for ds_id, oid in ((10, OID), (11, OID), (20, 2)):
            s.add(CoreDatasource(id=ds_id, name=f"ds{ds_id}", type="pg", oid=oid, configuration=""))
        s.flush()
        crud.put_workspace_models(s, OID, _llm("sk-ws-00000000000001", "mc-ws"))
        crud.pin_datasource_models(s, 10, OID, _llm("sk-ds-00000000000010", "mc-ds10"))
        crud.put_workspace_models(s, 2, _llm("sk-ws-00000000000002", "mc-ws2"))
        crud.pin_datasource_models(s, 20, 2, parse_model_set(None))
        s.commit()
        yield s
    engine.dispose()


def _chat(session, chat_id, datasource=None, oid=OID):
    """Thêm một chat và trả câu hỏi tương ứng."""
    session.add(Chat(id=chat_id, oid=oid, datasource=datasource, create_by=1, brief="", engine_type=""))
    session.commit()
    return SimpleNamespace(chat_id=chat_id, datasource_id=None)


def _resolve(session, question, **kw):
    """Gọi ``resolve_llm_config`` đồng bộ."""
    return asyncio.run(resolver.resolve_llm_config(session, question, **kw))


def test_model_rieng_cua_tro_ly_thang(session, fake_default):
    question = _chat(session, 1, datasource=10)
    assert _resolve(session, question, specialized_model_id=99) is DEFAULT
    assert fake_default == [99]


def test_tro_ly_datasource_ngoai_bo_qua_cau_hinh(session, fake_default):
    question = _chat(session, 1, datasource=10)
    assert _resolve(session, question, skip_scoped=True) is DEFAULT


def test_chat_co_datasource_dung_dong_cua_datasource(session):
    config = _resolve(session, _chat(session, 1, datasource=10))
    assert (config.model_name, config.api_base_url, config.api_key) == (
        "mc-ds10", f"{GW}/v1", "sk-ds-00000000000010")
    assert config.model_id is None and config.model_type == "openai"
    # Không tiêm tham số tắt thinking (D8).
    assert config.additional_params == {}


def test_datasource_id_cua_cau_hoi_khi_chat_chua_gan(session):
    question = _chat(session, 1)
    question.datasource_id = 10
    assert _resolve(session, question).model_name == "mc-ds10"


def test_datasource_da_gan_vao_chat_thang_datasource_id(session):
    question = _chat(session, 1, datasource=11)
    question.datasource_id = 10
    assert _resolve(session, question) is DEFAULT


def test_datasource_cu_khong_muon_cau_hinh_workspace(session):
    assert _resolve(session, _chat(session, 1, datasource=11)) is DEFAULT


def test_chat_chua_co_datasource_dung_dong_workspace(session):
    assert _resolve(session, _chat(session, 1)).model_name == "mc-ws"


def test_datasource_khac_workspace_bi_bo_qua(session):
    question = _chat(session, 1)
    question.datasource_id = 20
    assert _resolve(session, question) is DEFAULT


def test_khoa_hong_thi_bao_loi_khong_roi_ve_model_chung(session):
    row = crud.get_row(session, "datasource", 10, "llm")
    row.api_key_enc = "rác"
    row.updated_at = datetime.now(timezone.utc)
    session.commit()
    with pytest.raises(ModelConfigUnreadable):
        _resolve(session, _chat(session, 1, datasource=10))


def test_cache_giai_ma_theo_id_va_updated_at(session, monkeypatch):
    calls = []
    real = crypto.sqlbot_aes_decrypt

    def _counting(text):
        calls.append(text)
        return real(text)

    monkeypatch.setattr(crypto, "sqlbot_aes_decrypt", _counting)
    row = crud.get_row(session, "workspace", OID, "llm")
    row.updated_at = datetime(2030, 1, 1, tzinfo=timezone.utc)
    assert crypto.decrypt_row_key(row) == crypto.decrypt_row_key(row) == "sk-ws-00000000000001"
    assert len(calls) == 1
    row.updated_at = datetime(2030, 1, 2, tzinfo=timezone.utc)
    crypto.decrypt_row_key(row)
    assert len(calls) == 2
