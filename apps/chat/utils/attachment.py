"""Tài liệu .docx đính kèm lượt hỏi chat: tải từ presigned URL, trích text, render khối prompt.

Toàn bộ là hàm thuần đồng bộ (không session, không request) theo đúng khuôn của
``datasource/utils/remote_file.py`` — và tải/parse là I/O + CPU đồng bộ, nên caller ở route async
phải bọc ``asyncio.to_thread``.

Vị trí của tài liệu trong hệ thống: nó là MESSAGE, không phải hạ tầng kiểu schema. Khối render ra
từ đây được ghép vào câu hỏi của người dùng lúc dựng prompt, đi vào ``chat_log`` như mọi message
khác, chịu cắt trần ký tự và trôi theo cửa sổ lịch sử. Vì thế module này chỉ lo ba việc cơ học
(tải, trích, bọc thẻ), không có đặc quyền "sống mãi" nào để cài.

Ba trần ký tự khác nhau cùng tồn tại, đừng gộp — và hai trong ba là trần TỔNG chứ không phải
trần mỗi file, vì một lượt hỏi đính được nhiều tài liệu:

- Trần LÚC TRÍCH (``CHAT_DOC_EXTRACT_MAX_CHARS``), tính cho MỖI FILE: chống zip bomb — file .docx
  là zip, vài trăm KB nén có thể bung ra hàng trăm MB text. Đây là trần của bản lưu DB.
- Trần LÚC RENDER cho lượt đính file (``CHAT_DOC_PROMPT_MAX_CHARS``), tính cho CẢ LƯỢT: ngân sách
  context của prompt hiện hành. Nó không được nở ra theo số file client gửi, nên nhiều file thì
  chia nhau đúng ngần ấy ký tự (``split_char_budget``).
- Trần LÚC RENDER trong khối lịch sử pha answer (``CHAT_DOC_HISTORY_MAX_CHARS``), tính cho MỖI
  LƯỢT trong cửa sổ: chặt hơn nữa, vì lịch sử chứa nhiều lượt và còn phải nhường chỗ cho
  ``<data>`` của lượt hiện tại.

Mọi chỗ cắt đều phải nói cho LLM biết đã cắt — quy tắc chung của repo, xem AI_ONBOARDING.md §5.
"""

import os
import re
from concurrent.futures import ThreadPoolExecutor
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

# Client gửi quá nhiều file trong một lượt. Phát hiện được mà không cần chạm tới URL nào, nên nó
# chặn ngay trước cả vòng kiểm URL — cùng nhóm HTTP 400 với các mã URL_*.
ERR_TOO_MANY_FILES = "TOO_MANY_FILES"

ATTACHMENT_EXTENSIONS = ("docx",)

# URL chỉ cần sống qua đúng một lần tải ngay trong request, không có hàng đợi worker nào phía sau
# như luồng excel — nên TTL tối thiểu chỉ là vài giây đề phòng lệch đồng hồ.
_URL_MIN_TTL_SECONDS = 10

# Ký tự bị khử khỏi filename trước khi đưa vào thuộc tính của thẻ <attached-document>: dấu nháy và
# ngoặc nhọn phá cấu trúc thẻ, ký tự điều khiển thì không có lý do gì xuất hiện trong tên file.
_UNSAFE_FILENAME_CHARS = re.compile(r'["<>\x00-\x1f]')

# Pool RIÊNG cho việc tải tài liệu, xem lý do ở CHAT_DOC_DOWNLOAD_WORKERS trong config: một lượt
# tải giữ thread hàng chục giây, không được phép chiếm thread của pool mặc định — nơi mọi lời gọi
# DB đồng bộ của app đang xếp hàng.
#
# Dùng `run_in_executor` với pool này thì KHÔNG có contextvar nào được mang sang thread mới (khác
# `asyncio.to_thread`). Vô hại ở đây vì mọi hàm trong module này là hàm thuần — không session,
# không request context, không i18n — và đó là một trong những lý do module được thiết kế như vậy.
docx_download_executor = ThreadPoolExecutor(max_workers=settings.CHAT_DOC_DOWNLOAD_WORKERS,
                                            thread_name_prefix='chat-docx')


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


def split_char_budget(lengths: list[int], total: int) -> list[int]:
    """Chia ngân sách ký tự của MỘT lượt hỏi cho các tài liệu của lượt đó. Trả về suất từng file.

    Chia đều là quy tắc gốc: không tài liệu nào được lấn suất của tài liệu khác chỉ vì nó dài hơn.
    Nhưng chia đều trần trụi thì một file 200 ký tự vẫn ôm nguyên suất 10.000 của nó, 9.800 kia bốc
    hơi, trong khi file bên cạnh đang bị cắt cụt — nên phần suất không dùng hết được gom lại chia
    đều tiếp cho những file còn đói, lặp tới khi không còn ai thừa. Đây là water-filling, không
    phải "ưu tiên file dài": file dài chỉ nhận được thứ mà file ngắn không cần tới.

    Bất biến: tổng kết quả không bao giờ vượt ``total`` (lý do tồn tại của trần này), và thứ tự trả
    về khớp thứ tự vào. ``total`` bé hơn số file thì có file nhận suất 0 — khối của nó rỗng nhưng
    vẫn mang lời báo đã cắt, chứ không biến mất im lặng.
    """
    n = len(lengths)
    if n == 0:
        return []
    quotas = [0] * n
    pending = list(range(n))
    remaining = max(0, total)
    while pending:
        share = remaining // len(pending)
        if share <= 0:
            break
        # File nào ngắn hơn suất của nó thì chốt luôn ở đúng độ dài thật, phần thừa quay lại quỹ.
        satisfied = [i for i in pending if lengths[i] <= share]
        if not satisfied:
            # Không ai còn thừa: mọi file còn lại đều dài hơn suất, chia nốt rồi dừng.
            for i in pending:
                quotas[i] = share
            break
        for i in satisfied:
            quotas[i] = lengths[i]
            remaining -= lengths[i]
        satisfied_set = set(satisfied)
        pending = [i for i in pending if i not in satisfied_set]
    return quotas


