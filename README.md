# H04 SQLBot

SQLBot là dịch vụ hỏi đáp dữ liệu bằng ngôn ngữ tự nhiên (Text-to-SQL) dựa trên mô hình ngôn ngữ lớn và RAG, fork từ dự án mã nguồn mở [dataease/SQLBot](https://github.com/dataease/SQLBot). Trong H04, SQLBot là backend phục vụ REST/SSE cho các hệ thống tích hợp (ví dụ `h04-ai-gateway`); repo này chỉ giữ phần backend, không có giao diện web.

## Cấu trúc

```
main.py, apps/, common/     mã nguồn backend (FastAPI)
alembic/, alembic.ini       migration database
locales/, templates/        bản dịch và mẫu
scripts/                    script đo và demo (không vào image)
pyproject.toml, uv.lock     dependency (uv)
docker/                     backend-entrypoint.sh: chờ Postgres và Redis sẵn sàng rồi mới chạy uvicorn
Dockerfile                  build image backend (Wolfi, Python 3.11, user appuser uid/gid 10001)
.dockerignore               chỉ để vào context build phần mã chạy và docker/backend-entrypoint.sh
docs/                       tài liệu kiến trúc, vận hành, pipeline Text-to-SQL, đặc tả API
tests/                      toàn bộ test (chạy từ gốc repo: `pytest tests/`)
.env.example                mẫu biến môi trường
```

Các thành phần của upstream không dùng trong H04 đã bị xóa: `frontend/`, `g2-ssr/`, `installer/`, `.github/`, `Dockerfile-base`, `docker-compose.*` (kể cả bản all-in-one), `start.sh`. Khi lấy thay đổi mới từ upstream, các đường dẫn này sẽ hiện lại dưới dạng conflict và nên bỏ qua.

## Build image

```bash
docker build -t sqlbot-backend:test .
```

Build cần ra mạng tới `mirrors.aliyun.com`, `test.pypi.org` và `download.pytorch.org` vì `uv.lock` ghim URL tuyệt đối của các host này. Lệnh `uv sync` dùng `--extra cpu` (bắt buộc, vì code import torch ngay lúc khởi động).

## Chạy local

```bash
cp .env.example .env     # điền DB_HOST, DB_PASSWORD, SECRET_KEY, EMBEDDING_API_BASE_URL
docker run --rm -p 8001:8000 --env-file .env sqlbot-backend:test
```

PostgreSQL phải có sẵn bên ngoài và có extension pgvector; Redis chỉ cần khi `CACHE_TYPE=redis`. Backend nghe cổng 8000 trong container. Kiểm tra sống bằng `GET /api/v1/system/config/key`: đường này không cần JWT và có truy vấn database nên cũng xác nhận kết nối Postgres.

## Biến môi trường chính

| Biến | Ý nghĩa |
|------|---------|
| `DB_HOST`, `DB_PORT`, `DB_USERNAME`, `DB_PASSWORD`, `DB_NAME` | Kết nối Postgres (cần extension pgvector) |
| `SECRET_KEY` | Khóa ký JWT, sinh bằng `openssl rand -base64 32` |
| `EMBEDDING_PROVIDER`, `EMBEDDING_API_BASE_URL`, `EMBEDDING_API_MODEL`, `EMBEDDING_API_KEY` | Embedding qua server riêng (`EMBEDDING_PROVIDER=api`) |
| `CACHE_TYPE` | Cache: `memory`, `redis` hoặc `None`; dùng `redis` khi chạy nhiều pod |
| `REDIS_CONNECTION_STRING`, `REDIS_MODE`, `REDIS_SENTINEL_MASTER`, `REDIS_PASSWORD`, `REDIS_DATABASE_ID` | Redis `standalone` (mặc định), `sentinel` hoặc `cluster`; xem `docs/OPERATIONS.md` §3.4 |
| `DEFAULT_PWD` | Mật khẩu mặc định của người dùng mới (mặc định `SQLBot@123456`); ai-gateway đăng nhập tài khoản workspace bằng đúng giá trị này |
| `AI_CALLBACK_URL`, `AI_CALLBACK_ACTION_TYPE` | Callback khi nạp Excel bất đồng bộ xong |
| `CORS_ALLOW_ALL_ORIGINS`, `BACKEND_CORS_ORIGINS`, `FILE_DOWNLOAD_ALLOWED_HOSTS` | CORS và danh sách host được phép tải file |

Danh sách đầy đủ cùng giá trị mặc định nằm trong `.env.example` và `docs/OPERATIONS.md` (mục 2).

## Triển khai

Triển khai lên Kubernetes qua Argo CD, cấu hình ở repo gitops `devops/cicd/h04`: sản phẩm `h04-sqlbot`, namespace `h04-ai-platform-test` (nhánh `test`) và `h04-ai-platform` (nhánh `staging`). Image mà gitops mong đợi là `dockerhub.elcomlab.com/h04/h04-sqlbot`.

`.gitlab-ci.yml` build image bằng `Dockerfile` ở gốc repo, đẩy lên Harbor rồi ghi tag vào `products/h04-sqlbot/<môi trường>.yaml` của repo gitops (nhánh `test` và `staging`).

## Tài liệu

- `docs/AI_ONBOARDING.md`: hệ thống này là gì và khác upstream ở đâu.
- `docs/BACKEND_ARCHITECTURE.md`: kiến trúc backend.
- `docs/TEXT2SQL_PIPELINE.md`: pipeline Text-to-SQL.
- `docs/OPERATIONS.md`: chạy, cấu hình, triển khai, đo đạc, gỡ lỗi.
- `docs/api_spec/`: đặc tả API.
- `docs/FLATTEN_BACKEND.md`: đợt chuyển code từ `backend/` ra gốc repo và danh sách thành phần đã xóa.
