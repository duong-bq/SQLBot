"""Test tầng giao thức của route hook: xác thực, idempotency, bảng lỗi, dịch status.

Patch tầng crud và handler nên KHÔNG cần DB — test này chỉ kiểm hợp đồng HTTP. Nghiệp vụ đã có
test riêng chạy trên Postgres thật.
"""

import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from apps.hooks.constants import SyncActionType, SyncStatus
from apps.hooks.handlers.authorization import HandlerResult

VALID_BODY = {
    "actionType": 1,
    "version": "1.0",
    "timestamp": "2026-08-10T10:00:00Z",
    "data": {"userId": "usr-1", "fullName": "A", "isAdmin": False, "formQueries": []},
}
TOKEN = "token-test-123"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "X-Request-ID": "req-1",
    "X-Idempotency-Key": "idem-1",
}


@pytest.fixture()
def client():
    """App tối giản chỉ mount router hook, session là MagicMock."""
    from apps.hooks.api import ai_sync
    from common.core.config import settings
    from common.core.db import get_session

    app = FastAPI()
    app.include_router(ai_sync.router, prefix="/api/v1")
    app.dependency_overrides[get_session] = lambda: MagicMock()

    original_enabled = settings.AI_SYNC_HOOK_ENABLED
    original_token = settings.AI_SYNC_HOOK_TOKEN
    settings.AI_SYNC_HOOK_ENABLED = True
    settings.AI_SYNC_HOOK_TOKEN = TOKEN
    yield TestClient(app)
    settings.AI_SYNC_HOOK_ENABLED = original_enabled
    settings.AI_SYNC_HOOK_TOKEN = original_token


def _fake_log(status=SyncStatus.RECEIVED.value, error_code=None, error_message=None):
    log = MagicMock()
    log.id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    log.action_type = 1
    log.status = status
    log.error_code = error_code
    log.error_message = error_message
    log.user_id = "usr-1"
    return log


@pytest.fixture()
def crud_patches():
    """Patch 3 hàm crud log mà route dùng; mặc định: chưa có log trùng, tạo log OK."""
    with (
        patch("apps.hooks.api.ai_sync.get_log_by_idempotency_key", return_value=None) as get_log,
        patch("apps.hooks.api.ai_sync.create_received_log", return_value=_fake_log()) as create_log,
        patch("apps.hooks.api.ai_sync.finish_log", side_effect=lambda s, log, **kw: log) as finish,
    ):
        yield {"get_log": get_log, "create_log": create_log, "finish_log": finish}


def _patch_handler(result=None, error=None):
    handler = MagicMock(
        return_value=result or HandlerResult(user_id="usr-1", status=SyncStatus.SUCCESS, upserted=2, deleted=1)
    )
    if error is not None:
        handler.side_effect = error
    from apps.hooks.handlers import ACTION_HANDLERS

    return patch.dict(ACTION_HANDLERS, {SyncActionType.AUTHORIZATION_SYNC: handler}), handler


