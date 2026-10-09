#!/bin/sh
# Entrypoint của container backend.
#
# Lý do cần script này thay vì gọi uvicorn trực tiếp: lifespan của FastAPI chạy
# run_migrations() (alembic upgrade head) ngay khi start — Postgres chưa nhận kết nối thì
# process chết luôn thay vì chờ. Với `depends_on: condition: service_healthy` trong compose
# thì đã đủ, nhưng khi DB là service ở host/máy khác thì không có healthcheck nào bảo đảm,
# nên chờ ở đây.
set -e

python - <<'PY'
import os
import socket
import sys
import time

# Redis chỉ chờ khi thực sự dùng; CACHE_TYPE mặc định của app là redis nên biến trống cũng tính là redis.
# Mỗi mục là (tên, danh sách địa chỉ); chỉ cần MỘT địa chỉ trong danh sách trả lời là đủ, vì Sentinel
# và cluster có nhiều node mà không node nào bắt buộc phải sống riêng lẻ.
targets = [("Postgres", [(os.environ.get("DB_HOST", "localhost"),
                          int(os.environ.get("DB_PORT", "5432")))])]

if os.environ.get("CACHE_TYPE", "redis").lower() == "redis":
    nodes = []
    for item in os.environ.get("REDIS_CONNECTION_STRING", "localhost:6379").split(","):
        host, _, port = item.strip().rpartition(":")
        if host:
            nodes.append((host, int(port)))
    if nodes:
        targets.append(("Redis", nodes))

timeout = int(os.environ.get("SQLBOT_WAIT_TIMEOUT", "120"))

for name, candidates in targets:
    deadline = time.time() + timeout
    ready = None
    while ready is None:
        for host, port in candidates:
            try:
                with socket.create_connection((host, port), timeout=3):
                    ready = (host, port)
                    break
            except OSError as exc:
                last_error = exc
        if ready is None:
            if time.time() >= deadline:
                addrs = ",".join(f"{h}:{p}" for h, p in candidates)
                print(f"[entrypoint] LOI: {name} {addrs} khong phan hoi sau "
                      f"{timeout}s ({last_error})", file=sys.stderr, flush=True)
                sys.exit(1)
            time.sleep(2)
    print(f"[entrypoint] {name} {ready[0]}:{ready[1]} san sang", flush=True)
PY

# --proxy-headers + --forwarded-allow-ips: để sau reverse proxy vẫn lấy đúng IP client và
# scheme https (audit log và SERVER_IMAGE_HOST dựa vào đó).
exec uvicorn main:app \
    --host 0.0.0.0 \
    --port "${SQLBOT_PORT:-8000}" \
    --workers "${SQLBOT_WORKERS:-1}" \
    --proxy-headers \
    --forwarded-allow-ips='*'
