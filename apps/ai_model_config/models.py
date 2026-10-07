"""Bảng ``ai_model_config``: một dòng là một model của một workspace hoặc một datasource.

Dòng ``workspace`` là cấu hình mặc định, Gateway đẩy sang và thay toàn bộ mỗi lần đẩy. Dòng
``datasource`` là bản CHÉP lúc tạo datasource (từ cấu hình tuỳ chỉnh hoặc từ dòng workspace), không
tham chiếu ngược: đổi cấu hình workspace về sau không đụng datasource đã có.

Index khai trong ``__table_args__`` (không chỉ trong migration) để test dựng bảng bằng metadata vẫn
có đủ ràng buộc thật, theo khuôn của ``excel_import_jobs``.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlmodel import Field

from common.core.models import SnowflakeBase

SCOPE_WORKSPACE = "workspace"
SCOPE_DATASOURCE = "datasource"

MODEL_LLM = "llm"
MODEL_EMBEDDING = "embedding"
MODEL_RERANK = "rerank"
MODEL_TYPES = (MODEL_LLM, MODEL_EMBEDDING, MODEL_RERANK)

ORIGIN_CUSTOM = "custom"
ORIGIN_WORKSPACE = "workspace"


class AiModelConfig(SnowflakeBase, table=True):
    """Một model (llm/embedding/rerank) gắn với một workspace hoặc một datasource."""

    __tablename__ = "ai_model_config"
    __table_args__ = (
        UniqueConstraint(
            "scope", "scope_id", "model_type", name="uq_ai_model_config_scope"
        ),
        Index("idx_ai_model_config_oid", "oid"),
    )

    scope: str = Field(sa_column=Column(String(16), nullable=False))
    # ``oid`` khi scope là workspace, ``ds_id`` khi scope là datasource.
    scope_id: int = Field(sa_column=Column(BigInteger, nullable=False))
    # Lặp lại ở dòng datasource: dùng để so host với cấu hình workspace và để dọn dẹp.
    oid: int = Field(sa_column=Column(BigInteger, nullable=False))
    model_type: str = Field(sa_column=Column(String(16), nullable=False))
    # Rerank lưu URL đầy đủ (``.../v2/rerank``), hai loại kia lưu base URL kiểu OpenAI.
    base_url: str = Field(sa_column=Column(Text, nullable=False))
    model: str = Field(sa_column=Column(String(255), nullable=False))
    # Mã hoá bằng ``SECRET_KEY``: đổi khoá này là mọi dòng thành không đọc được.
    api_key_enc: str = Field(sa_column=Column(Text, nullable=False))
    api_key_hint: str = Field(sa_column=Column(String(16), nullable=False))
    # Số chiều vector, chỉ cho embedding. NULL cho tới lần embed thành công đầu tiên.
    dim: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    # Dòng datasource: ``custom`` hoặc ``workspace``. Dòng workspace: NULL.
    origin: str | None = Field(default=None, sa_column=Column(String(16), nullable=True))
    created_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    # Đổi khi nội dung đổi; là một nửa khoá của cache khoá đã giải mã và cache embedder.
    updated_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
