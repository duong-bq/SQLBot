# chat_stream_demo — so sánh streaming giữa 2 endpoint chat

Mục đích: nhìn tận mắt sự khác nhau về **dữ liệu trả về** giữa endpoint chat của UI web và
endpoint chat dành cho MCP client, trên cùng một câu hỏi và cùng một datasource.

> **Cần đặc tả để giao cho đối tác tích hợp?** Xem [`API_SPEC.md`](API_SPEC.md) — tài liệu hoàn
> chỉnh 5 API của luồng chat thường.

Datasource dùng làm demo: **DataLaoCai** (`core_datasource.id = 5`) — thu/chi ngân sách tỉnh Lào Cai.

## Chuẩn bị

Backend phải đang chạy ở `localhost:8001` (kèm Postgres 5434 + Redis 6380 qua
`docker-compose.service.yml`).

## Chạy

```bash
cd backend
.venv/bin/python scripts/chat_stream_demo/01_chat_standard.py
.venv/bin/python scripts/chat_stream_demo/02_chat_mcp.py
```

Đổi câu hỏi / datasource bằng biến môi trường:

```bash
SQLBOT_QUESTION="Top 5 nguồn thu lớn nhất" SQLBOT_DS_ID=5 \
  .venv/bin/python scripts/chat_stream_demo/01_chat_standard.py
```

## Về việc lấy JWT

Cả hai script lấy token qua `POST /mcp/access_token` (nhận mật khẩu plaintext) chứ không qua
`POST /login/access-token` — endpoint login chính chạy username/password qua `sqlbot_decrypt`,
mà hàm giải mã nằm trong package biên dịch sẵn `sqlbot_xpack` nên script không tự mã hóa được.
Hai đường đều gọi chung `create_access_token()` nên **token sinh ra là như nhau**.

## Khác biệt cần quan sát

| | `/chat/question` (thường) | `/mcp/mcp_question` (MCP) |
|---|---|---|
| JWT đi ở đâu | header `X-SQLBOT-TOKEN: Bearer <jwt>` | trong **body** (`token`) |
| Ai xác thực | `TokenMiddleware` (toàn cục) | handler tự gọi `get_user()` — route `/mcp*` nằm trong whitelist |
| Datasource | gắn vào hội thoại lúc `/chat/start` | khai theo **từng câu hỏi** (`datasource_id`) |
| Body | chỉ `chat_id` + `question` | thêm `stream`, `lang`, `datasource_id`, `return_img` |
| Response | luôn SSE, mỗi event là JSON có `type` | `stream=true` → SSE **markdown**; `stream=false` → **một JSON tổng hợp** |
| Kết quả cuối | không có — client tự ráp theo `type` | `{success, record_id, title, sql, data, chart, image_url}` |

## File sinh ra (`output/`)

| File | Nội dung |
|---|---|
| `01_standard.sse.txt` | stream thô của chat thường, kèm mốc thời gian từng dòng |
| `01_standard.events.json` | các event SSE đã parse thành mảng JSON |
| `02_mcp_stream.txt` | stream markdown của MCP (`stream=true`) |
| `02_mcp_nostream.json` | JSON tổng hợp của MCP (`stream=false`) |

Cột thời gian trong file `.txt` là thứ cho thấy dữ liệu về **dần dần** theo tiến độ pipeline
(sinh SQL → chạy SQL → sinh chart) chứ không phải về một cục ở cuối.

## Kết quả đo thực tế

Cùng câu hỏi "Tổng số thu ngân sách theo từng nhóm nguồn thu" trên DataLaoCai:

| | chat thường | MCP `stream=true` |
|---|---:|---:|
| Dung lượng stream | **670 KB** | **1.8 KB** |
| Số dòng thô | 14 754 | 21 |
| Số event | 7 377 | — (markdown, không có event) |

Chat thường stream **từng token** của LLM (3 690 event `sql-result` + 3 678 event `chart-result`)
để UI hiện chữ chạy dần; MCP chỉ đẩy **kết quả đã hoàn chỉnh của từng pha** (SQL xong mới đẩy cả
khối, data xong mới đẩy cả bảng markdown). Cùng một nội dung, chênh nhau ~370 lần dung lượng.

Mốc thời gian chat thường: `id` 0.5s → `sql` 30s → `sql-data` 30s → `chart` + `finish` 58s.
Mốc thời gian MCP: `Answer ID` 1s → khối SQL 23s → bảng markdown 49s.

> **Lưu ý pha sinh chart hay treo.** Lần chạy đầu tiên bị đứng hẳn ở `chart-result` (LLM rơi vào
> vòng lặp lặp từ, liên quan thiếu `presence_penalty`). Vì vậy `_common.py` đặt `READ_TIMEOUT=60`:
> stream im quá 60 giây thì client dừng đọc và ghi chú vào file, thay vì treo vô hạn.
