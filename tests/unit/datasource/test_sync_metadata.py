"""Test đồng bộ metadata bảng/cột từ nguồn vào ``core_table``/``core_field``.

Phủ ``sync_table``/``sync_fields``: quy tắc custom_comment khi comment nguồn đổi, dọn bảng/cột biến
mất, dọn bản ghi trùng tên, và số câu lệnh ghi/commit — chỉ số chứng minh đồng bộ không còn đi
từng cột một. DB nguồn, đối soát đồ thị quan hệ và embedding được thay bằng đồ giả.

Chạy trên SQLite file tạm (cùng hai chỉnh sửa kiểu cột như ``test_excel_import_sync``), và thêm
trên Postgres khi có ``SQLBOT_TEST_DB_URL`` — ghi theo lô (executemany, cột Identity) là chỗ hai
DB khác nhau. Bản Postgres dựng bảng trong một schema riêng và xoá schema khi xong.
"""

# Import sqlbot_xpack TRƯỚC: apps.datasource.crud.datasource nằm trong một vòng import lẫn nhau,
# chỉ gỡ được khi xpack là module khởi động vòng đó.
import sqlbot_xpack  # noqa: F401  isort:skip

import os

import pytest
from sqlalchemy import BigInteger, create_engine, event, insert, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlmodel import Session

from apps.datasource.crud import datasource as ds_crud
from apps.datasource.models.datasource import ColumnSchema, CoreDatasource, CoreField, CoreTable


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(element, compiler, **kw):
    """SQLite không có JSONB; JSON đủ cho mọi phép đọc ghi trong test."""
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_on_sqlite(element, compiler, **kw):
    """Để cột id ``BIGINT PRIMARY KEY`` thành ``INTEGER PRIMARY KEY`` và tự sinh giá trị."""
    return "INTEGER"


class Env:
    """Gom engine, nguồn giả và bộ đếm câu lệnh cho từng test."""

    def __init__(self, engine):
        self.engine = engine
        self.source = {}
        self.statements = []
        self.commits = 0
        self.embedded_tables = []

    def reset_counters(self):
        """Xoá bộ đếm trước phần cần đo."""
        self.statements.clear()
        self.commits = 0

    def writes(self, verb, table):
        """Đếm câu lệnh ``verb`` (INSERT/UPDATE/DELETE) nhắm vào ``table``."""
        return sum(1 for s in self.statements
                   if s.startswith(verb) and table in s.split('(')[0])


PG_SCHEMA = "test_sync_metadata"


def _engine(kind, tmp_path):
    """Dựng engine theo loại DB; Postgres dùng schema riêng qua ``search_path``."""
    if kind == "sqlite":
        return create_engine(f"sqlite:///{tmp_path / 'meta.db'}")
    url = os.getenv("SQLBOT_TEST_DB_URL")
    if not url:
        pytest.skip("Chưa đặt SQLBOT_TEST_DB_URL nên bỏ qua bản chạy trên Postgres")
    with create_engine(url).begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {PG_SCHEMA} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {PG_SCHEMA}"))
    return create_engine(url, connect_args={"options": f"-c search_path={PG_SCHEMA}"})


@pytest.fixture(params=["sqlite", "postgres"])
def env(request, tmp_path, monkeypatch):
    """DB metadata, nguồn giả cho ``get_fields`` và bộ đếm câu lệnh/commit."""
    engine = _engine(request.param, tmp_path)
    for model in (CoreDatasource, CoreTable, CoreField):
        model.__table__.create(engine)
    e = Env(engine)

    @event.listens_for(engine, "before_cursor_execute")
    def _record(conn, cursor, statement, parameters, context, executemany):
        """Ghi lại câu lệnh SQL, bỏ khoảng trắng đầu để so tiền tố."""
        e.statements.append(statement.lstrip().upper())

    @event.listens_for(engine, "commit")
    def _count_commit(conn):
        """Đếm số transaction được commit."""
        e.commits += 1

    monkeypatch.setattr(ds_crud, "getFieldsByDs",
                        lambda session, ds, name: [ColumnSchema(*c) for c in e.source[name]])
    monkeypatch.setattr(ds_crud, "reconcile_table_relation", lambda session, ds: None)
    monkeypatch.setattr(ds_crud, "run_save_table_embeddings", e.embedded_tables.append)
    monkeypatch.setattr(ds_crud, "run_save_ds_embeddings", lambda ids: None)
    yield e
    if request.param == "postgres":
        with engine.begin() as conn:
            conn.execute(text(f"DROP SCHEMA {PG_SCHEMA} CASCADE"))
    engine.dispose()