def test_thanh_cong_tra_200_va_dem_dung(client, crud_patches):
    ctx, handler = _patch_handler()
    with ctx:
        resp = client.post("/api/v1/hooks/ai-sync", json=VALID_BODY, headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["actionType"] == 1
    assert body["userId"] == "usr-1"
    assert body["requestId"] == "req-1"
    assert body["idempotencyKey"] == "idem-1"
    assert body["applied"] == {"upserted": 2, "deleted": 1}
    assert body["errorCode"] is None
    # sync_version = epoch millis của 2026-08-10T10:00:00Z
    assert handler.call_args[0][2] == 1786356000000
    # response KHÔNG bị bọc envelope {code,data,msg}
    assert "code" not in body and "data" not in body


def test_khong_co_token_tra_401_va_khong_ghi_log(client, crud_patches):
    resp = client.post("/api/v1/hooks/ai-sync", json=VALID_BODY,
                       headers={"X-Idempotency-Key": "idem-1"})
    assert resp.status_code == 401
    assert resp.json()["errorCode"] == "UNAUTHORIZED"
    crud_patches["create_log"].assert_not_called()


def test_token_sai_tra_401(client, crud_patches):
    headers = dict(HEADERS, Authorization="Bearer sai-token")
    resp = client.post("/api/v1/hooks/ai-sync", json=VALID_BODY, headers=headers)
    assert resp.status_code == 401
    crud_patches["create_log"].assert_not_called()


def test_hook_tat_tra_503(client, crud_patches):
    from common.core.config import settings

    settings.AI_SYNC_HOOK_ENABLED = False
    resp = client.post("/api/v1/hooks/ai-sync", json=VALID_BODY, headers=HEADERS)
    settings.AI_SYNC_HOOK_ENABLED = True
    assert resp.status_code == 503
    assert resp.json()["errorCode"] == "HOOK_DISABLED"


def test_token_cau_hinh_rong_tra_503(client, crud_patches):
    from common.core.config import settings

    settings.AI_SYNC_HOOK_TOKEN = ""
    resp = client.post("/api/v1/hooks/ai-sync", json=VALID_BODY, headers=HEADERS)
    settings.AI_SYNC_HOOK_TOKEN = TOKEN
    assert resp.status_code == 503
    assert resp.json()["errorCode"] == "HOOK_DISABLED"


def test_thieu_idempotency_key_tra_400(client, crud_patches):
    headers = {"Authorization": f"Bearer {TOKEN}"}
    resp = client.post("/api/v1/hooks/ai-sync", json=VALID_BODY, headers=headers)
    assert resp.status_code == 400
    assert resp.json()["errorCode"] == "MISSING_IDEMPOTENCY_KEY"
    crud_patches["create_log"].assert_not_called()


def test_body_khong_phai_json_tra_400(client, crud_patches):
    resp = client.post(
        "/api/v1/hooks/ai-sync", content=b"khong-phai-json",
        headers=dict(HEADERS, **{"Content-Type": "application/json"}),
    )
    assert resp.status_code == 400
    assert resp.json()["errorCode"] == "MALFORMED_JSON"
    crud_patches["create_log"].assert_not_called()


def test_body_json_nhung_khong_phai_object_tra_400(client, crud_patches):
    resp = client.post("/api/v1/hooks/ai-sync", json=[1, 2, 3], headers=HEADERS)
    assert resp.status_code == 400
    assert resp.json()["errorCode"] == "MALFORMED_JSON"


@pytest.mark.parametrize("action_type", [2, 3, 4, 5, 6])
def test_action_type_chua_implement_tra_422_va_co_ghi_log(client, crud_patches, action_type):
    body = dict(VALID_BODY, actionType=action_type)
    resp = client.post("/api/v1/hooks/ai-sync", json=body, headers=HEADERS)
    assert resp.status_code == 422
    assert resp.json()["errorCode"] == "UNSUPPORTED_ACTION_TYPE"
    assert resp.json()["actionType"] == action_type
    crud_patches["create_log"].assert_called_once()
    assert crud_patches["finish_log"].call_args.kwargs["status"] == SyncStatus.FAILED.value


@pytest.mark.parametrize("action_type", ["1", 99, None, 0, True])
def test_action_type_khong_hop_le_tra_422(client, crud_patches, action_type):
    body = dict(VALID_BODY, actionType=action_type)
    resp = client.post("/api/v1/hooks/ai-sync", json=body, headers=HEADERS)
    assert resp.status_code == 422
    assert resp.json()["errorCode"] == "UNKNOWN_ACTION_TYPE"
    crud_patches["create_log"].assert_called_once()


def test_envelope_thieu_truong_tra_400(client, crud_patches):
    body = {"actionType": 1, "data": {"userId": "u1", "isAdmin": False, "formQueries": []}}
    resp = client.post("/api/v1/hooks/ai-sync", json=body, headers=HEADERS)
    assert resp.status_code == 400
    assert resp.json()["errorCode"] == "INVALID_ENVELOPE"
    crud_patches["create_log"].assert_called_once()


def test_trung_idempotency_key_tra_200_duplicate_va_khong_xu_ly_lai(client, crud_patches):
    crud_patches["get_log"].return_value = _fake_log(
        status=SyncStatus.SUCCESS.value, error_code=None, error_message=None
    )
    ctx, handler = _patch_handler()
    with ctx:
        resp = client.post("/api/v1/hooks/ai-sync", json=VALID_BODY, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["status"] == "DUPLICATE"
    assert resp.json()["logId"] == "11111111-1111-1111-1111-111111111111"
    handler.assert_not_called()
    crud_patches["create_log"].assert_not_called()


def test_race_idempotency_integrity_error_tra_200_duplicate(client, crud_patches):
    crud_patches["create_log"].side_effect = IntegrityError("insert", {}, Exception("dup"))
    crud_patches["get_log"].side_effect = [None, _fake_log(status=SyncStatus.SUCCESS.value)]
    ctx, handler = _patch_handler()
    with ctx:
        resp = client.post("/api/v1/hooks/ai-sync", json=VALID_BODY, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["status"] == "DUPLICATE"
    handler.assert_not_called()


def test_handler_raise_sync_hook_error_duoc_dich_dung(client, crud_patches):
    from apps.hooks.constants import SyncErrorCode
    from apps.hooks.errors import SyncHookError

    ctx, _ = _patch_handler(
        error=SyncHookError(400, SyncErrorCode.INVALID_PAYLOAD, "userId rỗng")
    )
    with ctx:
        resp = client.post("/api/v1/hooks/ai-sync", json=VALID_BODY, headers=HEADERS)
    assert resp.status_code == 400
    assert resp.json()["errorCode"] == "INVALID_PAYLOAD"
    assert resp.json()["errorMessage"] == "userId rỗng"
    assert crud_patches["finish_log"].call_args.kwargs["status"] == SyncStatus.FAILED.value


def test_handler_loi_khong_luong_truoc_tra_500(client, crud_patches):
    ctx, _ = _patch_handler(error=RuntimeError("bug"))
    with ctx:
        resp = client.post("/api/v1/hooks/ai-sync", json=VALID_BODY, headers=HEADERS)
    assert resp.status_code == 500
    assert resp.json()["errorCode"] == "INTERNAL_ERROR"
    assert crud_patches["finish_log"].call_args.kwargs["status"] == SyncStatus.FAILED.value


def test_ket_qua_stale_tra_200(client, crud_patches):
    ctx, _ = _patch_handler(result=HandlerResult(user_id="usr-1", status=SyncStatus.STALE))
    with ctx:
        resp = client.post("/api/v1/hooks/ai-sync", json=VALID_BODY, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["status"] == "STALE"
    assert resp.json()["applied"] == {"upserted": 0, "deleted": 0}


def test_path_hooks_nam_trong_whitelist_cua_token_middleware():
    from common.utils.whitelist import whiteUtils

    assert whiteUtils.is_whitelisted("/api/v1/hooks/ai-sync") is True
