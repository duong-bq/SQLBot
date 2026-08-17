"""Tài liệu .docx đính kèm lượt hỏi chat: tải từ presigned URL, trích text, render khối prompt.

Toàn bộ là hàm thuần đồng bộ (không session, không request) theo đúng khuôn của
``datasource/utils/remote_file.py`` — và tải/parse là I/O + CPU đồng bộ, nên caller ở route async
phải bọc ``asyncio.to_thread``.

Vị trí của tài liệu trong hệ thống: nó là MESSAGE, không phải hạ tầng kiểu schema. Khối render ra
từ đây được ghép vào câu hỏi của người dùng lúc dựng prompt, đi vào ``chat_log`` như mọi message
khác, chịu cắt trần ký tự và trôi theo cửa sổ lịch sử. Vì thế module này chỉ lo ba việc cơ học
(tải, trích, bọc thẻ), không có đặc quyền "sống mãi" nào để cài.

Ba trần ký tự khác nhau cùng tồn tại, đừng gộp:

- Trần LÚC TRÍCH (``CHAT_DOC_EXTRACT_MAX_CHARS``): chống zip bomb — file .docx là zip, vài trăm KB
  nén có thể bung ra hàng trăm MB text. Đây là trần của bản lưu DB.
- Trần LÚC RENDER cho lượt đính file (``CHAT_DOC_PROMPT_MAX_CHARS``): ngân sách context của prompt
  hiện hành.
- Trần LÚC RENDER trong khối lịch sử pha answer (``CHAT_DOC_HISTORY_MAX_CHARS``): chặt hơn nữa,
  vì lịch sử chứa nhiều lượt và còn phải nhường chỗ cho ``<data>`` của lượt hiện tại.

Mọi chỗ cắt đều phải nói cho LLM biết đã cắt — quy tắc chung của repo, xem AI_ONBOARDING.md §5.
"""

import os
import re
from dataclasses import dataclass
from io import BytesIO

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from apps.datasource.utils.excel_import import ExcelImportError
from apps.datasource.utils.remote_file import download_bytes, validate_source_url
from common.core.config import settings

# Mã lỗi mới cho ca file tải được nhưng không đọc được (không phải docx thật, file hỏng, có mật
# khẩu). Các mã còn lại (URL_*, DOWNLOAD_*, FILE_TOO_LARGE) dùng lại nguyên bộ của remote_file.
ERR_DOC_PARSE_FAILED = "DOC_PARSE_FAILED"

ATTACHMENT_EXTENSIONS = ("docx",)

# URL chỉ cần sống qua đúng một lần tải ngay trong request, không có hàng đợi worker nào phía sau
# như luồng excel — nên TTL tối thiểu chỉ là vài giây đề phòng lệch đồng hồ.
_URL_MIN_TTL_SECONDS = 10

# Ký tự bị khử khỏi filename trước khi đưa vào thuộc tính của thẻ <attached-document>: dấu nháy và
# ngoặc nhọn phá cấu trúc thẻ, ký tự điều khiển thì không có lý do gì xuất hiện trong tên file.
_UNSAFE_FILENAME_CHARS = re.compile(r'["<>\x00-\x1f]')


@dataclass(frozen=True)
class AttachmentContent:
    """Kết quả trích một tài liệu đính kèm: tên file hiển thị, text markdown, cờ đã-cắt."""

    filename: str
    content: str
    truncated: bool


def _table_to_markdown(table: Table) -> list[str]:
    """Chuyển một bảng docx thành các dòng markdown table.

    Dòng đầu của bảng được coi là header — giả định đúng với đa số tài liệu hành chính; bảng không
    có header thật thì LLM vẫn đọc được, chỉ là dòng đầu bị in đậm oan.

    Text trong ô được ép phẳng (xuống dòng thành khoảng trắng, ``|`` được escape) vì cả hai ký tự
    đó phá vỡ cấu trúc dòng của markdown table.
    """
    lines: list[str] = []
    for idx, row in enumerate(table.rows):
        cells = [' '.join(cell.text.split()).replace('|', '\\|') for cell in row.cells]
        lines.append('| ' + ' | '.join(cells) + ' |')
        if idx == 0:
            lines.append('|' + '|'.join([' --- '] * len(cells)) + '|')
    return lines


