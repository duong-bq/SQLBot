# Chuyển code từ `backend/` ra gốc repo và dọn thành phần không dùng

Tài liệu này ghi lại đợt dọn repo: toàn bộ code backend được đưa từ `backend/` ra thư mục gốc, các test được gộp về một chỗ, và những thành phần của upstream mà H04 không dùng đã bị xóa. Đọc khi cần lấy thay đổi từ upstream `dataease/SQLBot` hoặc khi tìm một file cũ.

## 1. Vì sao

H04 chỉ chạy backend (REST/SSE) phía sau `h04-ai-gateway`, không có giao diện web. Frontend, SSR biểu đồ, bộ cài offline và các workflow của upstream không còn tác dụng, trong khi `backend/` làm mọi đường dẫn dài thêm một cấp và tách test làm hai nơi. Sau đợt này repo chỉ còn phần chạy được và phần để build image.

## 2. Đã chuyển

Chuyển bằng `git mv` nên lịch sử từng file được giữ (`git log --follow`).

| Cũ | Mới |
|----|-----|
| `backend/apps/`, `backend/common/` | `apps/`, `common/` |
| `backend/alembic/`, `backend/alembic.ini` | `alembic/`, `alembic.ini` |
| `backend/main.py` | `main.py` |
| `backend/locales/`, `backend/templates/` | `locales/`, `templates/` |
| `backend/scripts/` | `scripts/` |
| `backend/pyproject.toml`, `backend/uv.lock` | `pyproject.toml`, `uv.lock` |
| `backend/tests/test_*.py` (10 file) | `tests/` |
| `backend/.gitignore` | gộp vào `.gitignore` gốc |
| `Dockerfile.backend` | `Dockerfile` |
| `Dockerfile.backend.dockerignore` | `.dockerignore` |

Khoảng 339 file đổi đường dẫn. Cấu trúc bên trong từng thư mục không đổi, nên các phép tính đường dẫn theo vị trí file (ví dụ `Path(__file__).parent.parent.parent` trong `apps/template/template.py`) vẫn đúng.

## 3. Đã sửa vì đường dẫn đổi

- `common/core/config.py`: `env_file="../.env"` thành `".env"`. Trước đó `.env` nằm ở thư mục cha của `backend/`; giờ nằm ở thư mục làm việc, tức gốc repo. Chạy backend từ thư mục khác thì `.env` bị bỏ qua im lặng.
- `tests/conftest.py` đưa gốc repo vào `sys.path`; ba test và hai chỗ trong test cứng chuỗi `backend` đã sửa.
- `Dockerfile` đọc `pyproject.toml` và `uv.lock` ở gốc; `.dockerignore` chuyển thành danh sách cho phép từng thư mục (`alembic`, `apps`, `common`, `locales`, `templates`, `main.py`, `pyproject.toml`, `uv.lock`). `scripts/` và `tests/` không vào image như trước.
- README, `.env.example`, `CLAUDE.md`, toàn bộ `docs/` và vài comment trong code không còn tham chiếu `backend/`.

## 4. Test sau khi gộp

Chỉ còn một thư mục `tests/` và một `conftest.py` (hợp từ hai conftest cũ: nạp `sqlbot_xpack` trước, đưa gốc repo vào `sys.path`, fixture `pg_url` và `db_session`). Chạy từ gốc repo:

```bash
pytest tests/
```

Kết quả khi chạy trong image Wolfi: 342 passed, 45 skipped, 0 failed. 45 test bị skip vì thiếu `SQLBOT_TEST_DB_URL` (cần Postgres có pgvector rỗng; xem `docs/OPERATIONS.md` §7.12).

Đã xóa 4 lớp test trong `tests/test_supplier_config.py` vì chúng đọc file của `frontend/` và `docs/README.en.md` (đều đã xóa): `TestMiniMaxSupplierConfig`, `TestMiniMaxI18nTranslations`, `TestMiniMaxIconFile`, `TestReadmeContent`. Lớp `TestModelFactoryConfig` (5 test về model factory của backend) được giữ lại.

## 5. Đã xóa

