"""Test handler actionType=1: validate batch, chống bản tin lùi theo từng user, áp snapshot."""

import pytest

from apps.hooks.constants import SyncActionType, SyncErrorCode, SyncStatus
from apps.hooks.crud.ai_sync_log import create_received_log, finish_log
from apps.hooks.crud.ai_user_permission import get_user_permissions
from apps.hooks.errors import SyncHookError
from apps.hooks.handlers import ACTION_HANDLERS
from apps.hooks.handlers.authorization import handle_authorization_sync
from apps.hooks.schemas.ai_sync_schema import SyncEnvelope


def _envelope(data: dict) -> SyncEnvelope:
    return SyncEnvelope.model_validate(
        {"actionType": 1, "version": "1.0", "timestamp": "2026-08-10T10:00:00Z", "data": data}
    )


def _user(user_id="u1", forms=(("f1", "t1"),)):
    return {
        "userId": user_id,
        "fullName": "Nguyễn Văn A",
        "isAdmin": False,
        "formQueries": [
            {
                "formUuid": form_uuid,
                "tableInfo": {"databaseTableName": table, "fields": [{"id": "a"}]},
                "postgresQuery": f"SELECT * FROM {table}",
            }
            for form_uuid, table in forms
        ],
    }


def _batch(*users) -> dict:
    return {"users": list(users) if users else [_user()]}


def _mark_applied(session, user_id: str, version: int) -> None:
    """Giả lập một bản tin đã áp dụng thành công ở version cho trước."""
    log = create_received_log(
        session, request_id="r", idempotency_key=f"k-{user_id}-{version}",
        action_type=1, schema_version="1.0", user_id=user_id, request_payload={},
    )
    finish_log(session, log, status=SyncStatus.SUCCESS.value, sync_version=version)


def test_dispatch_chi_co_authorization_sync(db_session):
    assert ACTION_HANDLERS[SyncActionType.AUTHORIZATION_SYNC] is handle_authorization_sync
    assert set(ACTION_HANDLERS) == {SyncActionType.AUTHORIZATION_SYNC}


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


def test_trung_form_uuid_trong_1_user_raise_duplicate_form_uuid(db_session):
    data = _user(forms=(("f1", "t1"), ("f1", "t2")))
    with pytest.raises(SyncHookError) as exc:
        handle_authorization_sync(db_session, _envelope(_batch(data)), 1000)
    assert exc.value.http_status == 400
    assert exc.value.error_code is SyncErrorCode.DUPLICATE_FORM_UUID
    assert "f1" in exc.value.message


def test_1_user_loi_cau_truc_thi_ca_batch_khong_ai_duoc_ap_dung(db_session):
    """all-or-nothing: user thứ 2 trùng formUuid thì user thứ 1 (hợp lệ) cũng không được áp dụng."""
    valid_user = _user("u1", (("f1", "t1"),))
    invalid_user = _user("u2", (("f2", "t2"), ("f2", "t3")))
    with pytest.raises(SyncHookError):
        handle_authorization_sync(db_session, _envelope(_batch(valid_user, invalid_user)), 1000)
    assert get_user_permissions(db_session, "u1") == []
    assert get_user_permissions(db_session, "u2") == []


def test_ban_tin_lui_bi_bo_qua(db_session):
    _mark_applied(db_session, "u1", 2000)
    result = handle_authorization_sync(db_session, _envelope(_batch(_user())), 1500)
    assert result.status is SyncStatus.SUCCESS
    assert result.results[0].status == "STALE"
    assert (result.results[0].applied.upserted, result.results[0].applied.deleted) == (0, 0)
    assert get_user_permissions(db_session, "u1") == []


def test_ban_tin_cung_version_bi_bo_qua(db_session):
    _mark_applied(db_session, "u1", 2000)
    result = handle_authorization_sync(db_session, _envelope(_batch(_user())), 2000)
    assert result.results[0].status == "STALE"


def test_ban_tin_moi_hon_duoc_ap_dung(db_session):
    _mark_applied(db_session, "u1", 2000)
    result = handle_authorization_sync(db_session, _envelope(_batch(_user())), 2001)
    assert result.results[0].status == "SUCCESS"
    assert len(get_user_permissions(db_session, "u1")) == 1


def test_stale_tinh_rieng_tung_user_trong_cung_batch(db_session):
    """u1 đã có mốc mới hơn (stale), u2 chưa từng đồng bộ (được áp dụng) — cùng 1 request."""
    _mark_applied(db_session, "u1", 5000)
    batch = _batch(_user("u1", (("f1", "t1"),)), _user("u2", (("f2", "t2"),)))
    result = handle_authorization_sync(db_session, _envelope(batch), 3000)
    statuses = {r.userId: r.status for r in result.results}
    assert statuses == {"u1": "STALE", "u2": "SUCCESS"}
    assert get_user_permissions(db_session, "u1") == []
    assert len(get_user_permissions(db_session, "u2")) == 1


def test_thu_hoi_het_quyen_bang_form_queries_rong(db_session):
    handle_authorization_sync(db_session, _envelope(_batch(_user())), 1000)
    data = _user()
    data["formQueries"] = []
    result = handle_authorization_sync(db_session, _envelope(_batch(data)), 2000)
    assert result.results[0].status == "SUCCESS"
    assert (result.results[0].applied.upserted, result.results[0].applied.deleted) == (0, 1)
    assert get_user_permissions(db_session, "u1") == []
