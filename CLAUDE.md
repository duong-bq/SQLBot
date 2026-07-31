# CLAUDE.md

Hướng dẫn cho Claude Code khi làm việc trong repo này.

## Quy ước viết code

### Docstring bắt buộc bằng tiếng Việt

Mọi hàm khi được viết mới hoặc sửa đều **phải có docstring viết bằng tiếng Việt**.

- Thuật ngữ IT được giữ nguyên tiếng Anh (CTE, embedding, session, async, parse, token, JOIN, …).
- Docstring nêu **mục đích** của hàm và những điều không đọc ra ngay từ code: giả định, bẫy, lý do
  chọn cách làm này thay vì cách khác. Không mô tả lại từng dòng.
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
