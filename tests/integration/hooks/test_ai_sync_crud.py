"""Test crud của hook trên Postgres thật (skip nếu chưa đặt SQLBOT_TEST_DB_URL).

Dùng DB thật vì code phụ thuộc JSONB, partial unique index và ON CONFLICT — SQLite không có.
"""

import pytest
from sqlalchemy.exc import IntegrityError

from apps.hooks.constants import SyncActionType, SyncStatus
from apps.hooks.crud.ai_sync_log import (
    create_received_log,
    finish_log,
    get_log_by_idempotency_key,
)
from apps.hooks.crud.ai_user_permission import get_user_permissions, replace_user_permissions
from apps.hooks.schemas.ai_sync_schema import FormQuery

PAYLOAD = {"actionType": 1, "payload": {"userId": "u1"}}


def _new_log(session, key="key-1", user_id="u1"):
    return create_received_log(
        session,
        request_id="req-1",
        idempotency_key=key,
        action_type=int(SyncActionType.AUTHORIZATION_SYNC),
        schema_version="1.0",
        user_id=user_id,
        request_payload=PAYLOAD,
    )


def test_create_received_log_ghi_dung_trang_thai_va_payload(db_session):
    log = _new_log(db_session)
    assert log.id is not None
    assert log.status == SyncStatus.RECEIVED.value
    assert log.source == "SW"
    assert log.request_payload == PAYLOAD
    assert log.received_at is not None
    assert log.processed_at is None
    assert log.sync_version is None


def test_get_log_by_idempotency_key(db_session):
    created = _new_log(db_session, key="key-abc")
    found = get_log_by_idempotency_key(db_session, "key-abc")
    assert found is not None and found.id == created.id
    assert get_log_by_idempotency_key(db_session, "khong-ton-tai") is None


def test_trung_idempotency_key_bi_unique_index_chan(db_session):
    _new_log(db_session, key="key-dup")
    with pytest.raises(IntegrityError):
        _new_log(db_session, key="key-dup")
    db_session.rollback()


def test_finish_log_cap_nhat_status_va_processed_at(db_session):
    log = _new_log(db_session, key="key-finish")
    finish_log(db_session, log, status=SyncStatus.SUCCESS.value, sync_version=1786356000000)
    assert log.status == SyncStatus.SUCCESS.value
    assert log.sync_version == 1786356000000
    assert log.processed_at is not None
    assert log.error_code is None


def test_finish_log_ghi_loi(db_session):
    log = _new_log(db_session, key="key-fail")
    finish_log(
        db_session, log, status=SyncStatus.FAILED.value,
        error_code="INVALID_PAYLOAD", error_message="userId rỗng",
    )
    assert log.status == SyncStatus.FAILED.value
    assert log.error_code == "INVALID_PAYLOAD"
    assert log.error_message == "userId rỗng"


def _form_query(*, form_uuid="f1", domain_code="LV_DAN_CU", domain_name="Lĩnh vực Dân cư"):
    return FormQuery.model_validate({
        "formUuid": form_uuid,
        "tableInfo": {
            "databaseTableName": "t1",
            "linhVucMa": domain_code,
            "linhVucUuid": "lv-uuid-5678",
            "linhVucName": domain_name,
            "linhVucDescription": "Mô tả lĩnh vực",
            "queries": [{"datasourceId": "d1", "datasourceType": "postgresql", "query": "SELECT 1"}],
        },
    })


def test_replace_user_permissions_ghi_du_domain_fields(db_session):
    replace_user_permissions(
        db_session, user_id="u-domain", full_name="A", is_admin=False,
        form_queries=[_form_query()], sync_version=100,
    )
    db_session.commit()
    rows = get_user_permissions(db_session, "u-domain")
    assert len(rows) == 1
    assert rows[0].domain_code == "LV_DAN_CU"
    assert rows[0].domain_uuid == "lv-uuid-5678"
    assert rows[0].domain_name == "Lĩnh vực Dân cư"
    assert rows[0].domain_description == "Mô tả lĩnh vực"


def test_replace_user_permissions_upsert_cap_nhat_domain_khac(db_session):
    replace_user_permissions(
        db_session, user_id="u-domain-2", full_name="A", is_admin=False,
        form_queries=[_form_query(domain_code="LV_A", domain_name="Lĩnh vực A")], sync_version=100,
    )
    db_session.commit()
    replace_user_permissions(
        db_session, user_id="u-domain-2", full_name="A", is_admin=False,
        form_queries=[_form_query(domain_code="LV_B", domain_name="Lĩnh vực B")], sync_version=200,
    )
    db_session.commit()
    rows = get_user_permissions(db_session, "u-domain-2")
    assert len(rows) == 1
    assert rows[0].domain_code == "LV_B"
    assert rows[0].domain_name == "Lĩnh vực B"


def test_replace_user_permissions_khong_can_form_uuid(db_session):
    """`form_uuid=None` (SW không gửi `formUuid`) vẫn ghi được — khoá là `database_table_name`."""
    form = FormQuery.model_validate({
        "tableInfo": {
            "databaseTableName": "t-no-form-uuid",
            "queries": [{"datasourceId": "d1", "datasourceType": "postgresql", "query": "SELECT 1"}],
        },
    })
    replace_user_permissions(
        db_session, user_id="u-no-form", full_name=None, is_admin=False,
        form_queries=[form], sync_version=100,
    )
    db_session.commit()
    rows = get_user_permissions(db_session, "u-no-form")
    assert len(rows) == 1
    assert rows[0].form_uuid is None
    assert rows[0].database_table_name == "t-no-form-uuid"


def test_replace_user_permissions_upsert_theo_bang_khong_theo_form_uuid(db_session):
    """Gửi lại đúng bảng đó với `formUuid` khác — GHI ĐÈ dòng cũ, không tạo dòng mới."""
    replace_user_permissions(
        db_session, user_id="u-table-key", full_name="A", is_admin=False,
        form_queries=[_form_query(form_uuid="f1")], sync_version=100,
    )
    db_session.commit()
    replace_user_permissions(
        db_session, user_id="u-table-key", full_name="A", is_admin=False,
        form_queries=[_form_query(form_uuid="f2")], sync_version=200,
    )
    db_session.commit()
    rows = get_user_permissions(db_session, "u-table-key")
    assert len(rows) == 1
    assert rows[0].form_uuid == "f2"
    assert rows[0].sync_version == 200
