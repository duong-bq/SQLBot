# Copy of the shared telemetry module (version 0.2.0), kept in the otel-integration skill (templates/python).
# Do not edit here: change the shared files and run templates/python/sync.py for every service.
"""Tên service.

Mọi cấu hình khác (endpoint, exporter, thuộc tính resource) do chart elcom-service sinh thành biến OTEL_* chuẩn
khi `otel.enabled`, nên SDK đọc thẳng từ môi trường và thư viện này không giữ bản sao nào của chúng.
"""

import os

SERVICE_NAME_FALLBACK = "service"


def service_name(fallback: str = SERVICE_NAME_FALLBACK) -> str:
    # OTEL_SERVICE_NAME do chart đặt; chạy ngoài cluster (local, test) thì dùng tên dự phòng.
    return os.environ.get("OTEL_SERVICE_NAME") or fallback
