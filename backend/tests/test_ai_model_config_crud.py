"""Test đọc ghi ``ai_model_config`` (``apps/ai_model_config/crud.py``) trên SQLite trong RAM.

Trọng tâm: PUT workspace thay toàn bộ và idempotent; chốt cấu hình datasource là CHÉP (đổi
workspace về sau không đụng datasource); cấu hình tuỳ chỉnh không được trỏ ra host lạ.
"""

# Import sqlbot_xpack TRƯỚC để gỡ vòng import lẫn nhau của upstream.
import sqlbot_xpack  # noqa: F401  isort:skip

from types import SimpleNamespace

import pytest
from sqlmodel import Session, create_engine

import apps.ai_model_config.crud as crud
from apps.ai_model_config.errors import (
    CODE_INVALID,
    CODE_SECRET_UNSTABLE,
    ModelConfigError,
)
from apps.ai_model_config.models import AiModelConfig
from apps.ai_model_config.schemas import parse_model_set
from common.utils.aes_crypto import sqlbot_aes_decrypt

OID = 1
GW = "http://litellm:4000"


def _blocks(llm="sk-llm-000000000001", emb="sk-emb-000000000001", rerank="sk-rr-0000000000001",
            model_suffix="1", host=GW):
    """Bộ model đủ ba khối theo hình dạng Gateway gửi."""
    raw = {}
    if llm:
        raw["llm"] = {"base_url": f"{host}/v1", "api_key": llm, "model": f"mc-l{model_suffix}"}
    if emb:
        raw["embedding"] = {"base_url": f"{host}/v1", "api_key": emb, "model": f"mc-e{model_suffix}"}
    if rerank:
        raw["rerank"] = {"url": f"{host}/v2/rerank", "api_key": rerank, "model": f"mc-r{model_suffix}"}
    return parse_model_set(raw)


@pytest.fixture(autouse=True)
def stable_secret(monkeypatch):
    """Giả lập ``SECRET_KEY`` đã đặt tường minh."""
    monkeypatch.setattr(crud, "settings", SimpleNamespace(model_fields_set={"SECRET_KEY"}))


