"""Test embedding theo datasource (``apps/ai_model_config/embedding.py`` và các chỗ gọi upstream).

Model embedding thật được thay bằng bản giả trả vector cố định; trọng tâm là chọn đúng model theo
datasource, cô lập lỗi theo model, ghi ``dim`` lần đầu, và xếp hạng khi trộn nhiều model.
"""

# Import sqlbot_xpack TRƯỚC để gỡ vòng import lẫn nhau của upstream.
import sqlbot_xpack  # noqa: F401  isort:skip

import json
from types import SimpleNamespace

import httpx
import openai
import pytest
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

import apps.ai_model_config.crud as crud
import apps.ai_model_config.embedding as emb
import apps.datasource.crud.table as table_crud
import apps.datasource.embedding.table_embedding as table_embedding
from apps.ai_model_config.models import AiModelConfig
from apps.ai_model_config.schemas import parse_model_set
from apps.datasource.models.datasource import CoreField, CoreTable
from common.core.config import settings

OID = 1
GW = "http://litellm:4000"


class FakeModel:
    """Model embedding giả: vector cố định theo tên, có thể cài lỗi."""

    def __init__(self, vector, error=None):
        """Ghi lại vector trả về và lỗi (nếu có) cần ném."""
        self.vector = vector
        self.error = error
        self.calls = []

    def embed_query(self, text):
        """Trả vector cố định hoặc ném lỗi đã cài."""
        self.calls.append(text)
        if self.error is not None:
            raise self.error
        return list(self.vector)


def _status_error(cls, status, message="boom"):
    """Dựng lỗi HTTP của openai SDK."""
    response = httpx.Response(status, request=httpx.Request("POST", "http://x"))
    return cls(message, response=response, body=None)


def _emb(model, host=GW):
    """Bộ model chỉ có khối embedding."""
    return parse_model_set(
        {"embedding": {"base_url": f"{host}/v1", "api_key": "sk-emb-0000000000001", "model": model}}
    )


