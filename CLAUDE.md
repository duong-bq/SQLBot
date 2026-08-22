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

### PEP 8 và PEP 257 — chỉ áp cho code mới

Code Python **viết mới** tuân thủ [PEP 8](https://peps.python.org/pep-0008/) và
[PEP 257](https://peps.python.org/pep-0257/). Code **cũ** thì giữ nguyên phong cách đang có.

Ranh giới "mới" gồm: file mới, class mới, hàm/method mới, dòng import mới, và những dòng bạn thật
sự viết ra bên trong một hàm cũ. Ranh giới "cũ" gồm mọi dòng bạn chỉ đi ngang qua — kể cả khi
chúng nằm trong hàm bạn đang sửa, kể cả khi chúng dài 120 ký tự và thụt lề lạ.

**Lý do phải kìm tay: conflict khi pull từ upstream.** Repo này là fork của SQLBot upstream. Format
lại một khối code cũ làm mọi dòng trong khối đó thành "đã sửa" dưới mắt `git`, nên lần merge sau sẽ
conflict ở đúng những chỗ mà thực chất chẳng ai đổi logic. Một hàm được dọn dẹp cho đẹp có thể đổi
lấy hàng chục dòng conflict thủ công — cái giá đó không đáng.

Hệ quả cần nhớ khi **di chuyển** code (bóc hàm, tách module): khối được di chuyển vẫn là code cũ.
Dịch chuyển nguyên xi rồi chỉ format phần khung bạn tự viết (chữ ký, docstring, các dòng nối) —
như thế `diff` còn đọc được là "chỉ dịch chuyển", và có thể kiểm chứng bằng cách so từng byte với
bản gốc.

Vài điểm cụ thể hay dùng:

- **Độ dài dòng**: khuyến nghị 79, chấp nhận nới tới **89** nếu dòng dài hơn thật sự dễ đọc hơn.
  Đây là ước lượng tương đối, không phải luật đếm ký tự.
- **Docstring** (PEP 257): dòng tóm tắt nằm gọn trên **một dòng**, ngay sau `"""`, kết thúc bằng
  dấu chấm. Docstring nhiều dòng thì để một dòng trống sau phần tóm tắt, và `"""` đóng nằm riêng
  một dòng. Docstring của class có thêm một dòng trống trước thành viên đầu tiên.
- **Import**: mỗi dòng một module, xếp theo nhóm thư viện chuẩn → bên thứ ba → nội bộ.
- Hai dòng trống giữa các định nghĩa ở cấp module, một dòng trống giữa các method.

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
