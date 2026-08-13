"""Model 2 bảng của AI Sync Hook.

Khác quy ước snowflake BigInt của repo, hai bảng này dùng khoá chính UUID sinh phía DB
(`gen_random_uuid()`, có sẵn từ Postgres 13 nên không cần extension): chúng do hệ ngoài ghi vào và
không join vào cụm id snowflake của SQLBot.

Index được khai ngay trong `__table_args__` (không chỉ trong migration) để test tích hợp tạo bảng
bằng metadata vẫn có đủ ràng buộc thật — đặc biệt là partial unique index chống trùng idempotency.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlmodel import Field, SQLModel


class AiSyncHookLog(SQLModel, table=True):
    """Audit toàn bộ request SW gửi vào hook: raw payload + kết quả xử lý.

    `sync_version` là cột thêm ngoài DDL gốc của bên tích hợp và bắt buộc phải có: sau một lần thu
    hồi hết quyền (`formQueries: []`), `ai_user_permissions` không còn dòng nào của user nên mốc
    version đã áp dụng chỉ còn đọc được từ bảng log này.
    """

    __tablename__ = "ai_sync_hook_logs"
    __table_args__ = (
        Index("idx_ai_sync_hook_logs_user_id", "user_id"),
        Index("idx_ai_sync_hook_logs_action_type", "action_type"),
        Index("idx_ai_sync_hook_logs_received_at", "received_at"),
        Index(
            "uq_ai_sync_hook_logs_idempotency",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    )
    request_id: str | None = Field(default=None, sa_column=Column(String(100), nullable=True))
    idempotency_key: str | None = Field(default=None, sa_column=Column(String(100), nullable=True))
    action_type: int = Field(sa_column=Column(Integer, nullable=False))
    schema_version: str | None = Field(default=None, sa_column=Column(String(20), nullable=True))
    source: str = Field(default="SW", sa_column=Column(String(50), nullable=False, server_default=text("'SW'")))
    user_id: str | None = Field(default=None, sa_column=Column(String(100), nullable=True))
    request_payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))
    status: str = Field(sa_column=Column(String(30), nullable=False))
    sync_version: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    error_code: str | None = Field(default=None, sa_column=Column(String(100), nullable=True))
    error_message: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    received_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now()),
    )
    processed_at: datetime | None = Field(
        default=None, sa_column=Column(TIMESTAMP(timezone=True), nullable=True)
    )


class AiUserPermission(SQLModel, table=True):
    """Trạng thái quyền HIỆN TẠI của user sau khi parse bản tin AUTHORIZATION_SYNC.

    Một dòng = một user + một form + một bảng nghiệp vụ + danh sách field + danh sách query theo
    từng datasource. `queries` lưu nguyên mảng `{datasourceId, datasourceType, query}`, phase này
    không parse và không chạy — giữ khoá camelCase để output API đọc thẳng không cần remap.

    `domain_*` là metadata lĩnh vực (linh vực nghiệp vụ) của bảng do SW gửi kèm `tableInfo`, chỉ để
    đọc lại khi truy vấn quyền — không dùng để lọc/join gì trong phase này nên không chuẩn hoá
    thành bảng riêng.
    """

    __tablename__ = "ai_user_permissions"
    __table_args__ = (
        UniqueConstraint("user_id", "form_uuid", name="uq_ai_user_permissions_user_form"),
        Index("idx_ai_user_permissions_user_id", "user_id"),
        Index("idx_ai_user_permissions_user_table", "user_id", "database_table_name"),
        Index("idx_ai_user_permissions_domain_code", "domain_code"),
    )

    id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    )
    user_id: str = Field(sa_column=Column(String(100), nullable=False))
    full_name: str | None = Field(default=None, sa_column=Column(String(255), nullable=True))
    is_admin: bool = Field(
        default=False, sa_column=Column(Boolean, nullable=False, server_default=text("false"))
    )
    form_uuid: str = Field(sa_column=Column(String(100), nullable=False))
    database_table_name: str = Field(sa_column=Column(String(255), nullable=False))
    table_display_name: str | None = Field(default=None, sa_column=Column(String(255), nullable=True))
    table_description: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    domain_code: str | None = Field(default=None, sa_column=Column(String(100), nullable=True))
    domain_uuid: str | None = Field(default=None, sa_column=Column(String(100), nullable=True))
    domain_name: str | None = Field(default=None, sa_column=Column(String(255), nullable=True))
    domain_description: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    fields: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    )
    queries: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    )
    sync_version: int = Field(sa_column=Column(BigInteger, nullable=False))
    synced_at: datetime | None = Field(
        default=None, sa_column=Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    )
    created_at: datetime | None = Field(
        default=None, sa_column=Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    )
    updated_at: datetime | None = Field(
        default=None, sa_column=Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    )
