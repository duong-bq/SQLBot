"""Test handler actionType=1: validate batch, áp snapshot cho từng user."""

import pytest

from apps.hooks.constants import SyncActionType, SyncErrorCode, SyncStatus
from apps.hooks.crud.ai_user_permission import get_user_permissions
from apps.hooks.errors import SyncHookError
from apps.hooks.handlers import ACTION_HANDLERS
from apps.hooks.handlers.authorization import handle_authorization_sync
from apps.hooks.schemas.ai_sync_schema import SyncEnvelope


def _envelope(data: dict) -> SyncEnvelope:
    return SyncEnvelope.model_validate({"actionType": 1, "payload": data})


def _user(user_id="u1", forms=(("f1", "t1"),)):
    return {
        "userId": user_id,
        "fullName": "Nguyễn Văn A",
        "isAdmin": False,
        "formQueries": [
            {
                "formUuid": form_uuid,
                "tableInfo": {
                    "databaseTableName": table,
                    "queries": [{"datasourceId": f"ds-{form_uuid}", "datasourceType": "postgresql", "query": f"SELECT * FROM {table}"}],
                },
            }
            for form_uuid, table in forms
        ],
    }


def _batch(*users) -> dict:
    return {"users": list(users) if users else [_user()]}


def test_dispatch_dung_hai_action_type_da_implement(db_session):
    from apps.hooks.handlers.datasource_sync import handle_datasource_sync

    assert ACTION_HANDLERS[SyncActionType.AUTHORIZATION_SYNC] is handle_authorization_sync
    assert ACTION_HANDLERS[SyncActionType.DATASOURCE_SYNC] is handle_datasource_sync
    assert set(ACTION_HANDLERS) == {SyncActionType.AUTHORIZATION_SYNC, SyncActionType.DATASOURCE_SYNC}


def test_ap_snapshot_1_user_thanh_cong(db_session):
    result = handle_authorization_sync(db_session, _envelope(_batch(_user())), 1000)
    assert result.status is SyncStatus.SUCCESS
    assert len(result.results) == 1
    assert result.results[0].userId == "u1"
    assert result.results[0].status == "SUCCESS"
    assert (result.results[0].applied.upserted, result.results[0].applied.deleted) == (1, 0)
    assert len(get_user_permissions(db_session, "u1")) == 1


def test_ap_snapshot_2_user_deu_thanh_cong(db_session):
    batch = _batch(_user("u1", (("f1", "t1"),)), _user("u2", (("f2", "t2"),)))
    result = handle_authorization_sync(db_session, _envelope(batch), 1000)
    assert result.status is SyncStatus.SUCCESS
    statuses = {r.userId: r.status for r in result.results}
    assert statuses == {"u1": "SUCCESS", "u2": "SUCCESS"}
    assert len(get_user_permissions(db_session, "u1")) == 1
    assert len(get_user_permissions(db_session, "u2")) == 1


def test_users_rong_raise_empty_user_list(db_session):
    with pytest.raises(SyncHookError) as exc:
        handle_authorization_sync(db_session, _envelope({"users": []}), 1000)
    assert exc.value.http_status == 400
    assert exc.value.error_code is SyncErrorCode.EMPTY_USER_LIST


def test_thieu_truong_users_raise_invalid_payload(db_session):
    with pytest.raises(SyncHookError) as exc:
        handle_authorization_sync(db_session, _envelope({}), 1000)
    assert exc.value.http_status == 400
    assert exc.value.error_code is SyncErrorCode.INVALID_PAYLOAD


def test_user_payload_sai_raise_invalid_payload(db_session):
    with pytest.raises(SyncHookError) as exc:
        handle_authorization_sync(db_session, _envelope({"users": [{"userId": "u1"}]}), 1000)
    assert exc.value.http_status == 400
    assert exc.value.error_code is SyncErrorCode.INVALID_PAYLOAD


def test_trung_user_id_raise_duplicate_user_id_khong_ai_duoc_ap_dung(db_session):
    batch = _batch(_user("u1"), _user("u1"))
    with pytest.raises(SyncHookError) as exc:
        handle_authorization_sync(db_session, _envelope(batch), 1000)
    assert exc.value.http_status == 400
    assert exc.value.error_code is SyncErrorCode.DUPLICATE_USER_ID
    assert "u1" in exc.value.message
    assert get_user_permissions(db_session, "u1") == []


def test_trung_database_table_name_trong_1_user_raise_duplicate_database_table_name(db_session):
    """Trùng `formUuid` không còn là lỗi — khoá định danh giờ là bảng, không phải form."""
    data = _user(forms=(("f1", "t1"), ("f2", "t1")))
    with pytest.raises(SyncHookError) as exc:
        handle_authorization_sync(db_session, _envelope(_batch(data)), 1000)
    assert exc.value.http_status == 400
    assert exc.value.error_code is SyncErrorCode.DUPLICATE_DATABASE_TABLE_NAME
    assert "t1" in exc.value.message