def _new_ds(session):
    """Tạo một datasource trống và trả về object đang gắn session."""
    ds = CoreDatasource(name="nguon", type="pg", type_name="PostgreSQL", oid=1)
    session.add(ds)
    session.commit()
    return ds


def _sync(env, session, ds, tables):
    """Đồng bộ theo nguồn giả: ``tables`` là ``{tên bảng: comment}``."""
    items = [CoreTable(table_name=name, table_comment=comment) for name, comment in tables.items()]
    ds_crud.sync_table(session, ds, items)
    return items


def _tables(session, ds_id):
    """Đọc ``{tên bảng: (table_comment, custom_comment)}`` từ DB."""
    rows = session.exec(select(CoreTable).where(CoreTable.ds_id == ds_id)).scalars().all()
    return {r.table_name: (r.table_comment, r.custom_comment) for r in rows}


def _fields(session, table_name):
    """Đọc các cột của một bảng theo ``field_index``, mỗi cột là một dict."""
    table = session.exec(select(CoreTable).where(CoreTable.table_name == table_name)).scalars().one()
    rows = session.exec(select(CoreField).where(CoreField.table_id == table.id)
                        .order_by(CoreField.field_index)).scalars().all()
    return [dict(name=r.field_name, type=r.field_type, comment=r.field_comment,
                 custom=r.custom_comment, index=r.field_index, checked=r.checked, ds_id=r.ds_id)
            for r in rows]


def test_first_sync_creates_tables_and_fields(env):
    env.source = {
        "don_vi": [("id", "int8", "Mã"), ("ten", "text", "Tên đơn vị")],
        "nhan_su": [("id", "int8", None)],
    }
    with Session(env.engine) as session:
        ds = _new_ds(session)
        ds_id = ds.id
        items = _sync(env, session, ds, {"don_vi": "Đơn vị", "nhan_su": None})

        assert _tables(session, ds_id) == {"don_vi": ("Đơn vị", "Đơn vị"), "nhan_su": (None, None)}
        assert _fields(session, "don_vi") == [
            dict(name="id", type="int8", comment="Mã", custom="Mã", index=0, checked=True, ds_id=ds_id),
            dict(name="ten", type="text", comment="Tên đơn vị", custom="Tên đơn vị", index=1,
                 checked=True, ds_id=ds_id),
        ]
        ids = {t.table_name: t.id for t in session.exec(select(CoreTable)).scalars()}
        assert [i.id for i in items] == [ids["don_vi"], ids["nhan_su"]]
        assert env.embedded_tables == [[ids["don_vi"], ids["nhan_su"]]]


def test_resync_comment_follows_source_unless_admin_edited(env):
    env.source = {"t": [("a", "int4", "A cũ"), ("b", "int4", "B cũ"), ("c", "int4", "C cũ")],
                  "u": []}
    with Session(env.engine) as session:
        ds = _new_ds(session)
        ds_id = ds.id
        _sync(env, session, ds, {"t": "T cũ", "u": "U cũ"})

        # Admin sửa custom_comment của bảng u, cột b; xoá trắng cột c.
        session.execute(CoreTable.__table__.update().where(CoreTable.table_name == "u")
                        .values(custom_comment="U admin"))
        session.execute(CoreField.__table__.update().where(CoreField.field_name == "b")
                        .values(custom_comment="B admin"))
        session.execute(CoreField.__table__.update().where(CoreField.field_name == "c")
                        .values(custom_comment=""))
        session.commit()

        env.source["t"] = [("a", "int4", "A mới"), ("b", "int4", "B mới"), ("c", "int4", "C mới")]
        _sync(env, session, ds, {"t": "T mới", "u": "U mới"})

        assert _tables(session, ds_id) == {"t": ("T mới", "T mới"), "u": ("U mới", "U admin")}
        assert [(f["name"], f["comment"], f["custom"]) for f in _fields(session, "t")] == [
            ("a", "A mới", "A mới"), ("b", "B mới", "B admin"), ("c", "C mới", "")]


