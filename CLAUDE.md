# CLAUDE.md

Hướng dẫn cho Claude Code khi làm việc trong repo này.

> **Bắt đầu session mới thì đọc [docs/AI_ONBOARDING.md](docs/AI_ONBOARDING.md) trước.** File đó nói
> hệ thống này là gì, nó khác SQLBot upstream ở đâu, và cần biết thêm thì mở tài liệu nào — tránh
> phải khám phá lại codebase từ đầu.

## Quy ước giao tiếp với user

Xưng hô là tôi với bạn.

User hỏi về code thì cái họ cần là **câu trả lời**, không phải bản đọc lại code. AI sinh code nhanh hơn tốc độ người đọc rất nhiều, nên trả lời bằng cách dán code ra là đẩy ngược việc đọc-hiểu về phía user — đúng cái việc họ hỏi để khỏi phải làm.

**Trả lời bằng khái niệm, không bằng code.** Diễn đạt bằng thuật ngữ IT phổ quát — thuật ngữ nền tảng, thuật ngữ của ngôn ngữ và framework (dependency injection, generator, thread pool, race condition, connection pool, middleware, AST, cache invalidation, …). Đây là vốn từ user đã có sẵn, không phụ thuộc codebase này, nên hiểu được ngay mà không phải mở file nào.

**Thuật ngữ riêng của codebase thì dùng dè.** Chỉ dùng những khái niệm đã nổi lên thành danh từ chung của hệ thống: datasource, workspace, M-Schema, pha answer, finish_step… Tên hàm và tên biến nội bộ thì không — chúng chỉ có nghĩa với người vừa đọc đúng file đó.

**Cần trích code thì trích ngắn, và phải kèm đủ ba thứ:** đoạn đó **nằm ở đâu** (file, hàm), **logic xử lý** ra sao, và **tác dụng** của nó trong bức tranh lớn. Code đứng một mình không phải câu trả lời — nó là bằng chứng cho câu trả lời, và bằng chứng thì phải có người diễn giải.

Ưu tiên trỏ `file:line` để user tự mở khi cần, thay vì dán cả khối vào hội thoại.

## Quy ước viết code

### Docstring bắt buộc bằng tiếng Việt

Mọi hàm khi được viết mới hoặc sửa đều **phải có docstring viết bằng tiếng Việt**.

- Thuật ngữ IT được giữ nguyên tiếng Anh (CTE, embedding, session, async, parse, token, JOIN, …).
- Docstring nêu **mục đích** của hàm và những điều không đọc ra ngay từ code: giả định, bẫy, lý do chọn cách làm này thay vì cách khác. Không mô tả lại từng dòng.
- Khi sửa một hàm có docstring bằng ngôn ngữ khác (vd tiếng Trung), dịch luôn sang tiếng Việt.

## Quy ước commit

### Commit message phải NGẮN GỌN

Mặc định là **chỉ một dòng tiêu đề**, tiếng Việt, có tiền tố kiểu Conventional Commits
(`feat:`, `fix:`, `refactor:`, `chore:`, `docs:`).

- Chỉ thêm phần thân khi có một cái "tại sao" mà **đọc diff không ra được**. Khi cần thì tối đa
  vài dòng, không phải vài đoạn.
- Commit message **không phải báo cáo**: không nhét số liệu đo đạc, không liệt kê từng thay đổi
  nhỏ, không kể quá trình. Những thứ đó thuộc về câu trả lời trong hội thoại hoặc file tài liệu
  riêng — commit chỉ cần đủ để người đọc `git log` sau này hiểu chuyện gì đã xảy ra.
- Nội dung dài thuộc về docstring hoặc chú thích ngay tại code, nơi nó nằm cạnh thứ nó giải thích.

## Quy ước giữ tài liệu sống

Xong một task **có sửa code** thì ngay lập tức đề xuất với user cập nhật tài liệu, nếu user đồng ý thì bắt đầu rà soát bộ tài liệu trong `docs/` và cập nhật. Tài liệu lệch code còn tệ hơn không có tài liệu: session sau đọc phải sẽ suy luận sai mà không biết mình sai.

- Rà rồi thấy không phải sửa gì thì nói thẳng "tài liệu không bị ảnh hưởng", đừng im lặng bỏ qua —
  user cần biết bước này đã chạy.
- **Đề xuất, không tự sửa.** Nêu file nào, mục nào, sửa thành gì, rồi chờ user quyết.
- Đổi các thứ dưới đây thì gần như chắc chắn phải cập nhật tài liệu:

  | Đổi gì | Kéo theo |
  |---|---|
  | Pha trong pipeline, `ChatFinishStep`, vòng retry/hạ cấp | `docs/TEXT2SQL_PIPELINE.md` |
  | Prompt trong `templates/` | `docs/TEXT2SQL_PIPELINE.md` |
  | Event SSE (thêm/bỏ/đổi schema) | `docs/TEXT2SQL_PIPELINE.md` **và** `backend/scripts/chat_stream_demo/API_SPEC.md` |
  | Endpoint datasource | `backend/scripts/chat_stream_demo/DATASOURCE_API_SPEC.md` |
  | Biến trong `common/core/config.py` | `docs/OPERATIONS.md` |
  | Cấu trúc thư mục, model DB, migration, cách chạy/deploy | `docs/BACKEND_ARCHITECTURE.md` / `docs/OPERATIONS.md` |
  | Phát hiện một bẫy mới (hành vi im lặng, lỗi khó đoán) | mục "Bẫy đã biết" của `docs/OPERATIONS.md` |

- Tài liệu trỏ tới code bằng `file:line`. Sửa code làm lệch số dòng thì trong lúc rà nhớ kiểm lại
  các anchor của đúng file vừa sửa.
