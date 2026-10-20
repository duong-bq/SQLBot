# syntax=docker/dockerfile:1

# Build cần ra mạng tới mirrors.aliyun.com và test.pypi.org: uv.lock ghim URL tuyệt đối của hai host này.
FROM dockerhub.elcomlab.com/wolfi/python:v3.11.17@sha256:029ed0b6837fb507feaf505b586708d7ba6c2c0857440b015fffeebbae0b6e57

ENV UV_PROJECT_ENVIRONMENT=/home/appuser/venv \
    UV_HTTP_TIMEOUT=600 \
    UV_CONCURRENT_DOWNLOADS=4 \
    PYTHONPATH=/home/appuser/app

RUN --mount=type=bind,source=pyproject.toml,target=/tmp/project/pyproject.toml \
    --mount=type=bind,source=uv.lock,target=/tmp/project/uv.lock \
    --mount=type=cache,target=/home/appuser/.cache/uv,uid=10001,gid=10001 \
    cd /tmp/project \
    && uv sync --frozen --no-install-project --no-dev --python /usr/bin/python3

# --chown dùng số: với --link, tên user được phân giải khi chưa có các lớp RUN phía trên nên BuildKit docker-container báo "invalid user index -1".
COPY --link --chown=10001:10001 . ./

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
