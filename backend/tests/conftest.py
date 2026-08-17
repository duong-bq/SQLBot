"""Cấu hình chung cho test: đưa thư mục backend vào sys.path để import được ``apps.*``.

Repo chưa có hạ tầng test nào khác — pytest chỉ tự thêm thư mục tests/ vào path, nên thiếu dòng
này thì mọi import tuyệt đối trong test đều hỏng.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