def test_resync_removes_vanished_and_adds_new_and_reorders(env):
    env.source = {"t": [("a", "int4", None), ("b", "int4", None), ("c", "int4", None)],
                  "cu": [("x", "int4", None)]}
    with Session(env.engine) as session:
        ds = _new_ds(session)
        ds_id = ds.id
        _sync(env, session, ds, {"t": None, "cu": None})
        old_ids = {f.field_name: f.id for f in session.exec(select(CoreField)).scalars()}

        env.source = {"t": [("c", "text", None), ("a", "int4", None), ("d", "int4", None)]}
        _sync(env, session, ds, {"t": None})

        assert set(_tables(session, ds_id)) == {"t"}
        assert [(f["name"], f["type"], f["index"]) for f in _fields(session, "t")] == [
            ("c", "text", 0), ("a", "int4", 1), ("d", "int4", 2)]
        new_ids = {f.field_name: f.id for f in session.exec(select(CoreField)).scalars()}
        assert new_ids["a"] == old_ids["a"] and new_ids["c"] == old_ids["c"]
        assert "x" not in new_ids


def test_duplicate_field_rows_collapse_to_oldest(env):
    env.source = {"t": [("a", "int4", "A")]}
    with Session(env.engine) as session:
        ds = _new_ds(session)
        _sync(env, session, ds, {"t": None})
        first = session.exec(select(CoreField)).scalars().one()
        session.execute(insert(CoreField).values(
            ds_id=first.ds_id, table_id=first.table_id, checked=True, field_name="a",
            field_type="int4", field_comment="A", custom_comment="A", field_index=0))
        session.commit()
        first_id = first.id

        _sync(env, session, ds, {"t": None})

        assert [f.id for f in session.exec(select(CoreField)).scalars()] == [first_id]


def test_source_without_columns_keeps_existing_fields(env):
    env.source = {"t": [("a", "int4", None)]}
    with Session(env.engine) as session:
        ds = _new_ds(session)
        _sync(env, session, ds, {"t": None})
        env.source = {"t": []}
        _sync(env, session, ds, {"t": None})

        assert [f["name"] for f in _fields(session, "t")] == ["a"]


def test_unchanged_resync_writes_nothing_and_commits_once_per_table(env):
    env.source = {f"t{i}": [(f"c{j}", "int4", f"cột {j}") for j in range(20)] for i in range(5)}
    tables = {name: f"bảng {name}" for name in env.source}
    with Session(env.engine) as session:
        ds = _new_ds(session)
        _sync(env, session, ds, tables)
        env.reset_counters()

        _sync(env, session, ds, tables)

        assert env.writes("UPDATE", "CORE_FIELD") == 0
        assert env.writes("UPDATE", "CORE_TABLE") == 0
        assert env.writes("INSERT", "CORE_FIELD") == 0
        # Mỗi bảng một commit, thêm một commit cho bước dọn bảng biến mất.
        assert env.commits == len(tables) + 1
        # Không còn tra từng cột: số SELECT không phụ thuộc số cột.
        assert sum(s.startswith("SELECT") for s in env.statements) <= 2 * len(tables) + 2


def test_changed_resync_batches_field_writes(env):
    env.source = {"t": [(f"c{j}", "int4", f"cột {j}") for j in range(20)]}
    with Session(env.engine) as session:
        ds = _new_ds(session)
        _sync(env, session, ds, {"t": None})
        env.source["t"] = [(f"c{j}", "int8", f"cột {j}") for j in range(20)] + [("moi", "int4", None)]
        env.reset_counters()

        _sync(env, session, ds, {"t": None})

        assert env.commits == 2
        assert {f["type"] for f in _fields(session, "t") if f["name"] != "moi"} == {"int8"}
        assert [f["name"] for f in _fields(session, "t")][-1] == "moi"
