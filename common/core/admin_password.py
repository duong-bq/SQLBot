"""Đồng bộ mật khẩu admin theo ADMIN_PASSWORD, chạy sau migration.

Hai nơi gọi: job migrate của k8s (`python -m common.core.admin_password` sau `alembic upgrade head`) và lúc khởi động
khi MIGRATE_ON_STARTUP bật. Không dùng khoá advisory: hàm chỉ ghi khi mật khẩu đang lưu khác ADMIN_PASSWORD nên chạy lại
hay chạy song song đều an toàn.
"""

from sqlmodel import Session

from apps.system.crud.user import set_admin_password
from common.core.config import settings
from common.core.db import engine
from common.utils.utils import SQLBotLogUtil


def sync_admin_password() -> bool:
    if not settings.ADMIN_PASSWORD:
        return False
    with Session(engine) as session:
        changed = set_admin_password(session=session, password=settings.ADMIN_PASSWORD)
    SQLBotLogUtil.info("Admin password updated from ADMIN_PASSWORD" if changed else "Admin password already matches ADMIN_PASSWORD")
    return changed


if __name__ == "__main__":
    print("admin password updated" if sync_admin_password() else "admin password unchanged")
