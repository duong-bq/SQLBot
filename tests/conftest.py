"""Fixture dùng chung cho test: đưa thư mục gốc repo vào sys.path và gán marker theo thư mục. Fixture Postgres nằm ở tests/integration/conftest.py."""

import os
import pathlib
import sys
import tempfile
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# `sqlbot_xpack` tạo thư mục UPLOAD_DIR ngay lúc import; mặc định /opt/sqlbot/... mà user thường không ghi được,
# nên test tự trỏ các đường dẫn ghi file vào thư mục tạm (biến đã đặt sẵn ở ngoài thì giữ nguyên).
_SCRATCH = Path(tempfile.gettempdir()) / "sqlbot-tests"
for _name, _sub in (("BASE_DIR", ""), ("UPLOAD_DIR", "data/file"), ("EXCEL_PATH", "data/excel"),
                    ("MCP_IMAGE_PATH", "images"), ("LOCAL_MODEL_PATH", "models")):
    os.environ.setdefault(_name, str(_SCRATCH / _sub))

# `main.py` import `sqlbot_xpack` độc lập TRƯỚC bất kỳ module `apps.*` nào. Nếu bỏ dòng này, module
# đầu tiên import `apps.system.schemas.permission` (vd `apps.hooks.api.ai_sync`) sẽ kích hoạt vòng
# lặp: permission.py -> apps.datasource.crud.datasource -> sqlbot_xpack -> ... -> apps.system.api.user
# -> quay lại permission.py trong lúc nó CHƯA thực thi xong, khiến `SqlbotPermission`/
# `require_permissions` chưa tồn tại → ImportError. Import trước ở đây để nạp sqlbot_xpack một lần,
# độc lập, y hệt thứ tự main.py.
import sqlbot_xpack  # noqa: E402,F401


def pytest_collection_modifyitems(items):
    for item in items:
        parts = pathlib.PurePosixPath(item.nodeid.split("::")[0]).parts
        if len(parts) > 2 and parts[1] in ("unit", "integration", "e2e"):
            item.add_marker(getattr(pytest.mark, parts[1]))
