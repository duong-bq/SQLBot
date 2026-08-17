"""Test validation envelope/payload và 3 hàm thuần. Không cần DB."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from apps.hooks.schemas.ai_sync_schema import (
    AuthorizationSyncBatch,
    AuthorizationSyncData,
    SyncEnvelope,
    SyncHookResponse,
    UserSyncResult,
    find_duplicate_datasource_id,
    find_duplicate_form_uuid,
    find_duplicate_user_id,
    normalize_queries,
    to_sync_version,
)

VALID_DATA = {
    "userId": "usr-12345-67890",
    "fullName": "Nguyễn Văn A",
    "isAdmin": False,
    "formQueries": [
        {
            "formUuid": "form-abcd-1234",
            "tableInfo": {
                "databaseTableName": "kdl_nhan_khau_row_values",
                "tableDisplayName": "Dữ liệu Nhân khẩu",
                "linhVucMa": "LV_DAN_CU",
                "linhVucUuid": "lv-uuid-5678",
                "linhVucName": "Lĩnh vực Dân cư",
                "linhVucDescription": "Lĩnh vực quản lý các thông tin liên quan đến dân cư",
                "queries": [
                    {"datasourceId": "ds-001", "datasourceType": "postgresql", "query": "SELECT * FROM kdl_nhan_khau_row_values WHERE province_id = '01'"},
                    {"datasourceId": "ds-002", "datasourceType": "clickhouse", "query": "SELECT * FROM kdl_nhan_khau_row_values WHERE province_id = '01'"},
                ],
            },
        }
    ],
}


def test_envelope_hop_le():
    env = SyncEnvelope.model_validate(
        {"actionType": 1, "version": "1.0", "timestamp": "2026-08-10T10:00:00Z", "data": VALID_DATA}
    )
    assert env.action_type == 1
    assert env.version == "1.0"
    assert env.timestamp == datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc)
    assert env.data["userId"] == "usr-12345-67890"


@pytest.mark.parametrize("missing", ["version", "timestamp", "data"])
def test_envelope_thieu_truong_bat_buoc(missing):
    body = {"actionType": 1, "version": "1.0", "timestamp": "2026-08-10T10:00:00Z", "data": VALID_DATA}
    body.pop(missing)
    with pytest.raises(ValidationError):
        SyncEnvelope.model_validate(body)


def test_payload_hop_le_va_parse_dung_alias():
    data = AuthorizationSyncData.model_validate(VALID_DATA)
    assert data.user_id == "usr-12345-67890"
    assert data.full_name == "Nguyễn Văn A"
    assert data.is_admin is False
    form = data.form_queries[0]
    assert form.form_uuid == "form-abcd-1234"
    assert form.table_info.database_table_name == "kdl_nhan_khau_row_values"
    assert form.table_info.domain_code == "LV_DAN_CU"
    assert form.table_info.domain_uuid == "lv-uuid-5678"
    assert form.table_info.domain_name == "Lĩnh vực Dân cư"
    assert form.table_info.domain_description == "Lĩnh vực quản lý các thông tin liên quan đến dân cư"
    assert len(form.table_info.queries) == 2
    assert form.table_info.queries[0].datasource_id == "ds-001"
    assert form.table_info.queries[0].datasource_type == "postgresql"
    assert form.table_info.queries[1].datasource_id == "ds-002"


def test_payload_bo_trong_form_queries_la_hop_le():
    data = AuthorizationSyncData.model_validate({"userId": "u1", "isAdmin": True, "formQueries": []})
    assert data.form_queries == []
    assert data.full_name is None


def test_table_info_thieu_domain_van_hop_le():
    from apps.hooks.schemas.ai_sync_schema import TableInfo

    info = TableInfo.model_validate({
        "databaseTableName": "t1",
        "queries": [{"datasourceId": "d1", "datasourceType": "postgresql", "query": "SELECT 1"}],
    })
    assert info.domain_code is None
    assert info.domain_uuid is None
    assert info.domain_name is None
    assert info.domain_description is None


@pytest.mark.parametrize("missing", ["userId", "isAdmin", "formQueries"])
def test_payload_thieu_truong_bat_buoc(missing):
    body = {"userId": "u1", "isAdmin": False, "formQueries": []}
    body.pop(missing)
    with pytest.raises(ValidationError):
        AuthorizationSyncData.model_validate(body)


def test_payload_user_id_rong_bi_loai():
    with pytest.raises(ValidationError):
        AuthorizationSyncData.model_validate({"userId": "", "isAdmin": False, "formQueries": []})


def test_form_thieu_table_info_bi_loai():
    with pytest.raises(ValidationError):
        AuthorizationSyncData.model_validate(
            {"userId": "u1", "isAdmin": False, "formQueries": [{"formUuid": "f1"}]}
        )


def test_table_info_field_thua_bi_bo_qua_am_tham():
    """`fields` không còn là field khai báo trong TableInfo — SW gửi kèm (payload cũ) bị
    `extra="ignore"` bỏ qua âm thầm, không lỗi, không có tác dụng."""
    data = AuthorizationSyncData.model_validate(
        {
            "userId": "u1", "isAdmin": False,
            "formQueries": [{
                "formUuid": "f1",
                "tableInfo": {
                    "databaseTableName": "t", "fields": [{"id": "a"}],
                    "queries": [{"datasourceId": "d1", "datasourceType": "postgresql", "query": "SELECT 1"}],
                },
            }],
        }
    )
    assert not hasattr(data.form_queries[0].table_info, "field_list")
    assert not hasattr(data.form_queries[0].table_info, "fields")


def test_table_info_table_description_thua_bi_bo_qua_am_tham():
    """`tableDescription` không còn là field khai báo trong TableInfo — mô tả bảng đã chuyển sang
    endpoint edit table riêng, gửi kèm (payload cũ) bị `extra="ignore"` bỏ qua âm thầm."""
    data = AuthorizationSyncData.model_validate(
        {
            "userId": "u1", "isAdmin": False,
            "formQueries": [{
                "formUuid": "f1",
                "tableInfo": {
                    "databaseTableName": "t", "tableDescription": "Mô tả cũ",
                    "queries": [{"datasourceId": "d1", "datasourceType": "postgresql", "query": "SELECT 1"}],
                },
            }],
        }
    )
    assert not hasattr(data.form_queries[0].table_info, "table_description")


def test_to_sync_version_doi_sang_epoch_millis():
    assert to_sync_version(datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc)) == 1786356000000


def test_to_sync_version_coi_datetime_naive_la_utc():
    naive = datetime(2026, 8, 10, 10, 0)
    aware = datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc)
    assert to_sync_version(naive) == to_sync_version(aware)


def test_to_sync_version_ton_trong_offset():
    plus7 = datetime(2026, 8, 10, 17, 0, tzinfo=timezone(timedelta(hours=7)))
    assert to_sync_version(plus7) == to_sync_version(datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc))


_ONE_QUERY = [{"datasourceId": "d1", "datasourceType": "postgresql", "query": "SELECT 1"}]


def test_find_duplicate_form_uuid():
    body = {
        "userId": "u1", "isAdmin": False,
        "formQueries": [
            {"formUuid": "f1", "tableInfo": {"databaseTableName": "t1", "queries": _ONE_QUERY}},
            {"formUuid": "f1", "tableInfo": {"databaseTableName": "t2", "queries": _ONE_QUERY}},
        ],
    }
    data = AuthorizationSyncData.model_validate(body)
    assert find_duplicate_form_uuid(data.form_queries) == "f1"


def test_find_duplicate_form_uuid_tra_none_khi_khong_trung():
    data = AuthorizationSyncData.model_validate(
        {
            "userId": "u1", "isAdmin": False,
            "formQueries": [
                {"formUuid": "f1", "tableInfo": {"databaseTableName": "t1", "queries": _ONE_QUERY}},
                {"formUuid": "f2", "tableInfo": {"databaseTableName": "t2", "queries": _ONE_QUERY}},
            ],
        }
    )
    assert find_duplicate_form_uuid(data.form_queries) is None


def test_table_info_queries_rong_bi_loai():
    with pytest.raises(ValidationError):
        AuthorizationSyncData.model_validate(
            {
                "userId": "u1", "isAdmin": False,
                "formQueries": [{"formUuid": "f1", "tableInfo": {"databaseTableName": "t", "queries": []}}],
            }
        )


def test_query_rong_duoc_chap_nhan():
    """SW dùng `query: ""` để biểu diễn "không giới hạn dữ liệu trên datasource đó" — hợp lệ,
    không phải lỗi dữ liệu."""
    data = AuthorizationSyncData.model_validate(
        {
            "userId": "u1", "isAdmin": False,
            "formQueries": [{
                "formUuid": "f1",
                "tableInfo": {
                    "databaseTableName": "t",
                    "queries": [{"datasourceId": "d1", "datasourceType": "postgresql", "query": ""}],
                },
            }],
        }
    )
    assert data.form_queries[0].table_info.queries[0].query == ""


def test_table_info_thieu_queries_bi_loai():
    with pytest.raises(ValidationError):
        AuthorizationSyncData.model_validate(
            {
                "userId": "u1", "isAdmin": False,
                "formQueries": [{"formUuid": "f1", "tableInfo": {"databaseTableName": "t"}}],
            }
        )


def test_normalize_queries_tra_dung_3_khoa_camel_case():
    data = AuthorizationSyncData.model_validate(
        {
            "userId": "u1", "isAdmin": False,
            "formQueries": [{"formUuid": "f1", "tableInfo": {"databaseTableName": "t", "queries": _ONE_QUERY}}],
        }
    )
    assert normalize_queries(data.form_queries[0].table_info.queries) == _ONE_QUERY


def test_find_duplicate_datasource_id():
    data = AuthorizationSyncData.model_validate(
        {
            "userId": "u1", "isAdmin": False,
            "formQueries": [{
                "formUuid": "f1",
                "tableInfo": {
                    "databaseTableName": "t",
                    "queries": [
                        {"datasourceId": "d1", "datasourceType": "postgresql", "query": "SELECT 1"},
                        {"datasourceId": "d1", "datasourceType": "clickhouse", "query": "SELECT 2"},
                    ],
                },
            }],
        }
    )
    assert find_duplicate_datasource_id(data.form_queries[0].table_info.queries) == "d1"


def test_find_duplicate_datasource_id_tra_none_khi_khong_trung():
    data = AuthorizationSyncData.model_validate(
        {
            "userId": "u1", "isAdmin": False,
            "formQueries": [{
                "formUuid": "f1",
                "tableInfo": {
                    "databaseTableName": "t",
                    "queries": [
                        {"datasourceId": "d1", "datasourceType": "postgresql", "query": "SELECT 1"},
                        {"datasourceId": "d2", "datasourceType": "clickhouse", "query": "SELECT 2"},
                    ],
                },
            }],
        }
    )
    assert find_duplicate_datasource_id(data.form_queries[0].table_info.queries) is None


def test_response_serialize_dung_ten_camel_case():
    body = SyncHookResponse(actionType=1, status="SUCCESS").model_dump()
    assert body["actionType"] == 1
    assert body["status"] == "SUCCESS"
    assert body["applied"] == {"upserted": 0, "deleted": 0}
    assert body["results"] == []
    assert body["errorCode"] is None


BATCH_DATA = {"users": [VALID_DATA, {"userId": "u2", "isAdmin": True, "formQueries": []}]}


def test_batch_hop_le_parse_dung_2_user():
    batch = AuthorizationSyncBatch.model_validate(BATCH_DATA)
    assert len(batch.users) == 2
    assert batch.users[0].user_id == "usr-12345-67890"
    assert batch.users[1].user_id == "u2"
    assert batch.users[1].is_admin is True


def test_batch_users_rong_van_parse_duoc_schema():
    # Schema không tự chặn rỗng — EMPTY_USER_LIST là lỗi tường minh ở handler,
    # không lẫn với lỗi schema chung.
    batch = AuthorizationSyncBatch.model_validate({"users": []})
    assert batch.users == []


def test_batch_thieu_truong_users_bi_loai():
    with pytest.raises(ValidationError):
        AuthorizationSyncBatch.model_validate({})


def test_find_duplicate_user_id():
    batch = AuthorizationSyncBatch.model_validate(
        {
            "users": [
                {"userId": "u1", "isAdmin": False, "formQueries": []},
                {"userId": "u1", "isAdmin": False, "formQueries": []},
            ]
        }
    )
    assert find_duplicate_user_id(batch.users) == "u1"


def test_find_duplicate_user_id_tra_none_khi_khong_trung():
    batch = AuthorizationSyncBatch.model_validate(
        {
            "users": [
                {"userId": "u1", "isAdmin": False, "formQueries": []},
                {"userId": "u2", "isAdmin": False, "formQueries": []},
            ]
        }
    )
    assert find_duplicate_user_id(batch.users) is None


def test_user_sync_result_serialize_dung_ten():
    result = UserSyncResult(userId="u1", status="SUCCESS")
    assert result.model_dump() == {
        "userId": "u1", "status": "SUCCESS", "applied": {"upserted": 0, "deleted": 0}
    }
