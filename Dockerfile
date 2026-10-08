# syntax=docker/dockerfile:1

# Build cần ra mạng tới mirrors.aliyun.com, test.pypi.org và download.pytorch.org: uv.lock ghim URL tuyệt đối của ba host này.
FROM dockerhub.elcomlab.com/wolfi/python:v3.11.17@sha256:fdf85f09b2ccf0d5cb8d68549ddbf615adf0129019f69546bcbc9e8ee73a523c AS builder

# Cài package cần root; toolchain chỉ ở stage này để không lọt vào image chạy.
USER root
RUN apk add --no-cache build-base pkgconf cyrus-sasl-dev
USER 10001:10001

# Timeout mặc định 30s không đủ tải wheel torch CPU (~900MB); hạ số tải song song để các wheel to không giành băng thông.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/home/appuser/venv \
    UV_HTTP_TIMEOUT=600 \
    UV_CONCURRENT_DOWNLOADS=4

# torch được ghim về index CPU trong pyproject.toml (tool.uv.sources), uv sync trần đã lấy bản CPU.
RUN --mount=type=bind,source=pyproject.toml,target=/tmp/project/pyproject.toml \
    --mount=type=bind,source=uv.lock,target=/tmp/project/uv.lock \
    --mount=type=cache,target=/home/appuser/.cache/uv,uid=10001,gid=10001 \
    cd /tmp/project \
    && uv sync --frozen --no-install-project --no-dev --python /usr/bin/python3

FROM dockerhub.elcomlab.com/wolfi/python:v3.11.17@sha256:fdf85f09b2ccf0d5cb8d68549ddbf615adf0129019f69546bcbc9e8ee73a523c

# libgomp: torch link tới OpenMP. sqlbot_xpack mkdir UPLOAD_DIR lúc import nên các thư mục /opt/sqlbot phải thuộc appuser (không chown -R, venv ~2GB).
USER root
RUN apk add --no-cache libgomp tzdata \
    && mkdir -p /opt/sqlbot/data/file /opt/sqlbot/data/excel /opt/sqlbot/images /opt/sqlbot/models \
    && chown 10001:10001 /opt/sqlbot /opt/sqlbot/data /opt/sqlbot/data/file /opt/sqlbot/data/excel \
                         /opt/sqlbot/images /opt/sqlbot/models
USER 10001:10001

ENV PYTHONUNBUFFERED=1 \
    SQLBOT_HOME=/opt/sqlbot \
    PYTHONPATH=/home/appuser/app \
    PATH="/home/appuser/venv/bin:${PATH}" \
    TZ=Asia/Ho_Chi_Minh \
    SQLBOT_PORT=8000

# --chown dùng số: với --link, tên user được phân giải khi chưa có các lớp RUN phía trên nên BuildKit docker-container báo "invalid user index -1".
COPY --from=builder --chown=10001:10001 /home/appuser/venv /home/appuser/venv
COPY --link --chown=10001:10001 . ./
COPY --link --chown=10001:10001 --chmod=755 docker/backend-entrypoint.sh /home/appuser/backend-entrypoint.sh

EXPOSE 8000

# Không dùng "/": image không build frontend nên "/" luôn 404. Đường này không cần JWT và có truy vấn DB nên xác nhận luôn kết nối Postgres.
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('SQLBOT_PORT','8000')+os.environ.get('CONTEXT_PATH','')+'/api/v1/system/config/key',timeout=5)" || exit 1

ENTRYPOINT ["/home/appuser/backend-entrypoint.sh"]
