"""Test đầu-cuối: HTTP thật + Postgres thật, không patch gì.

Đây là test duy nhất chứng minh cả chuỗi hoạt động: route → audit log → bảng quyền. Các test khác
hoặc chỉ kiểm giao thức (patch crud), hoặc chỉ kiểm nghiệp vụ (gọi trực tiếp handler).
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from apps.hooks.crud.ai_sync_log import get_log_by_idempotency_key
from apps.hooks.crud.ai_user_permission import get_user_permissions


class _FakeAuthMiddleware(BaseHTTPMiddleware):
    """Gán `request.state.current_user`, mô phỏng việc TokenMiddleware thật làm sau khi giải mã JWT."""

    async def dispatch(self, request, call_next):
        request.state.current_user = SimpleNamespace(isAdmin=False, weight=1, oid=1)
        return await call_next(request)


def _body(user_id: str, timestamp: str, form_uuids: list[str]) -> dict:
    return {
        "actionType": 1,
        "version": "1.0",
        "timestamp": timestamp,
        "data": {
            "userId": user_id,
            "fullName": "Nguyễn Văn A",
            "isAdmin": False,
            "formQueries": [
                {
                    "formUuid": form_uuid,
                    "tableInfo": {
                        "databaseTableName": "kdl_nhan_khau_row_values",
                        "tableDisplayName": "Dữ liệu Nhân khẩu",
                        "tableDescription": "Bảng lưu trữ thông tin cư trú của công dân",
                        "fields": [
                            {"id": "province_id", "name": "Mã Tỉnh/Thành", "description": "Mã định danh"},
                        ],
                    },
                    "postgresQuery": "SELECT * FROM kdl_nhan_khau_row_values WHERE province_id = '01'",
                    "clickHouseQuery": "SELECT * FROM kdl_nhan_khau_row_values WHERE province_id = '01'",
                }
                for form_uuid in form_uuids
            ],
        },
    }


@pytest.fixture()
def e2e_client(db_session):
    """App thật (router hook) nối vào đúng session Postgres của fixture db_session.

    Có đủ `RequestContextMiddleware` + middleware giả lập user đã đăng nhập (`ws_admin`) để
    `require_permissions` trên route hoạt động đúng như trên app thật.
    """
    from apps.hooks.api import ai_sync
    from apps.system.schemas.permission import RequestContextMiddleware
    from common.core.db import get_session

    app = FastAPI()
    app.include_router(ai_sync.router, prefix="/api/v1")
    app.dependency_overrides[get_session] = lambda: db_session
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(_FakeAuthMiddleware)
    return TestClient(app)


def _headers(key: str) -> dict:
    return {"X-Request-ID": str(uuid.uuid4()), "X-Idempotency-Key": key}


def test_e2e_dong_bo_roi_thu_hoi_va_retry(e2e_client, db_session):
    # 1. Bản tin đầu: cấp 2 form
    resp = e2e_client.post(
        "/api/v1/hooks/ai-sync",
        json=_body("usr-e2e", "2026-08-10T10:00:00Z", ["form-1", "form-2"]),
        headers=_headers("idem-e2e-1"),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "SUCCESS"
    assert resp.json()["applied"] == {"upserted": 2, "deleted": 0}
    assert {r.form_uuid for r in get_user_permissions(db_session, "usr-e2e")} == {"form-1", "form-2"}
    log = get_log_by_idempotency_key(db_session, "idem-e2e-1")
    assert log.status == "SUCCESS"
    assert log.sync_version == 1786356000000
    assert log.processed_at is not None
    assert log.request_payload["data"]["userId"] == "usr-e2e"

    # 2. Retry đúng bản tin đó: DUPLICATE, không xử lý lại
    again = e2e_client.post(
        "/api/v1/hooks/ai-sync",
        json=_body("usr-e2e", "2026-08-10T10:00:00Z", ["form-1", "form-2"]),
        headers=_headers("idem-e2e-1"),
    )
    assert again.json()["status"] == "DUPLICATE"

    # 3. Bản tin mới hơn chỉ còn form-2: form-1 bị thu hồi
    newer = e2e_client.post(
        "/api/v1/hooks/ai-sync",
        json=_body("usr-e2e", "2026-08-10T11:00:00Z", ["form-2"]),
        headers=_headers("idem-e2e-2"),
    )
    assert newer.json()["applied"] == {"upserted": 1, "deleted": 1}
    assert {r.form_uuid for r in get_user_permissions(db_session, "usr-e2e")} == {"form-2"}

    # 4. Bản tin CŨ gửi lại (idempotency key khác): bị chặn vì lùi version
    stale = e2e_client.post(
        "/api/v1/hooks/ai-sync",
        json=_body("usr-e2e", "2026-08-10T09:00:00Z", ["form-1", "form-2"]),
        headers=_headers("idem-e2e-3"),
    )
    assert stale.status_code == 200
    assert stale.json()["status"] == "STALE"
    assert {r.form_uuid for r in get_user_permissions(db_session, "usr-e2e")} == {"form-2"}

    # 5. Thu hồi hết quyền
    revoke = _body("usr-e2e", "2026-08-10T12:00:00Z", [])
    revoked = e2e_client.post("/api/v1/hooks/ai-sync", json=revoke, headers=_headers("idem-e2e-4"))
    assert revoked.json()["status"] == "SUCCESS"
    assert get_user_permissions(db_session, "usr-e2e") == []

    # 6. Sau khi thu hồi hết, bản tin cũ vẫn không được áp ngược
    #    (mốc version đọc từ bảng log nên không mất khi bảng quyền trống)
    late = e2e_client.post(
        "/api/v1/hooks/ai-sync",
        json=_body("usr-e2e", "2026-08-10T11:30:00Z", ["form-1"]),
        headers=_headers("idem-e2e-5"),
    )
    assert late.json()["status"] == "STALE"
    assert get_user_permissions(db_session, "usr-e2e") == []


def test_e2e_action_type_chua_ho_tro_van_de_lai_vet(e2e_client, db_session):
    body = _body("usr-e2e-2", "2026-08-10T10:00:00Z", [])
    body["actionType"] = 3
    resp = e2e_client.post("/api/v1/hooks/ai-sync", json=body, headers=_headers("idem-e2e-unsup"))
    assert resp.status_code == 422
    assert resp.json()["errorCode"] == "UNSUPPORTED_ACTION_TYPE"
    log = get_log_by_idempotency_key(db_session, "idem-e2e-unsup")
    assert log.status == "FAILED"
    assert log.action_type == 3
    assert log.error_code == "UNSUPPORTED_ACTION_TYPE"


def test_e2e_payload_sai_van_de_lai_vet(e2e_client, db_session):
    body = {"actionType": 1, "version": "1.0", "timestamp": "2026-08-10T10:00:00Z", "data": {"userId": "u-bad"}}
    resp = e2e_client.post("/api/v1/hooks/ai-sync", json=body, headers=_headers("idem-e2e-badpayload"))
    assert resp.status_code == 400
    assert resp.json()["errorCode"] == "INVALID_PAYLOAD"
    log = get_log_by_idempotency_key(db_session, "idem-e2e-badpayload")
    assert log.status == "FAILED"
    assert log.user_id == "u-bad"
    assert get_user_permissions(db_session, "u-bad") == []