| Đường dẫn | Số file | Lý do |
|-----------|---------|-------|
| `frontend/` | 627 | Giao diện web của upstream; H04 chỉ phục vụ REST/SSE |
| `g2-ssr/` | 10 | Dịch vụ SSR sinh biểu đồ cho frontend, đã vô dụng |
| `installer/` | 6 | Bộ cài offline của upstream |
| `.github/` | 9 | Workflow build, đồng bộ Gitee/CNB, kiểm tra typo của upstream |
| `.idea/` | 1 | Cấu hình IDE |
| `Dockerfile-base` | 1 | Base có nhúng Postgres 17 cho bản all-in-one |
| `docker-compose.yaml` | 1 | Bản all-in-one của upstream |
| `docker-compose.backend.yml` | 1 | Backend + Postgres + Redis cho máy test; thay bằng Kubernetes qua Argo CD |
| `docker-compose.service.yml` | 1 | Postgres + Redis cho dev; Postgres là service bên ngoài, Redis chạy bằng `docker run` |
| `start.sh` | 1 | Khởi động all-in-one (Postgres + pm2 + uvicorn) |
| `sqlbot-assistant-demo.html` | 1 | Trang demo nhúng trợ lý |
| `.typos.toml` | 1 | Chỉ phục vụ workflow typos đã xóa |
| `.mcp.json` | 1 | Cấu hình MCP của nhà phát triển, xem cảnh báo bên dưới |
| `docs/README.en.md` | 1 | README tiếng Anh của upstream; README gốc đã viết lại bằng tiếng Việt |
| `backend/README.md` | 1 | Chỉ có một dòng "FastAPI Project - Backend" |
| `.dockerignore` cũ | 1 | Dành cho bản all-in-one cần `frontend/` và `g2-ssr/` trong context |

Tổng cộng khoảng 665 file bị xóa. Postgres không còn được dựng cùng sqlbot: dùng PostgreSQL bên ngoài có extension pgvector.

⚠ `.mcp.json` chứa chuỗi kết nối Postgres kèm mật khẩu ở dạng rõ và đã nằm trong lịch sử git. Xóa file không gỡ nó khỏi lịch sử; cần đổi mật khẩu đó.

## 6. Thay đổi đi kèm

- `Dockerfile` dựng trên image Python tự dựng (Wolfi, Python 3.11, user `appuser` uid 10001) thay vì `python:3.11-slim`, gồm hai stage: builder cài toolchain và `uv sync --frozen --extra cpu`, runtime chỉ copy venv. Port 8000.
- `.gitlab-ci.yml` viết lại theo mẫu của `h04-ai-gateway`: build, push lên Harbor `dockerhub.elcomlab.com/h04/h04-sqlbot` (tag `test-x.y.z` hoặc `stg-x.y.z`), rồi ghi tag vào repo gitops. Pipeline cũ (build, push và deploy SSH bằng compose lên máy test) bị thay hoàn toàn.
- `README.md` viết lại bằng tiếng Việt.

## 7. Lưu ý khi lấy thay đổi từ upstream

- Mọi đường dẫn `backend/...` của upstream giờ là `...` ở gốc repo. Merge hoặc cherry-pick sẽ báo conflict hoặc "file deleted/renamed" ở các đường dẫn này. Git thường phát hiện đổi tên nếu nội dung file giống, nên nên thử `git merge -X find-renames=40%` trước khi giải tay.
- Thay đổi của upstream ở `frontend/`, `g2-ssr/`, `installer/`, `.github/` và các file all-in-one nên bỏ qua (giữ phía đã xóa).
- Thay đổi `backend/pyproject.toml` hoặc `backend/uv.lock` của upstream phải áp vào `pyproject.toml` và `uv.lock` ở gốc, rồi chạy lại `uv lock` nếu cần.

## 8. Khôi phục

Mọi thứ nằm trong lịch sử git. Khôi phục một đường dẫn đã xóa từ commit trước đợt này:

```bash
git checkout <commit-trước-đợt-dọn> -- frontend/
```

Xem một file cũ trước khi chuyển: `git show <commit-trước-đợt-dọn>:backend/apps/chat/task/llm.py`.
