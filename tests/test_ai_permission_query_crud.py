"""Test hàm CRUD đọc tóm tắt quyền theo user, trên Postgres thật."""

from apps.hooks.crud.ai_user_permission import (
    list_users_with_permission_summary,
    replace_user_permissions,
)
from apps.hooks.schemas.ai_sync_schema import AuthorizationSyncData


def _forms(*specs):
    """Dựng list[FormQuery] từ các tuple (form_uuid, table_name)."""
    payload = {
        "userId": "u1",
        "isAdmin": False,
        "formQueries": [
            {
                "formUuid": form_uuid,
                "tableInfo": {
                    "databaseTableName": table, "fields": [{"id": "a"}],
                    "queries": [{"datasourceId": "d1", "datasourceType": "postgresql", "query": "SELECT 1"}],
                },
            }
            for form_uuid, table in specs
        ],
    }
    return AuthorizationSyncData.model_validate(payload).form_queries


def _seed(session, user_id: str, full_name, is_admin: bool, forms: tuple, sync_version: int):
    replace_user_permissions(
        session, user_id=user_id, full_name=full_name, is_admin=is_admin,
        form_queries=_forms(*forms), sync_version=sync_version,
    )
    session.commit()


def test_tra_dung_form_count_cho_nhieu_user(db_session):
    _seed(db_session, "u1", "Nguyễn Văn A", False, (("f1", "t1"), ("f2", "t2")), 100)
    _seed(db_session, "u2", None, True, (("f3", "t3"),), 200)

    result = list_users_with_permission_summary(db_session)
    by_user = {r["user_id"]: r for r in result}

    assert by_user["u1"]["form_count"] == 2
    assert by_user["u1"]["full_name"] == "Nguyễn Văn A"
    assert by_user["u1"]["is_admin"] is False
    assert by_user["u1"]["sync_version"] == 100
    assert by_user["u1"]["synced_at"] is not None

    assert by_user["u2"]["form_count"] == 1
    assert by_user["u2"]["is_admin"] is True
    assert by_user["u2"]["sync_version"] == 200


def test_user_khong_co_dong_nao_khong_xuat_hien(db_session):
    _seed(db_session, "u1", None, False, (("f1", "t1"),), 100)
    result = list_users_with_permission_summary(db_session)
    assert {r["user_id"] for r in result} == {"u1"}


def test_sap_theo_user_id(db_session):
    _seed(db_session, "u2", None, False, (("f1", "t1"),), 100)
    _seed(db_session, "u1", None, False, (("f2", "t2"),), 100)
    result = list_users_with_permission_summary(db_session)
    assert [r["user_id"] for r in result] == ["u1", "u2"]


def test_khong_co_user_nao_tra_rong(db_session):
    assert list_users_with_permission_summary(db_session) == []
