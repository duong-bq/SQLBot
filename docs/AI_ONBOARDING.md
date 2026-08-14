# Onboarding cho AI coding agent

> Đọc file này **đầu mỗi session**. Nó không dạy code — nó nói cho bạn biết hệ thống này là gì,
> nó đã lệch khỏi upstream ở đâu, và muốn biết thêm thì mở file nào. Chỉ nạp tiếp file chuyên đề
> khi thực sự cần: đó là lý do bộ tài liệu này được chia nhỏ.

## 1. SQLBot là gì

Hệ thống hỏi dữ liệu bằng ngôn ngữ tự nhiên (ChatBI): người dùng hỏi bằng tiếng Việt → LLM sinh
SQL → backend **thực sự chạy** SQL đó trên database nghiệp vụ → LLM đọc kết quả và trả lời bằng
lời. LLM không bao giờ chạm trực tiếp vào database; nó chỉ nhận schema dạng text và trả về SQL.

Upstream là [dataease/SQLBot](https://github.com/dataease/SQLBot). Repo này là **bản fork đã đi rất
xa khỏi upstream**, cả về pipeline lẫn mục tiêu sản phẩm.

## 2. Fork này khác upstream ở đâu

Đây là phần quan trọng nhất của cả file. Kiến thức về SQLBot upstream (kể cả từ tài liệu trên
mạng) sẽ dẫn bạn đi sai ở đúng bốn chỗ này:

| Điểm | Upstream | Repo này |
|---|---|---|
| Pha cuối pipeline | Sinh **biểu đồ** (chart JSON) | Sinh **câu trả lời bằng lời** (answer), rồi chart chạy **song song** với nó. Bật/tắt chart bằng `GENERATE_CHART_ENABLED` |
| Khi pha SQL hỏng | Trả event `error`, hết lượt | Sinh lại SQL (`LLM_SQL_MAX_RETRY`), vẫn hỏng thì **hạ cấp** sang trả lời dựa trên lịch sử hội thoại |
| Đối tượng dùng | Web UI là chính | **API-first**: đối tác tích hợp gọi thẳng `POST /chat/question` qua SSE. Một lời gọi trả đủ `sql`, số liệu, `answer`, `chart` — không phải gọi thêm API nào |
| Ngôn ngữ prompt | Tiếng Trung | Tiếng Việt (`backend/templates/template.yaml` đã dịch và viết lại) |

Hệ quả cụ thể: `ChatFinishStep` đã bị **đánh số lại** để chèn `GENERATE_ANSWER` vào giữa
`QUERY_DATA` và `GENERATE_CHART`. Xem [chat_model.py:54](../backend/apps/chat/models/chat_model.py#L54).

## 3. Trạng thái hiện tại

- Nhánh làm việc: `feature/ai-permission-answer`. Nhánh gốc để mở PR: `main`.
- Đối tác tích hợp: HĐND TP Hà Nội. Họ gọi API, không dùng web UI.
- `finish_step` mặc định của `POST /chat/question` là `GENERATE_CHART`, do `GENERATE_CHART_ENABLED`
  quyết định (mặc định bật) qua `default_finish_step`
  ([chat.py:462](../backend/apps/chat/api/chat.py#L462)); nhánh MCP truyền `QUERY_DATA`
  ([mcp.py:258](../backend/apps/mcp/mcp.py#L258)).
- Số lần gọi LLM cho một lượt hỏi bình thường: **3** (sinh SQL + answer + chart; hai lần sau chạy
  song song). Tắt chart thì còn 2.
- Kết quả SQL đi kèm luôn trong event `sql-data` của stream. Endpoint
  `GET /chat/record/{id}/data` vẫn còn để web UI dựng lại hội thoại cũ, nhưng đối tác không cần
  dùng tới và `API_SPEC.md` đã bỏ mô tả nó.
- Backend Python 3.11 + FastAPI, quản lý dependency bằng `uv`; frontend Vue 3 + TypeScript.
- Có cổng `POST /hooks/ai-sync` (`apps/hooks/`) nhận bản tin đồng bộ quyền user từ hệ thống SW, ghi
  vào `ai_user_permissions`. **Chưa** nối vào pipeline Text2SQL — LLM chưa dùng dữ liệu này để giới
  hạn quyền truy vấn.
- Nhóm endpoint quản trị tài khoản định danh bằng **`account`** thay vì `id`: `/user/by-account/*`
  ([user.py:215](../backend/apps/system/api/user.py#L215)). Tồn tại vì SW đặt `account` bằng đúng
  `userId` bên họ nên không giữ id snowflake của SQLBot — cùng một định danh dùng cho cả API quản
  trị lẫn `POST /hooks/ai-sync`. Sáu route chỉ phân giải `account` → `id` rồi gọi lại handler cũ.
- Nạp nguồn dữ liệu từ file Excel/CSV có đường **bất đồng bộ**: `POST
  /datasource/createFromExcelAsync` nhận một **presigned URL** trỏ tới file (không nhận bytes), trả
  `202` ngay, rồi tự tải và nạp ở nền, báo kết quả về SW bằng callback HTTP. Cần đặt
  `AI_CALLBACK_URL` (rỗng là tắt gửi) và `EXCEL_DOWNLOAD_ALLOWED_HOSTS` (rỗng là **chặn hết**).
  Kiến trúc ở [BACKEND_ARCHITECTURE.md](BACKEND_ARCHITECTURE.md) §5, hợp đồng ở
  `DATASOURCE_API_SPEC.md` §9.

## 4. Bản đồ tài liệu — hỏi gì thì đọc file nào

### Bộ tài liệu này (`docs/`)

| Cần biết | Đọc |
|---|---|
| Pipeline Text2SQL chạy thế nào, prompt lắp ra sao, bảo mật SQL, RAG | [TEXT2SQL_PIPELINE.md](TEXT2SQL_PIPELINE.md) |
| Code nằm ở đâu, sửa chức năng X thì mở file nào, data model, auth | [BACKEND_ARCHITECTURE.md](BACKEND_ARCHITECTURE.md) |
| Chạy local, biến cấu hình, deploy, chạy eval, debug một lượt hỏi, bẫy đã biết | [OPERATIONS.md](OPERATIONS.md) |

`docs/README.en.md` là README tiếng Anh của upstream, không mô tả fork này.

### Spec API (đang chính xác, **đừng viết lại**)

| File | Nội dung |
|---|---|
| [backend/scripts/chat_stream_demo/API_SPEC.md](../backend/scripts/chat_stream_demo/API_SPEC.md) | Hợp đồng tích hợp chat: login, list datasource, `POST /chat/question` (SSE) — đủ catalog event, bảng lỗi, giới hạn hệ thống |
| [backend/scripts/chat_stream_demo/DATASOURCE_API_SPEC.md](../backend/scripts/chat_stream_demo/DATASOURCE_API_SPEC.md) | 26 endpoint quản trị datasource — tạo/sync/chọn bảng/sửa chú thích/quan hệ bảng, cho cả database quan hệ lẫn file Excel/CSV; §9 là luồng nạp Excel bất đồng bộ + hợp đồng callback |
| [backend/scripts/ai_sync_hook/AI_SYNC_HOOK_API_SPEC.md](../backend/scripts/ai_sync_hook/AI_SYNC_HOOK_API_SPEC.md) | Cổng `POST /hooks/ai-sync` nhận bản tin đồng bộ quyền từ SW — actionType, full-snapshot, idempotency, bảng lỗi |
| [backend/scripts/ai_sync_hook/AI_PERMISSION_QUERY_API_SPEC.md](../backend/scripts/ai_sync_hook/AI_PERMISSION_QUERY_API_SPEC.md) | 2 endpoint GET đọc lại quyền đã đồng bộ (dev/test), tách khỏi spec ghi ở trên |
| [backend/scripts/ai_sync_hook/USER_ADMIN_API_SPEC.md](../backend/scripts/ai_sync_hook/USER_ADMIN_API_SPEC.md) | Quản trị tài khoản cho SW: đăng nhập, tạo/sửa/khoá/xoá user, đổi mật khẩu. Mọi endpoint định danh bằng `account`. Bản `..._LITE.md` cạnh đó là bản rút gọn gửi đối tác — sửa cái nào thì sửa cả hai |
| [backend/scripts/eval_text2sql/README.md](../backend/scripts/eval_text2sql/README.md) | Cách dùng harness đánh giá chất lượng |

### Tài liệu ở repo root — biết để **khỏi mở nhầm**

| File | Là gì |
|---|---|
| `Text2SQL_Evaluation_Plan.md` | Chiến lược đánh giá: chọn metric nào, đo theo pha nào. Đọc trước khi thiết kế phép đo mới |
| `Text2SQL_Eval_Status.md` | Bàn giao trạng thái công việc của module eval |
| `Text2SQL_survey.md` | Ghi chép khảo sát pipeline Text2SQL nói chung (academic), **không** mô tả code repo này |
| `BaoCao_DanhGia_Text2SQL_SQLBot.md`, `..._v2.md` | Kết quả đo bộ test DataLaoCai (64 câu, rồi 32 câu) |
| `BaoCao_DanhGia_Text2SQL_HDND.md` | Kết quả đo bộ test HĐND qua HTTP |
| `BaoCao_ChatLuongTraLoi_HDND_BA.md` | Báo cáo chất lượng **câu trả lời** (không phải SQL), viết cho người đọc nghiệp vụ |
| `Cum_ThuNganSach.md`, `Cum_ChiNganSach.md`, `Cum_DauTuCong.md` | Sơ đồ liên kết bảng + gold SQL của DB nghiệp vụ Lào Cai, dùng để soạn testset |
| `README.md`, `CONTRIBUTING.md` | Của upstream, tiếng Trung |

## 5. Ba luật riêng của repo này

**1. Đừng đổi biến `LLM_*` trong `config.py` theo cảm giác.**
Các giá trị mặc định (`LLM_SQL_MAX_RETRY`, `LLM_ANSWER_HISTORY_*`, `LLM_SQL_HISTORY_JSON_ONLY`…)
là kết quả đo bằng `backend/scripts/eval_text2sql`, không phải giá trị đặt bừa. Đổi thì phải chạy
lại harness và kèm số liệu. Chi tiết trong [OPERATIONS.md](OPERATIONS.md) §2.

**2. Chú thích trong code là nguồn sự thật về "tại sao".**
[llm.py](../backend/apps/chat/task/llm.py) và [config.py](../backend/common/core/config.py) có
chú thích rất dày, nhiều chỗ ghi cả kết quả đo được và cảnh báo *"cố ý giữ nhánh này khi merge từ
upstream"* (ví dụ [llm.py:102](../backend/apps/chat/task/llm.py#L102)). Trước khi "dọn dẹp" một
đoạn trông thừa, đọc chú thích của nó — phần lớn là vá lỗi đã đo được.

**3. Mọi chỗ cắt bớt dữ liệu đưa vào prompt đều phải nói cho LLM biết đã cắt.**
Cắt im lặng thì LLM đếm số dòng nó nhận được rồi báo đó là tổng. Ca đo thật: SQL trả 136 dòng,
câu trả lời khẳng định "tổng cộng 72 nghị quyết". Cặp `ANSWER_MAX_ROWS` +
`build_data_scope_note` ([llm.py:74-77](../backend/apps/chat/task/llm.py#L74)) tồn tại vì lý do
đó và **phải luôn đi cùng nhau**.

## 6. Đi tiếp

Muốn sửa pha answer → [TEXT2SQL_PIPELINE.md](TEXT2SQL_PIPELINE.md) §5, rồi mở
[llm.py:611](../backend/apps/chat/task/llm.py#L611) và khối `answer` trong
[templates/template.yaml:630](../backend/templates/template.yaml#L630).