@pytest.fixture()
def session():
    """Session SQLite chỉ có bảng ``ai_model_config``."""
    engine = create_engine("sqlite://")
    AiModelConfig.__table__.create(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()


def _rows(session, scope, scope_id):
    return {r.model_type: r for r in crud.list_rows(session, scope, scope_id)}


class TestPutWorkspace:
    def test_tao_moi_ma_hoa_khoa(self, session):
        assert crud.put_workspace_models(session, OID, _blocks()) is True
        rows = _rows(session, "workspace", OID)
        assert set(rows) == {"llm", "embedding", "rerank"}
        assert rows["llm"].api_key_enc != "sk-llm-000000000001"
        assert sqlbot_aes_decrypt(rows["llm"].api_key_enc) == "sk-llm-000000000001"
        assert rows["llm"].api_key_hint == "0001"
        assert rows["rerank"].base_url == f"{GW}/v2/rerank"
        assert rows["llm"].origin is None

    def test_gui_lai_y_nguyen_thi_khong_doi(self, session):
        crud.put_workspace_models(session, OID, _blocks())
        before = {t: (r.updated_at, r.api_key_enc) for t, r in _rows(session, "workspace", OID).items()}
        assert crud.put_workspace_models(session, OID, _blocks()) is False
        after = {t: (r.updated_at, r.api_key_enc) for t, r in _rows(session, "workspace", OID).items()}
        assert before == after

    def test_thieu_khoi_thi_xoa_dong(self, session):
        crud.put_workspace_models(session, OID, _blocks())
        assert crud.put_workspace_models(session, OID, _blocks(rerank=None)) is True
        assert set(_rows(session, "workspace", OID)) == {"llm", "embedding"}

    def test_doi_model_thi_xoa_dim_doi_khoa_thi_giu(self, session):
        crud.put_workspace_models(session, OID, _blocks())
        emb = _rows(session, "workspace", OID)["embedding"]
        emb.dim = 1024
        session.flush()
        crud.put_workspace_models(session, OID, _blocks(emb="sk-emb-000000000002"))
        assert _rows(session, "workspace", OID)["embedding"].dim == 1024
        crud.put_workspace_models(session, OID, _blocks(emb="sk-emb-000000000002", model_suffix="2"))
        assert _rows(session, "workspace", OID)["embedding"].dim is None

    def test_khoa_khong_giai_ma_duoc_thi_ghi_de(self, session):
        """Đường phục hồi sau khi đổi SECRET_KEY: đẩy lại là dòng đọc được trở lại."""
        crud.put_workspace_models(session, OID, _blocks())
        _rows(session, "workspace", OID)["llm"].api_key_enc = "rác"
        session.flush()
        assert crud.put_workspace_models(session, OID, _blocks()) is True
        assert sqlbot_aes_decrypt(_rows(session, "workspace", OID)["llm"].api_key_enc) == "sk-llm-000000000001"

    def test_secret_key_khong_co_dinh_thi_503(self, session, monkeypatch):
        monkeypatch.setattr(crud, "settings", SimpleNamespace(model_fields_set=set()))
        with pytest.raises(ModelConfigError) as info:
            crud.put_workspace_models(session, OID, _blocks())
        assert info.value.code == CODE_SECRET_UNSTABLE and info.value.status == 503


class TestPinDatasource:
    def test_chep_cau_hinh_workspace(self, session):
        crud.put_workspace_models(session, OID, _blocks())
        crud.pin_datasource_models(session, 10, OID, parse_model_set(None))
        ws = _rows(session, "workspace", OID)
        ds = _rows(session, "datasource", 10)
        assert set(ds) == {"llm", "embedding", "rerank"}
        for t in ds:
            assert ds[t].origin == "workspace" and ds[t].oid == OID
            assert (ds[t].base_url, ds[t].model, ds[t].api_key_enc) == (
                ws[t].base_url, ws[t].model, ws[t].api_key_enc)

    def test_tuy_chinh_mot_phan(self, session):
        crud.put_workspace_models(session, OID, _blocks())
        custom = _blocks(emb=None, rerank=None, llm="sk-custom-0000009999", model_suffix="9")
        crud.pin_datasource_models(session, 10, OID, custom)
        ds = _rows(session, "datasource", 10)
        assert ds["llm"].origin == "custom" and ds["llm"].model == "mc-l9"
        assert sqlbot_aes_decrypt(ds["llm"].api_key_enc) == "sk-custom-0000009999"
        assert ds["embedding"].origin == "workspace"

    def test_workspace_trong_khong_ghi_gi(self, session):
        crud.pin_datasource_models(session, 10, OID, parse_model_set(None))
        assert _rows(session, "datasource", 10) == {}

    def test_workspace_trong_ma_gui_tuy_chinh_thi_422(self, session):
        with pytest.raises(ModelConfigError) as info:
            crud.pin_datasource_models(session, 10, OID, _blocks())
        assert info.value.code == CODE_INVALID and info.value.field == "models"

    def test_host_la_thi_422(self, session):
        crud.put_workspace_models(session, OID, _blocks())
        with pytest.raises(ModelConfigError) as info:
            crud.pin_datasource_models(session, 10, OID, _blocks(llm=None, rerank=None, host="http://evil:4000"))
        assert info.value.field == "models.embedding.base_url"
        assert _rows(session, "datasource", 10) == {}

    def test_cung_host_khac_port_mac_dinh_van_khop(self, session):
        crud.put_workspace_models(session, OID, _blocks(host="https://gw.local"))
        crud.pin_datasource_models(session, 10, OID, _blocks(rerank=None, emb=None, host="https://gw.local:443"))
        assert _rows(session, "datasource", 10)["llm"].origin == "custom"

    def test_doi_workspace_khong_dung_datasource(self, session):
        crud.put_workspace_models(session, OID, _blocks())
        crud.pin_datasource_models(session, 10, OID, parse_model_set(None))
        crud.put_workspace_models(session, OID, _blocks(model_suffix="2", rerank=None))
        ds = _rows(session, "datasource", 10)
        assert ds["llm"].model == "mc-l1" and "rerank" in ds


def test_delete_scope_chi_xoa_dung_pham_vi(session):
    crud.put_workspace_models(session, OID, _blocks())
    crud.pin_datasource_models(session, 10, OID, parse_model_set(None))
    crud.pin_datasource_models(session, 11, OID, parse_model_set(None))
    crud.delete_scope(session, "datasource", 10)
    assert _rows(session, "datasource", 10) == {}
    assert len(_rows(session, "datasource", 11)) == 3
    assert len(_rows(session, "workspace", OID)) == 3


def test_record_dim_chi_ghi_lan_dau(session):
    crud.put_workspace_models(session, OID, _blocks())
    crud.pin_datasource_models(session, 10, OID, parse_model_set(None))
    row = _rows(session, "datasource", 10)["embedding"]
    crud.record_dim(session, row.id, 1024)
    crud.record_dim(session, row.id, 768)
    session.expire_all()
    assert _rows(session, "datasource", 10)["embedding"].dim == 1024
