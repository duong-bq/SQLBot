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
        "table_display_name", "table_description", "fields", "postgres_query",
        "clickhouse_query", "sync_version", "synced_at", "created_at", "updated_at",
    ):
        assert name in cols, f"thiếu cột {name}"
    assert cols["sync_version"].nullable is False
    constraint_cols = {
        tuple(sorted(c.name for c in con.columns))
        for con in AiUserPermission.__table__.constraints
        if con.__class__.__name__ == "UniqueConstraint"
    }
    assert ("form_uuid", "user_id") in constraint_cols


def test_permission_co_index_user_id_va_user_table():
    names = {i.name for i in AiUserPermission.__table__.indexes}
    assert "idx_ai_user_permissions_user_id" in names
    assert "idx_ai_user_permissions_user_table" in names