def extract_docx_text(data: bytes, max_chars: int | None = None) -> tuple[str, bool]:
    """Trích text từ bytes của một file .docx, trả về ``(text, đã_cắt_hay_chưa)``.

    Đọc theo đúng thứ tự xuất hiện trong tài liệu (``iter_inner_content`` trộn đoạn văn và bảng
    theo vị trí thật), bảng chuyển thành markdown table.

    Trần ``max_chars`` ép NGAY TRONG lúc duyệt: gặp trần là dừng đọc, không trích nốt phần còn lại
    rồi mới cắt — đó là lớp chống zip bomb ở tầng text (lớp tầng byte nằm ở trần dung lượng tải).
    Cắt tại ranh giới đoạn/dòng bảng chứ không cắt giữa chừng một đoạn.

    Ném ``ExcelImportError(DOC_PARSE_FAILED)`` khi bytes không phải docx đọc được. Bắt Exception
    rộng có chủ ý: python-docx ném đủ loại (BadZipFile, KeyError, ValueError...) tùy kiểu hỏng,
    mà với caller thì mọi kiểu hỏng đều là một chuyện — file không đọc được.
    """
    limit = settings.CHAT_DOC_EXTRACT_MAX_CHARS if max_chars is None else max_chars
    try:
        doc = Document(BytesIO(data))
        pieces: list[str] = []
        total = 0
        truncated = False
        for item in doc.iter_inner_content():
            if isinstance(item, Paragraph):
                text = item.text.strip()
                lines = [text] if text else []
            elif isinstance(item, Table):
                lines = _table_to_markdown(item)
            else:
                continue
            for line in lines:
                # +1 cho ký tự xuống dòng nối các mảnh
                if total + len(line) + 1 > limit:
                    truncated = True
                    break
                pieces.append(line)
                total += len(line) + 1
            if truncated:
                break
        return '\n'.join(pieces), truncated
    except ExcelImportError:
        raise
    except Exception as e:
        raise ExcelImportError(ERR_DOC_PARSE_FAILED,
                               f"Cannot read .docx file: {type(e).__name__}: {e}")


def render_attachment_block(filename: str | None, content: str, source_truncated: bool,
                            max_chars: int, indent: str = '') -> str:
    """Bọc nội dung tài liệu vào thẻ ``<attached-document>`` để ghép vào prompt.

    Cặp thẻ này là "tay cầm" ổn định cho các chính sách quản lý hội thoại về sau (tóm tắt, vứt bỏ,
    chuyển graph-memory): muốn tìm/thay khối tài liệu trong một message đã lưu thì tìm theo thẻ.

    Áp trần ``max_chars`` lần nữa tại đây — bản lưu DB dài hơn ngân sách prompt là chuyện bình
    thường. Bị cắt ở bất kỳ tầng nào (lúc trích ``source_truncated``, hay tại đây) đều phải chèn
    lời báo tường minh cho LLM, nếu không nó sẽ coi phần nhận được là toàn bộ tài liệu.
    """
    body = content or ''
    cut_here = len(body) > max_chars
    if cut_here:
        body = body[:max_chars]
    notes = []
    if cut_here:
        notes.append(f'(Tài liệu dài quá ngân sách ngữ cảnh, CHỈ {max_chars} ký tự đầu được đưa '
                     f'vào đây, phần còn lại KHÔNG được gửi cho bạn.)')
    elif source_truncated:
        notes.append('(Tài liệu gốc dài hơn bản trích này: nội dung đã bị cắt bớt từ lúc trích '
                     'xuất, phần cuối KHÔNG được gửi cho bạn.)')
    safe_name = _UNSAFE_FILENAME_CHARS.sub('_', filename or 'document.docx')[:120]
    lines = [f'<attached-document filename="{safe_name}">', body]
    lines.extend(notes)
    lines.append('</attached-document>')
    if indent:
        return '\n'.join(indent + line if line else line for line in '\n'.join(lines).split('\n'))
    return '\n'.join(lines)


def fetch_docx_attachment(file_url: str) -> AttachmentContent:
    """Trọn gói pha nhận tài liệu: kiểm URL → tải vào RAM → trích text. Hàm ĐỒNG BỘ.

    Không probe trước như luồng excel: file bé và tải ngay trong request, lời tải chính là lời
    thăm dò — mọi câu trả lời dứt khoát của nguồn (403/404/quá trần) đều nổi lên ngay tại đây.

    Ném ``ExcelImportError`` cho mọi kiểu hỏng; caller dịch thành event SSE ``error`` với
    ``error_code`` giữ nguyên.
    """
    src = validate_source_url(file_url, allowed_extensions=ATTACHMENT_EXTENSIONS,
                              min_ttl_seconds=_URL_MIN_TTL_SECONDS)
    data = download_bytes(
        src,
        limit=max(1, settings.CHAT_DOC_MAX_MB) * 1024 * 1024,
        connect_timeout=settings.EXCEL_DOWNLOAD_CONNECT_TIMEOUT,
        read_timeout=settings.EXCEL_DOWNLOAD_READ_TIMEOUT,
        total_timeout=settings.CHAT_DOC_DOWNLOAD_TIMEOUT,
    )
    content, truncated = extract_docx_text(data, settings.CHAT_DOC_EXTRACT_MAX_CHARS)
    filename = os.path.basename(src.object_key.replace('\\', '/')) or 'document.docx'
    return AttachmentContent(filename=filename, content=content, truncated=truncated)
