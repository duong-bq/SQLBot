"""Test hàm thuần map AiUserPermission -> response output. Không cần DB."""

from datetime import datetime, timezone

from apps.hooks.models.ai_sync_model import AiUserPermission
from apps.hooks.schemas.permission_query_schema import (
    UserPermissionDetailOut,
    to_permission_detail,
)


def _row(**overrides) -> AiUserPermission:
    defaults = dict(
        user_id="u1",
        full_name="Nguyễn Văn A",
        is_admin=False,
        form_uuid="f1",
        database_table_name="t1",
        table_display_name="Bảng 1",
        table_description="Mô tả",
        domain_code="LV_DAN_CU",
        domain_uuid="lv-uuid-5678",
        domain_name="Lĩnh vực Dân cư",
        domain_description="Mô tả lĩnh vực",
        fields=[{"id": "a", "name": "A", "description": None}],
        queries=[{"datasourceId": "d1", "datasourceType": "postgresql", "query": "SELECT 1"}],
        sync_version=100,
        synced_at=datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return AiUserPermission(**defaults)


def test_rong_tra_toan_null_va_form_queries_rong():
    result = to_permission_detail("u-khong-ton-tai", [])
    assert result == UserPermissionDetailOut(
        userId="u-khong-ton-tai",
        fullName=None,
        isAdmin=None,
        syncVersion=None,
        syncedAt=None,
        formQueries=[],
    )


def test_co_du_lieu_map_dung_field():
    rows = [_row()]
    result = to_permission_detail("u1", rows)
    assert result.userId == "u1"
    assert result.fullName == "Nguyễn Văn A"
    assert result.isAdmin is False
    assert result.syncVersion == 100
    assert result.syncedAt == datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc)
    assert len(result.formQueries) == 1
    form = result.formQueries[0]
    assert form.formUuid == "f1"
    assert form.tableInfo.databaseTableName == "t1"
    assert form.tableInfo.tableDisplayName == "Bảng 1"
    assert form.tableInfo.linhVucMa == "LV_DAN_CU"
    assert form.tableInfo.linhVucUuid == "lv-uuid-5678"
    assert form.tableInfo.linhVucName == "Lĩnh vực Dân cư"
    assert form.tableInfo.linhVucDescription == "Mô tả lĩnh vực"
    assert form.tableInfo.fields == [{"id": "a", "name": "A", "description": None}]
    assert form.tableInfo.queries == [{"datasourceId": "d1", "datasourceType": "postgresql", "query": "SELECT 1"}]


def test_nhieu_form_lay_sync_version_tu_dong_dau():
    rows = [_row(form_uuid="f1", sync_version=200), _row(form_uuid="f2", sync_version=200)]
    result = to_permission_detail("u1", rows)
    assert result.syncVersion == 200
    assert len(result.formQueries) == 2
    assert {f.formUuid for f in result.formQueries} == {"f1", "f2"}


def test_domain_none_khong_loi():
    row = _row(domain_code=None, domain_uuid=None, domain_name=None, domain_description=None)
    result = to_permission_detail("u1", [row])
    form = result.formQueries[0]
    assert form.tableInfo.linhVucMa is None
    assert form.tableInfo.linhVucUuid is None
    assert form.tableInfo.linhVucName is None
    assert form.tableInfo.linhVucDescription is None
