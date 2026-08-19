"""Test tầng giao thức của router đọc quyền: xác thực ws_admin, response bọc envelope {code,data,msg}.

Patch tầng crud nên KHÔNG cần DB. Khác `test_ai_sync_api.py`: router này KHÔNG trả JSONResponse thô
nên cần bật thêm `ResponseMiddleware` trong app test để xác nhận đúng hành vi bọc envelope.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware


def _user(*, is_admin: bool = False, weight: int = 1, oid: int = 1):
    return SimpleNamespace(isAdmin=is_admin, weight=weight, oid=oid)


class _FakeAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, user):
        super().__init__(app)
        self.user = user

    async def dispatch(self, request, call_next):
        if self.user is not None:
            request.state.current_user = self.user
        return await call_next(request)


def _build_app(user) -> FastAPI:
    from apps.hooks.api import permission_query
    from apps.system.schemas.permission import RequestContextMiddleware
    from common.core.db import get_session
    from common.core.response_middleware import ResponseMiddleware, exception_handler

    app = FastAPI()
    app.include_router(permission_query.router, prefix="/api/v1")
    app.dependency_overrides[get_session] = lambda: MagicMock()
    app.add_middleware(ResponseMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(_FakeAuthMiddleware, user=user)
    app.add_exception_handler(StarletteHTTPException, exception_handler.http_exception_handler)
    app.add_exception_handler(Exception, exception_handler.global_exception_handler)
    return app


@pytest.fixture()
def client():
    return TestClient(_build_app(_user(weight=1)), raise_server_exceptions=False)


@pytest.fixture()
def crud_patches():
    with (
        patch(
            "apps.hooks.api.permission_query.list_users_with_permission_summary",
            return_value=[],
        ) as list_summary,
        patch(
            "apps.hooks.api.permission_query.get_user_permissions",
            return_value=[],
        ) as get_perms,
    ):
        yield {"list_summary": list_summary, "get_perms": get_perms}


def test_chua_dang_nhap_tra_401(crud_patches):
    client = TestClient(_build_app(user=None), raise_server_exceptions=False)
    resp = client.get("/api/v1/hooks/ai-sync/permissions")
    assert resp.status_code == 401


def test_thieu_quyen_ws_admin_tra_500(crud_patches):
    client = TestClient(_build_app(_user(weight=0, is_admin=False)), raise_server_exceptions=False)
    resp = client.get("/api/v1/hooks/ai-sync/permissions")
    assert resp.status_code == 500


def test_list_summary_tra_ve_duoc_boc_envelope(client, crud_patches):
    crud_patches["list_summary"].return_value = [
        {
            "user_id": "u1", "full_name": "A", "is_admin": False,
            "table_count": 2, "sync_version": 100, "synced_at": "2026-08-12T10:00:00Z",
        }
    ]
    resp = client.get("/api/v1/hooks/ai-sync/permissions")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"code", "data", "msg"}
    assert body["code"] == 0
    assert body["data"] == [
        {
            "userId": "u1", "fullName": "A", "isAdmin": False,
            "tableCount": 2, "syncVersion": 100, "syncedAt": "2026-08-12T10:00:00Z",
        }
    ]


def test_list_summary_rong_tra_mang_rong(client, crud_patches):
    resp = client.get("/api/v1/hooks/ai-sync/permissions")
    assert resp.status_code == 200
    assert resp.json()["data"] == []


def test_detail_user_khong_ton_tai_tra_200_rong(client, crud_patches):
    resp = client.get("/api/v1/hooks/ai-sync/permissions/u-khong-ton-tai")
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["userId"] == "u-khong-ton-tai"
    assert body["fullName"] is None
    assert body["isAdmin"] is None
    assert body["formQueries"] == []


def test_detail_goi_dung_user_id_tu_path(client, crud_patches):
    client.get("/api/v1/hooks/ai-sync/permissions/usr-xyz")
    crud_patches["get_perms"].assert_called_once()
    assert crud_patches["get_perms"].call_args[0][1] == "usr-xyz"


def test_detail_co_domain_fields_trong_table_info(client, crud_patches):
    from datetime import datetime, timezone

    from apps.hooks.models.ai_sync_model import AiUserPermission

    crud_patches["get_perms"].return_value = [
        AiUserPermission(
            user_id="usr-1", full_name="A", is_admin=False, form_uuid="f1",
            database_table_name="t1", table_display_name="Bảng 1",
            domain_code="LV_DAN_CU", domain_uuid="lv-uuid-5678", domain_name="Lĩnh vực Dân cư",
            domain_description="Mô tả lĩnh vực",
            queries=[{"datasourceId": "d1", "datasourceType": "postgresql", "query": "SELECT 1"}],
            sync_version=100,
            synced_at=datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc),
        )
    ]
    resp = client.get("/api/v1/hooks/ai-sync/permissions/usr-1")
    assert resp.status_code == 200
    table_info = resp.json()["data"]["formQueries"][0]["tableInfo"]
    assert table_info["linhVucMa"] == "LV_DAN_CU"
    assert table_info["linhVucUuid"] == "lv-uuid-5678"
    assert table_info["linhVucName"] == "Lĩnh vực Dân cư"
    assert table_info["linhVucDescription"] == "Mô tả lĩnh vực"
    assert table_info["queries"] == [{"datasourceId": "d1", "datasourceType": "postgresql", "query": "SELECT 1"}]
