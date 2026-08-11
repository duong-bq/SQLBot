"""Test tầng giao thức của route hook: xác thực JWT + quyền ws_admin, idempotency, bảng lỗi.

Patch tầng crud và handler nên KHÔNG cần DB — test này chỉ kiểm hợp đồng HTTP. Nghiệp vụ đã có
test riêng chạy trên Postgres thật.

Hook dùng chung cơ chế xác thực của SQLBot (`require_permissions` đọc `request.state.current_user`
qua contextvar của `RequestContextMiddleware`), không có static token riêng. App test dựng một
middleware giả lập để gán `current_user` — mô phỏng đúng việc `TokenMiddleware` thật làm sau khi
giải mã JWT.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from apps.hooks.constants import SyncActionType, SyncStatus
from apps.hooks.handlers.authorization import HandlerResult
from apps.hooks.schemas.ai_sync_schema import SyncAppliedCounts, UserSyncResult

VALID_BODY = {
    "actionType": 1,
    "version": "1.0",
    "timestamp": "2026-08-10T10:00:00Z",
    "data": {"users": [{"userId": "usr-1", "fullName": "A", "isAdmin": False, "formQueries": []}]},
}
HEADERS = {"X-Request-ID": "req-1", "X-Idempotency-Key": "idem-1"}


def _user(*, is_admin: bool = False, weight: int = 1, oid: int = 1):
    """Đối tượng tối giản đủ 3 thuộc tính `require_permissions` cần đọc."""
    return SimpleNamespace(isAdmin=is_admin, weight=weight, oid=oid)


class _FakeAuthMiddleware(BaseHTTPMiddleware):
    """Gán `request.state.current_user`, mô phỏng việc TokenMiddleware thật làm sau khi giải mã JWT.

    `user=None` mô phỏng request chưa xác thực (không có `X-SQLBOT-TOKEN` hợp lệ).
    """

    def __init__(self, app, user):
        super().__init__(app)
        self.user = user

    async def dispatch(self, request, call_next):
        if self.user is not None:
            request.state.current_user = self.user
        return await call_next(request)


def _build_app(user) -> FastAPI:
    """App tối giản: router hook + đúng 2 middleware mà `require_permissions` cần, + exception
    handler giống `main.py` để lỗi thiếu quyền (Exception thường) trả 500 như trên app thật."""
    from apps.hooks.api import ai_sync
    from apps.system.schemas.permission import RequestContextMiddleware
    from common.core.db import get_session
    from common.core.response_middleware import exception_handler

    app = FastAPI()
    app.include_router(ai_sync.router, prefix="/api/v1")
    app.dependency_overrides[get_session] = lambda: MagicMock()
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(_FakeAuthMiddleware, user=user)
    app.add_exception_handler(StarletteHTTPException, exception_handler.http_exception_handler)
    app.add_exception_handler(Exception, exception_handler.global_exception_handler)
    return app


@pytest.fixture()
def client():
    """Client với user đã đăng nhập và có quyền ws_admin — ca mặc định cho các test nghiệp vụ."""
    return TestClient(_build_app(_user(weight=1)), raise_server_exceptions=False)


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
        return_value=result
        or HandlerResult(
            status=SyncStatus.SUCCESS,
            results=[
                UserSyncResult(
                    userId="usr-1", status="SUCCESS",
                    applied=SyncAppliedCounts(upserted=2, deleted=1),
                )
            ],
        )
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
    assert body["results"] == [
        {"userId": "usr-1", "status": "SUCCESS", "applied": {"upserted": 2, "deleted": 1}}
    ]
    assert body["requestId"] == "req-1"
    assert body["idempotencyKey"] == "idem-1"
    assert body["applied"] == {"upserted": 2, "deleted": 1}
    assert body["errorCode"] is None
    # sync_version = epoch millis của 2026-08-10T10:00:00Z
    assert handler.call_args[0][2] == 1786356000000
    # response KHÔNG bị bọc envelope {code,data,msg}
    assert "code" not in body and "data" not in body


def test_chua_dang_nhap_tra_401_va_khong_ghi_log(crud_patches):
    """`request.state.current_user` không tồn tại — đúng ca TokenMiddleware chưa xác thực được."""
    client = TestClient(_build_app(user=None), raise_server_exceptions=False)
    resp = client.post("/api/v1/hooks/ai-sync", json=VALID_BODY, headers=HEADERS)
    assert resp.status_code == 401
    crud_patches["create_log"].assert_not_called()


def test_user_thuong_khong_du_quyen_tra_loi(crud_patches):
    """`weight=0` và không phải admin: `require_permissions` raise Exception thường → 500.

    Đây là quy ước chung của SQLBot (không có 403 riêng cho thiếu quyền), không phải hành vi
    riêng của hook — xem `BACKEND_ARCHITECTURE.md` §7 và "bẫy" ở `OPERATIONS.md`.
    """
    client = TestClient(_build_app(_user(weight=0, is_admin=False)), raise_server_exceptions=False)
    resp = client.post("/api/v1/hooks/ai-sync", json=VALID_BODY, headers=HEADERS)
    assert resp.status_code == 500
    crud_patches["create_log"].assert_not_called()


def test_admin_toan_cuc_van_goi_duoc_du_weight_0(client, crud_patches):
    """`isAdmin=True` bỏ qua kiểm tra role — khớp hành vi `require_permissions` dùng chung toàn app."""
    ctx, handler = _patch_handler()
    admin_client = TestClient(_build_app(_user(weight=0, is_admin=True)), raise_server_exceptions=False)
    with ctx:
        resp = admin_client.post("/api/v1/hooks/ai-sync", json=VALID_BODY, headers=HEADERS)
    assert resp.status_code == 200
    handler.assert_called_once()


def test_thieu_idempotency_key_tra_400(client, crud_patches):
    resp = client.post("/api/v1/hooks/ai-sync", json=VALID_BODY, headers={"X-Request-ID": "req-1"})
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


def test_ket_qua_voi_user_stale_tra_200_va_giu_status_stale_trong_results(client, crud_patches):
    ctx, _ = _patch_handler(
        result=HandlerResult(
            status=SyncStatus.SUCCESS,
            results=[UserSyncResult(userId="usr-1", status="STALE")],
        )
    )
    with ctx:
        resp = client.post("/api/v1/hooks/ai-sync", json=VALID_BODY, headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["results"] == [
        {"userId": "usr-1", "status": "STALE", "applied": {"upserted": 0, "deleted": 0}}
    ]
    assert body["applied"] == {"upserted": 0, "deleted": 0}


def test_body_voi_nhieu_user_tra_ket_qua_tung_user(client, crud_patches):
    ctx, _ = _patch_handler(
        result=HandlerResult(
            status=SyncStatus.SUCCESS,
            results=[
                UserSyncResult(
                    userId="usr-1", status="SUCCESS",
                    applied=SyncAppliedCounts(upserted=1, deleted=0),
                ),
                UserSyncResult(userId="usr-2", status="STALE"),
            ],
        )
    )
    body = dict(
        VALID_BODY,
        data={
            "users": [
                {"userId": "usr-1", "isAdmin": False, "formQueries": []},
                {"userId": "usr-2", "isAdmin": False, "formQueries": []},
            ]
        },
    )
    with ctx:
        resp = client.post("/api/v1/hooks/ai-sync", json=body, headers=HEADERS)
    assert resp.status_code == 200
    r = resp.json()
    assert r["applied"] == {"upserted": 1, "deleted": 0}
    assert [x["userId"] for x in r["results"]] == ["usr-1", "usr-2"]
    assert [x["status"] for x in r["results"]] == ["SUCCESS", "STALE"]


def test_userid_trung_trong_batch_tra_400(client, crud_patches):
    from apps.hooks.constants import SyncErrorCode
    from apps.hooks.errors import SyncHookError

    ctx, _ = _patch_handler(
        error=SyncHookError(400, SyncErrorCode.DUPLICATE_USER_ID, "userId bị lặp trong payload: usr-1")
    )
    body = dict(
        VALID_BODY,
        data={
            "users": [
                {"userId": "usr-1", "isAdmin": False, "formQueries": []},
                {"userId": "usr-1", "isAdmin": False, "formQueries": []},
            ]
        },
    )
    with ctx:
        resp = client.post("/api/v1/hooks/ai-sync", json=body, headers=HEADERS)
    assert resp.status_code == 400
    assert resp.json()["errorCode"] == "DUPLICATE_USER_ID"


def test_users_rong_tra_400(client, crud_patches):
    from apps.hooks.constants import SyncErrorCode
    from apps.hooks.errors import SyncHookError

    ctx, _ = _patch_handler(
        error=SyncHookError(400, SyncErrorCode.EMPTY_USER_LIST, "data.users không được rỗng")
    )
    body = dict(VALID_BODY, data={"users": []})
    with ctx:
        resp = client.post("/api/v1/hooks/ai-sync", json=body, headers=HEADERS)
    assert resp.status_code == 400
    assert resp.json()["errorCode"] == "EMPTY_USER_LIST"


def test_path_hooks_khong_nam_trong_whitelist():
    """`/hooks/*` phải đi qua TokenMiddleware như mọi endpoint khác — không có ngoại lệ riêng."""
    from common.utils.whitelist import whiteUtils

    assert whiteUtils.is_whitelisted("/api/v1/hooks/ai-sync") is False