def render_attachment_blocks(attachments, total_max_chars: int, indent: str = '') -> str:
    """Render các khối ``<attached-document>`` của CÙNG một lượt hỏi, dùng chung một ngân sách.

    Nhận bất cứ object nào có ba thuộc tính ``filename``/``content``/``truncated`` — đúng cả
    ``AttachmentContent`` (vừa trích ngay trong request) lẫn dòng ``ChatAttachment`` đọc lên từ DB
    lúc dựng lịch sử, hai nguồn duy nhất của khối này.

    ``total_max_chars`` là trần cho CẢ LƯỢT chứ không phải cho mỗi file: ngân sách context thuộc về
    prompt, nó không được phép nở ra theo số file client gửi. Cách chia xem ``split_char_budget``.

    Thứ tự giữ nguyên thứ tự client gửi — LLM đọc theo thứ tự, mà câu hỏi hay tham chiếu bằng vị
    trí ("tài liệu thứ hai"). Danh sách rỗng trả chuỗi rỗng để caller khỏi phải phân nhánh.
    """
    items = list(attachments)
    if not items:
        return ''
    quotas = split_char_budget([len(a.content or '') for a in items], total_max_chars)
    return '\n'.join(
        render_attachment_block(a.filename, a.content, a.truncated, max_chars=q, indent=indent)
        for a, q in zip(items, quotas)
    )


def dedupe_file_urls(urls: list[str]) -> list[tuple[int, str]]:
    """Bỏ URL trùng trong một lượt, trả về ``(vị trí trong mảng client gửi, url)`` của bản đầu tiên.

    Giữ lại vị trí gốc vì nó là thứ duy nhất chỉ đúng được file nào hỏng khi báo lỗi: sau khi bỏ
    trùng, chỉ số trong danh sách rút gọn không còn khớp mảng ``fileUrls`` mà client cầm trong tay.

    So sánh bằng chính chuỗi URL (đã strip). Hai URL khác chữ ký vẫn có thể trỏ về cùng một object,
    nhưng server không có cách nào biết mà không tải cả hai — nên phép so sánh dừng ở mức chắc chắn
    đúng. Trùng thật mà không lọc thì vừa tốn một lượt tải, vừa nhồi cùng một tài liệu hai lần vào
    prompt và ngốn hai suất ngân sách.
    """
    seen: set[str] = set()
    result: list[tuple[int, str]] = []
    for index, url in enumerate(urls):
        key = (url or '').strip()
        if key in seen:
            continue
        seen.add(key)
        result.append((index, url))
    return result


def fetch_docx_attachment(file_url: str) -> AttachmentContent:
    """Trọn gói pha nhận tài liệu: kiểm URL → tải vào RAM → trích text. Hàm ĐỒNG BỘ.

    Không probe trước như luồng excel: file bé và tải ngay trong request, lời tải chính là lời
    thăm dò — mọi câu trả lời dứt khoát của nguồn (403/404/quá trần) đều nổi lên ngay tại đây.

    Cố ý chỉ nhận MỘT url: một lượt hỏi đính nhiều file thì caller gọi song song nhiều lần (route
    bọc mỗi lời gọi trong ``asyncio.to_thread``), chứ hàm này không biết gì về lô. Nhờ vậy lỗi luôn
    thuộc về đúng một file và caller quy được nó về đúng vị trí trong ``fileUrls``.

    Ném ``ExcelImportError`` cho mọi kiểu hỏng; caller dịch thành HTTP 400 với ``error_code`` giữ
    nguyên.
    """
    src = validate_source_url(file_url, allowed_extensions=ATTACHMENT_EXTENSIONS,
                              min_ttl_seconds=_URL_MIN_TTL_SECONDS)
    data = download_bytes(
        src,
        limit=max(1, settings.CHAT_DOC_MAX_MB) * 1024 * 1024,
        connect_timeout=settings.FILE_DOWNLOAD_CONNECT_TIMEOUT,
        read_timeout=settings.FILE_DOWNLOAD_READ_TIMEOUT,
        total_timeout=settings.CHAT_DOC_DOWNLOAD_TIMEOUT,
    )
    content, truncated = extract_docx_text(data, settings.CHAT_DOC_EXTRACT_MAX_CHARS)
    filename = os.path.basename(src.object_key.replace('\\', '/')) or 'document.docx'
    return AttachmentContent(filename=filename, content=content, truncated=truncated)