@pytest.fixture()
def engine(monkeypatch):
    """SQLite dùng chung một kết nối (StaticPool) để session của job và của embedder cùng thấy dữ liệu."""
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    for table in (AiModelConfig, CoreTable, CoreField):
        table.__table__.create(eng)
    monkeypatch.setattr(crud, "settings", SimpleNamespace(model_fields_set={"SECRET_KEY"}))
    with Session(eng) as s:
        crud.put_workspace_models(s, OID, _emb("mc-a"))
        crud.pin_datasource_models(s, 10, OID, parse_model_set(None))
        crud.pin_datasource_models(s, 12, OID, _emb("mc-b"))
        s.commit()
    monkeypatch.setattr(emb, "engine", eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def models(monkeypatch):
    """Model giả theo tên model của dòng cấu hình; ``global`` là model chung."""
    registry = {"mc-a": FakeModel([1.0, 0.0, 0.0]), "mc-b": FakeModel([0.0, 1.0]),
                "global": FakeModel([1.0, 1.0])}
    monkeypatch.setattr(emb, "_embedder_for_row", lambda row: registry[row.model])
    monkeypatch.setattr(emb.EmbeddingModelCache, "get_model", staticmethod(lambda *a, **k: registry["global"]))
    return registry


def _dim(engine, ds_id):
    with Session(engine) as s:
        return crud.get_row(s, "datasource", ds_id, "embedding").dim


class TestDsEmbedder:
    def test_moi_datasource_dung_model_cua_no(self, engine, models):
        embedder = emb.DsEmbedder()
        assert json.loads(embedder.embed(10, "t")) == [1.0, 0.0, 0.0]
        assert json.loads(embedder.embed(12, "t")) == [0.0, 1.0]
        # Datasource không có dòng cấu hình (11) dùng model chung.
        assert json.loads(embedder.embed(11, "t")) == [1.0, 1.0]

    def test_ghi_dim_lan_dau(self, engine, models):
        assert _dim(engine, 10) is None
        emb.DsEmbedder().embed(10, "t")
        assert _dim(engine, 10) == 3

    def test_lech_dim_thi_khong_ghi(self, engine, models):
        emb.DsEmbedder().embed(10, "t")
        models["mc-a"].vector = [1.0, 0.0]
        assert emb.DsEmbedder().embed(10, "t") is None
        assert _dim(engine, 10) == 3

    def test_model_hong_khong_chan_model_khac(self, engine, models):
        models["mc-a"].error = _status_error(openai.AuthenticationError, 401)
        embedder = emb.DsEmbedder()
        assert embedder.embed(10, "t1") is None
        assert embedder.embed(10, "t2") is None
        # Đã đánh dấu hỏng: không gọi lại model lần hai.
        assert models["mc-a"].calls == ["t1"]
        assert embedder.embed(12, "t") is not None

    def test_loi_400_dung_ban_rut_gon(self, engine, models):
        model = models["mc-a"]
        original = model.embed_query

        def _embed(text):
            if text == "dài":
                raise _status_error(openai.BadRequestError, 400)
            return original(text)

        model.embed_query = _embed
        assert json.loads(emb.DsEmbedder().embed(10, "dài", fallback_text="ngắn")) == [1.0, 0.0, 0.0]

    def test_loi_400_khong_danh_dau_hong(self, engine, models):
        models["mc-a"].error = _status_error(openai.BadRequestError, 400)
        embedder = emb.DsEmbedder()
        assert embedder.embed(10, "t1") is None
        embedder.embed(10, "t2")
        assert models["mc-a"].calls == ["t1", "t2"]


def test_save_table_embedding_theo_datasource(engine, models, monkeypatch):
    monkeypatch.setattr(settings, "TABLE_EMBEDDING_ENABLED", True)
    with Session(engine) as s:
        s.add(CoreTable(id=1, ds_id=10, table_name="a", table_comment="", custom_comment=""))
        s.add(CoreTable(id=2, ds_id=12, table_name="b", table_comment="", custom_comment=""))
        s.add(CoreTable(id=3, ds_id=11, table_name="c", table_comment="", custom_comment=""))
        s.commit()
    models["mc-a"].error = _status_error(openai.AuthenticationError, 401)
    table_crud.save_table_embedding(scoped_session(sessionmaker(bind=engine)), [1, 2, 3])
    with Session(engine) as s:
        got = {t.id: t.embedding for t in s.query(CoreTable).all()}
    assert got[1] is None
    assert json.loads(got[2]) == [0.0, 1.0]
    assert json.loads(got[3]) == [1.0, 1.0]


def test_calc_table_embedding_dung_model_cua_datasource(engine, models):
    tables = [{"id": 1, "table_name": "a", "schema_table": "a", "embedding": json.dumps([0.0, 1.0])},
              {"id": 2, "table_name": "b", "schema_table": "b", "embedding": json.dumps([1.0, 0.0])}]
    ranked = table_embedding.calc_table_embedding(tables, "q", ds_id=12)
    assert [t["id"] for t in ranked] == [1, 2]
    assert models["mc-b"].calls == ["q"]


class TestRankDs:
    def _item(self, ds_id, vector):
        return {"id": ds_id, "embedding": json.dumps(vector), "cosine_similarity": 0.0}

    def test_mot_nhom_xep_nhu_cu(self, engine, models):
        items = [self._item(10, [0.0, 1.0, 0.0]), self._item(10, [1.0, 0.0, 0.0])]
        ranked = emb.rank_ds_by_embedding(items, "q")
        assert ranked[0]["embedding"] == json.dumps([1.0, 0.0, 0.0])

    def test_nhieu_nhom_xen_ke_theo_thu_hang(self, engine, models):
        items = [self._item(11, [1.0, 0.0]), self._item(11, [1.0, 1.0]),
                 self._item(12, [0.0, 1.0])]
        ranked = emb.rank_ds_by_embedding(items, "q")
        # Hạng 1 của từng nhóm đứng trước hạng 2 của bất kỳ nhóm nào.
        assert {r["id"] for r in ranked[:2]} == {11, 12}
        assert ranked[2]["embedding"] == json.dumps([1.0, 0.0])
        assert models["global"].calls == ["q"] and models["mc-b"].calls == ["q"]
