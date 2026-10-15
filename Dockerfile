# syntax=docker/dockerfile:1

# Build cần ra mạng tới mirrors.aliyun.com, test.pypi.org và download.pytorch.org: uv.lock ghim URL tuyệt đối của ba host này.
FROM dockerhub.elcomlab.com/wolfi/python:v3.11.17@sha256:029ed0b6837fb507feaf505b586708d7ba6c2c0857440b015fffeebbae0b6e57

# libgomp: torch link tới OpenMP. File tạm (UPLOAD_DIR, EXCEL_PATH, MCP_IMAGE_PATH) mặc định nằm dưới /tmp/sqlbot và do ứng dụng tự tạo.
USER root
RUN apk add --no-cache libgomp
USER 10001:10001

# Timeout mặc định 30s không đủ tải wheel torch CPU (~900MB); hạ số tải song song để các wheel to không giành băng thông.
ENV UV_PROJECT_ENVIRONMENT=/home/appuser/venv \
    UV_HTTP_TIMEOUT=600 \
    UV_CONCURRENT_DOWNLOADS=4 \
    PYTHONPATH=/home/appuser/app

# torch được ghim về index CPU trong pyproject.toml (tool.uv.sources), uv sync trần đã lấy bản CPU.
RUN --mount=type=bind,source=pyproject.toml,target=/tmp/project/pyproject.toml \
    --mount=type=bind,source=uv.lock,target=/tmp/project/uv.lock \
    --mount=type=cache,target=/home/appuser/.cache/uv,uid=10001,gid=10001 \
    cd /tmp/project \
    && uv sync --frozen --no-install-project --no-dev --python /usr/bin/python3

# --chown dùng số: với --link, tên user được phân giải khi chưa có các lớp RUN phía trên nên BuildKit docker-container báo "invalid user index -1".
COPY --link --chown=10001:10001 . ./

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
