"""Fixture dùng chung cho test: đưa `backend/` vào sys.path và cấp session Postgres thật.

Repo không có hạ tầng test DB sẵn; các test cần JSONB / partial unique index / ON CONFLICT nên
không thể chạy trên SQLite. Fixture `pg_url` skip (không fail) khi thiếu SQLBOT_TEST_DB_URL để
`pytest tests/` vẫn xanh trên máy không dựng Postgres.
"""

import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# `main.py` import `sqlbot_xpack` độc lập TRƯỚC bất kỳ module `apps.*` nào. Nếu bỏ dòng này, module
# đầu tiên import `apps.system.schemas.permission` (vd `apps.hooks.api.ai_sync`) sẽ kích hoạt vòng
# lặp: permission.py -> apps.datasource.crud.datasource -> sqlbot_xpack -> ... -> apps.system.api.user
# -> quay lại permission.py trong lúc nó CHƯA thực thi xong, khiến `SqlbotPermission`/
# `require_permissions` chưa tồn tại → ImportError. Import trước ở đây để nạp sqlbot_xpack một lần,
# độc lập, y hệt thứ tự main.py.
import sqlbot_xpack  # noqa: E402,F401


@pytest.fixture(scope="session")
def pg_url() -> str:
    """URL Postgres dùng cho test tích hợp; skip cả test nếu chưa cấu hình."""
    url = os.getenv("SQLBOT_TEST_DB_URL")
    if not url:
        pytest.skip("Chưa đặt SQLBOT_TEST_DB_URL nên bỏ qua test cần Postgres thật")
    return url


@pytest.fixture()
def db_session(pg_url: str):
    """Session sạch cho mỗi test: tạo 2 bảng của module hook nếu chưa có rồi TRUNCATE.

    Tạo bảng bằng metadata của SQLModel thay vì chạy alembic để test không phụ thuộc toàn bộ
    lịch sử migration của repo. Vì index được khai ngay trong `__table_args__` nên bản tạo bằng
    metadata có đủ partial unique index như migration.
    """
    from sqlalchemy import create_engine, text
    from sqlmodel import Session

    from apps.hooks.models.ai_sync_model import AiSyncHookLog, AiUserPermission

    engine = create_engine(pg_url)
    AiSyncHookLog.__table__.create(engine, checkfirst=True)
    AiUserPermission.__table__.create(engine, checkfirst=True)
    with Session(engine) as session:
        session.execute(text("TRUNCATE ai_sync_hook_logs, ai_user_permissions"))
        session.commit()
        yield session
    engine.dispose()
