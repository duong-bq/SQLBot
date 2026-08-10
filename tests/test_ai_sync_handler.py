"""Test handler actionType=1: validate payload, chống bản tin lùi, áp snapshot."""

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


def _data(user_id="u1", forms=(("f1", "t1"),)):
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


def test_ap_snapshot_thanh_cong(db_session):
    result = handle_authorization_sync(db_session, _envelope(_data()), 1000)
    assert result.status is SyncStatus.SUCCESS
    assert result.user_id == "u1"
    assert (result.upserted, result.deleted) == (1, 0)
    assert len(get_user_permissions(db_session, "u1")) == 1


def test_payload_sai_raise_invalid_payload(db_session):
    with pytest.raises(SyncHookError) as exc:
        handle_authorization_sync(db_session, _envelope({"userId": "u1"}), 1000)
    assert exc.value.http_status == 400
    assert exc.value.error_code is SyncErrorCode.INVALID_PAYLOAD


def test_trung_form_uuid_raise_duplicate_form_uuid(db_session):
    data = _data(forms=(("f1", "t1"), ("f1", "t2")))
    with pytest.raises(SyncHookError) as exc:
        handle_authorization_sync(db_session, _envelope(data), 1000)
    assert exc.value.http_status == 400
    assert exc.value.error_code is SyncErrorCode.DUPLICATE_FORM_UUID
    assert "f1" in exc.value.message


def test_ban_tin_lui_bi_bo_qua(db_session):
    _mark_applied(db_session, "u1", 2000)
    result = handle_authorization_sync(db_session, _envelope(_data()), 1500)
    assert result.status is SyncStatus.STALE
    assert (result.upserted, result.deleted) == (0, 0)
    assert get_user_permissions(db_session, "u1") == []


def test_ban_tin_cung_version_bi_bo_qua(db_session):
    _mark_applied(db_session, "u1", 2000)
    result = handle_authorization_sync(db_session, _envelope(_data()), 2000)
    assert result.status is SyncStatus.STALE


def test_ban_tin_moi_hon_duoc_ap_dung(db_session):
    _mark_applied(db_session, "u1", 2000)
    result = handle_authorization_sync(db_session, _envelope(_data()), 2001)
    assert result.status is SyncStatus.SUCCESS
    assert len(get_user_permissions(db_session, "u1")) == 1


def test_thu_hoi_het_quyen_bang_form_queries_rong(db_session):
    handle_authorization_sync(db_session, _envelope(_data()), 1000)
    data = _data()
    data["formQueries"] = []
    result = handle_authorization_sync(db_session, _envelope(data), 2000)
    assert result.status is SyncStatus.SUCCESS
    assert (result.upserted, result.deleted) == (0, 1)
    assert get_user_permissions(db_session, "u1") == []
