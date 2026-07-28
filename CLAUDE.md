# CLAUDE.md

Hướng dẫn cho Claude Code khi làm việc trong repo này.

## Quy ước viết code

### Docstring bắt buộc bằng tiếng Việt

Mọi hàm khi được viết mới hoặc sửa đều **phải có docstring viết bằng tiếng Việt**.

- Thuật ngữ IT được giữ nguyên tiếng Anh (CTE, embedding, session, async, parse, token, JOIN, …).
- Docstring nêu **mục đích** của hàm và những điều không đọc ra ngay từ code: giả định, bẫy, lý do
  chọn cách làm này thay vì cách khác. Không mô tả lại từng dòng.
- Khi sửa một hàm có docstring bằng ngôn ngữ khác (vd tiếng Trung), dịch luôn sang tiếng Việt.
