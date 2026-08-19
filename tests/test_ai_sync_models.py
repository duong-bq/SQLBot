"""Test khai báo schema của 2 bảng hook: tên bảng, cột, ràng buộc, index.

Đọc trực tiếp metadata của SQLAlchemy nên không cần DB — bắt được sai lệch khai báo ngay, còn
việc DDL chạy thật do test tích hợp (fixture db_session) lo.
"""

from apps.hooks.models.ai_sync_model import AiSyncHookLog, AiUserPermission


def test_ten_bang():
    assert AiSyncHookLog.__tablename__ == "ai_sync_hook_logs"
    assert AiUserPermission.__tablename__ == "ai_user_permissions"


def test_log_co_du_cot():
    cols = AiSyncHookLog.__table__.columns
    for name in (
        "id", "request_id", "idempotency_key", "action_type", "schema_version",
        "source", "user_id", "request_payload", "status", "sync_version",
        "error_code", "error_message", "received_at", "processed_at",
    ):
        assert name in cols, f"thiếu cột {name}"
    assert cols["action_type"].nullable is False
    assert cols["request_payload"].nullable is False
    assert cols["status"].nullable is False


def test_log_co_partial_unique_index_tren_idempotency_key():
    idx = {i.name: i for i in AiSyncHookLog.__table__.indexes}
    target = idx["uq_ai_sync_hook_logs_idempotency"]
    assert target.unique is True
    # partial index: chỉ áp dụng khi idempotency_key IS NOT NULL
    assert target.dialect_options["postgresql"]["where"] is not None


def test_permission_co_du_cot_va_unique_user_form():
    cols = AiUserPermission.__table__.columns
    for name in (
        "id", "user_id", "full_name", "is_admin", "form_uuid", "database_table_name",
        "table_display_name", "domain_code", "domain_uuid",
        "domain_name", "domain_description", "queries",
        "sync_version", "synced_at", "created_at", "updated_at",
    ):
        assert name in cols, f"thiếu cột {name}"
    assert "fields" not in cols, "cột fields đã bị xoá — field-level metadata chuyển sang endpoint riêng"
    assert "table_description" not in cols, "cột table_description đã bị xoá — mô tả bảng chuyển sang endpoint riêng"
    assert cols["sync_version"].nullable is False
    assert cols["domain_code"].nullable is True
    assert cols["form_uuid"].nullable is True, "formUuid optional ở SW — cột phải nullable"
    constraint_cols = {
        tuple(sorted(c.name for c in con.columns))
        for con in AiUserPermission.__table__.constraints
        if con.__class__.__name__ == "UniqueConstraint"
    }
    assert ("database_table_name", "user_id") in constraint_cols
    assert ("form_uuid", "user_id") not in constraint_cols


def test_permission_co_index_user_id():
    """`idx_ai_user_permissions_user_table` cũ đã bỏ — trùng với index tự sinh của unique
    constraint mới `uq_ai_user_permissions_user_table` trên `(user_id, database_table_name)`."""
    names = {i.name for i in AiUserPermission.__table__.indexes}
    assert "idx_ai_user_permissions_user_id" in names


def test_permission_co_index_domain_code():
    names = {i.name for i in AiUserPermission.__table__.indexes}
    assert "idx_ai_user_permissions_domain_code" in names