def test_trung_form_uuid_khac_bang_khong_con_la_loi(db_session):
    """2 form cùng `formUuid` nhưng khác bảng — hợp lệ, vì `formUuid` chỉ còn tham khảo."""
    data = _user(forms=(("f1", "t1"), ("f1", "t2")))
    result = handle_authorization_sync(db_session, _envelope(_batch(data)), 1000)
    assert result.results[0].status == "SUCCESS"
    assert len(get_user_permissions(db_session, "u1")) == 2


def test_form_uuid_khong_bat_buoc(db_session):
    """SW không gửi `formUuid` — vẫn áp dụng được, khoá định danh là `databaseTableName`."""
    data = {
        "userId": "u1", "isAdmin": False,
        "formQueries": [{
            "tableInfo": {
                "databaseTableName": "t1",
                "queries": [{"datasourceId": "d1", "datasourceType": "postgresql", "query": "SELECT 1"}],
            },
        }],
    }
    result = handle_authorization_sync(db_session, _envelope(_batch(data)), 1000)
    assert result.results[0].status == "SUCCESS"
    rows = get_user_permissions(db_session, "u1")
    assert len(rows) == 1
    assert rows[0].form_uuid is None
    assert rows[0].database_table_name == "t1"


def test_gui_lai_cung_bang_khac_form_uuid_thi_ghi_de_khong_tao_dong_moi(db_session):
    """Full-snapshot lần sau gửi lại đúng bảng đó nhưng `formUuid` khác (hoặc thiếu) — ghi đè dòng
    cũ, không tạo dòng mới, vì khoá so khớp là bảng chứ không phải form."""
    handle_authorization_sync(db_session, _envelope(_batch(_user(forms=(("f1", "t1"),)))), 1000)
    result = handle_authorization_sync(
        db_session, _envelope(_batch(_user(forms=(("f2", "t1"),)))), 2000
    )
    assert result.results[0].status == "SUCCESS"
    assert (result.results[0].applied.upserted, result.results[0].applied.deleted) == (1, 0)
    rows = get_user_permissions(db_session, "u1")
    assert len(rows) == 1
    assert rows[0].form_uuid == "f2"


def test_trung_datasource_id_trong_1_form_raise_duplicate_datasource_id(db_session):
    data = {
        "userId": "u1", "isAdmin": False,
        "formQueries": [{
            "formUuid": "f1",
            "tableInfo": {
                "databaseTableName": "t1",
                "queries": [
                    {"datasourceId": "d1", "datasourceType": "postgresql", "query": "SELECT 1"},
                    {"datasourceId": "d1", "datasourceType": "clickhouse", "query": "SELECT 2"},
                ],
            },
        }],
    }
    with pytest.raises(SyncHookError) as exc:
        handle_authorization_sync(db_session, _envelope(_batch(data)), 1000)
    assert exc.value.http_status == 400
    assert exc.value.error_code is SyncErrorCode.DUPLICATE_DATASOURCE_ID
    assert "d1" in exc.value.message
    assert get_user_permissions(db_session, "u1") == []


def test_1_user_loi_cau_truc_thi_ca_batch_khong_ai_duoc_ap_dung(db_session):
    """all-or-nothing: user thứ 2 trùng bảng thì user thứ 1 (hợp lệ) cũng không được áp dụng."""
    valid_user = _user("u1", (("f1", "t1"),))
    invalid_user = _user("u2", (("f2", "t2"), ("f3", "t2")))
    with pytest.raises(SyncHookError):
        handle_authorization_sync(db_session, _envelope(_batch(valid_user, invalid_user)), 1000)
    assert get_user_permissions(db_session, "u1") == []
    assert get_user_permissions(db_session, "u2") == []


def test_ban_tin_cu_hon_van_duoc_ap_dung(db_session):
    """Không còn STALE: 1 user được áp lại với sync_version NHỎ HƠN lần trước vẫn thành công —
    thứ tự thời gian không còn ý nghĩa, chỉ còn snapshot mới nhất thắng."""
    handle_authorization_sync(db_session, _envelope(_batch(_user())), 2000)
    result = handle_authorization_sync(db_session, _envelope(_batch(_user())), 1000)
    assert result.results[0].status == "SUCCESS"
    assert len(get_user_permissions(db_session, "u1")) == 1


def test_thu_hoi_het_quyen_bang_form_queries_rong(db_session):
    handle_authorization_sync(db_session, _envelope(_batch(_user())), 1000)
    data = _user()
    data["formQueries"] = []
    result = handle_authorization_sync(db_session, _envelope(_batch(data)), 2000)
    assert result.results[0].status == "SUCCESS"
    assert (result.results[0].applied.upserted, result.results[0].applied.deleted) == (0, 1)
    assert get_user_permissions(db_session, "u1") == []
