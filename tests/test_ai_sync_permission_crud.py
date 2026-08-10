"""Test ngữ nghĩa full-snapshot của bảng quyền, trên Postgres thật."""

from apps.hooks.crud.ai_user_permission import get_user_permissions, replace_user_permissions
from apps.hooks.schemas.ai_sync_schema import AuthorizationSyncData


def _forms(*specs):
    """Dựng list[FormQuery] từ các tuple (form_uuid, table_name, field_ids)."""
    payload = {
        "userId": "u1",
        "isAdmin": False,
        "formQueries": [
            {
                "formUuid": form_uuid,
                "tableInfo": {
                    "databaseTableName": table,
                    "tableDisplayName": f"Hiển thị {table}",
                    "tableDescription": f"Mô tả {table}",
                    "fields": [{"id": fid, "name": fid.upper(), "description": None} for fid in field_ids],
                },
                "postgresQuery": f"SELECT * FROM {table}",
                "clickHouseQuery": f"SELECT * FROM {table} SETTINGS x=1",
            }
            for form_uuid, table, field_ids in specs
        ],
    }
    return AuthorizationSyncData.model_validate(payload).form_queries


def test_ghi_moi_snapshot(db_session):
    upserted, deleted = replace_user_permissions(
        db_session, user_id="u1", full_name="Nguyễn Văn A", is_admin=False,
        form_queries=_forms(("f1", "t1", ["a", "b"]), ("f2", "t2", ["c"])), sync_version=100,
    )
    assert (upserted, deleted) == (2, 0)
    rows = get_user_permissions(db_session, "u1")
    assert {r.form_uuid for r in rows} == {"f1", "f2"}
    r1 = next(r for r in rows if r.form_uuid == "f1")
    assert r1.database_table_name == "t1"
    assert r1.table_display_name == "Hiển thị t1"
    assert r1.table_description == "Mô tả t1"
    assert r1.fields == [
        {"id": "a", "name": "A", "description": None},
        {"id": "b", "name": "B", "description": None},
    ]
    assert r1.postgres_query == "SELECT * FROM t1"
    assert r1.clickhouse_query == "SELECT * FROM t1 SETTINGS x=1"
    assert r1.sync_version == 100
    assert r1.full_name == "Nguyễn Văn A"
    assert r1.is_admin is False


def test_snapshot_moi_xoa_form_khong_con_trong_payload(db_session):
    replace_user_permissions(
        db_session, user_id="u1", full_name=None, is_admin=False,
        form_queries=_forms(("f1", "t1", ["a"]), ("f2", "t2", ["b"])), sync_version=100,
    )
    upserted, deleted = replace_user_permissions(
        db_session, user_id="u1", full_name=None, is_admin=False,
        form_queries=_forms(("f2", "t2", ["b"])), sync_version=200,
    )
    assert (upserted, deleted) == (1, 1)
    assert {r.form_uuid for r in get_user_permissions(db_session, "u1")} == {"f2"}


def test_form_queries_rong_thu_hoi_het_quyen(db_session):
    replace_user_permissions(
        db_session, user_id="u1", full_name=None, is_admin=False,
        form_queries=_forms(("f1", "t1", ["a"])), sync_version=100,
    )
    upserted, deleted = replace_user_permissions(
        db_session, user_id="u1", full_name=None, is_admin=False, form_queries=[], sync_version=200,
    )
    assert (upserted, deleted) == (0, 1)
    assert get_user_permissions(db_session, "u1") == []


def test_upsert_ghi_de_dung_form_cu(db_session):
    replace_user_permissions(
        db_session, user_id="u1", full_name="Tên cũ", is_admin=False,
        form_queries=_forms(("f1", "t_old", ["a"])), sync_version=100,
    )
    before = get_user_permissions(db_session, "u1")[0]
    replace_user_permissions(
        db_session, user_id="u1", full_name="Tên mới", is_admin=True,
        form_queries=_forms(("f1", "t_new", ["x", "y"])), sync_version=200,
    )
    rows = get_user_permissions(db_session, "u1")
    assert len(rows) == 1
    row = rows[0]
    # giữ nguyên dòng cũ (UPDATE, không DELETE + INSERT) nên id và created_at không đổi
    assert row.id == before.id
    assert row.created_at == before.created_at
    assert row.database_table_name == "t_new"
    assert [f["id"] for f in row.fields] == ["x", "y"]
    assert row.sync_version == 200
    assert row.full_name == "Tên mới"
    assert row.is_admin is True


def test_khong_dung_den_quyen_cua_user_khac(db_session):
    replace_user_permissions(
        db_session, user_id="u1", full_name=None, is_admin=False,
        form_queries=_forms(("f1", "t1", ["a"])), sync_version=100,
    )
    replace_user_permissions(
        db_session, user_id="u2", full_name=None, is_admin=False,
        form_queries=_forms(("f1", "t1", ["a"])), sync_version=100,
    )
    replace_user_permissions(
        db_session, user_id="u2", full_name=None, is_admin=False, form_queries=[], sync_version=200,
    )
    assert len(get_user_permissions(db_session, "u1")) == 1
    assert get_user_permissions(db_session, "u2") == []


def test_get_user_permissions_tra_rong_voi_user_la(db_session):
    assert get_user_permissions(db_session, "khong-ton-tai") == []
