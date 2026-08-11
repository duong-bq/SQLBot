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

# Redis chỉ chờ khi thực sự dùng (CACHE_TYPE=redis); mặc định của app là cache in-memory.
targets = [("Postgres", os.environ.get("POSTGRES_SERVER", "localhost"),
            int(os.environ.get("POSTGRES_PORT", "5432")))]

if os.environ.get("CACHE_TYPE", "").lower() == "redis":
    url = os.environ.get("CACHE_REDIS_URL", "redis://localhost:6379/0")
    # Parse thủ công cho cả dạng redis:// và rediss://, có hoặc không có user:pass@
    netloc = url.split("://", 1)[-1].split("/", 1)[0]
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
    host, _, port = netloc.partition(":")
    targets.append(("Redis", host or "localhost", int(port or 6379)))

timeout = int(os.environ.get("SQLBOT_WAIT_TIMEOUT", "120"))

for name, host, port in targets:
    deadline = time.time() + timeout
    while True:
        try:
            with socket.create_connection((host, port), timeout=3):
                print(f"[entrypoint] {name} {host}:{port} san sang", flush=True)
                break
        except OSError as exc:
            if time.time() >= deadline:
                print(f"[entrypoint] LOI: {name} {host}:{port} khong phan hoi sau "
                      f"{timeout}s ({exc})", file=sys.stderr, flush=True)
                sys.exit(1)
            time.sleep(2)
PY

# --proxy-headers + --forwarded-allow-ips: để sau reverse proxy vẫn lấy đúng IP client và
# scheme https (audit log và SERVER_IMAGE_HOST dựa vào đó).
exec uvicorn main:app \
    --host 0.0.0.0 \
    --port "${SQLBOT_PORT:-8000}" \
    --workers "${SQLBOT_WORKERS:-1}" \
    --proxy-headers \
    --forwarded-allow-ips='*'
