"""Lấy file nguồn về đĩa từ một URL do hệ ngoài cung cấp (presigned URL của MinIO/S3).

Tách thành module riêng vì bước tải chạy trong thread nền của worker import, mà worker không gọi
được route handler: ``require_permissions`` đọc user qua contextvar và ném ``RuntimeError`` khi
không có request. Nên mọi thứ ở đây là hàm thuần — không session, không request, không i18n — theo
đúng khuôn của ``excel_import.py``.

Lỗi ném ra dùng lại ``ExcelImportError`` để worker khỏi phải thêm nhánh bắt mới: nhánh sẵn có đã
đưa thẳng ``error_code`` vào payload callback.

Bốn mối nguy chi phối thiết kế ở đây, không cái nào đọc ra được từ code:

- **SSRF.** URL do client đưa mà server tự đi gọi, từ bên trong mạng nội bộ. Chặn bằng allowlist
  host, VÀ bằng việc cấm đi theo redirect — redirect là đường vòng qua mọi allowlist chỉ kiểm URL
  đầu tiên.
- **Chữ ký nằm trong query string.** Nó vừa là credential (tuyệt đối không log nguyên URL), vừa là
  lý do mọi phép kiểm đuôi file phải làm trên path đã cắt query.
- **Nguồn có thể nói dối.** ``Content-Length`` là lời khai; trần dung lượng phải ép thêm một lần
  nữa bằng cách đếm byte thật trong lúc stream.
- **Object key là chuỗi do bên ngoài kiểm soát** và nó đi vào tên file trên đĩa. URL còn cho phép
  mã hóa phần trăm, nên ``..%2F`` vượt qua được phép cắt theo dấu gạch chéo nếu quên giải mã trước.

Cả ``probe_source`` và ``download_to`` đều là lời gọi mạng ĐỒNG BỘ. Gọi từ ``async def`` phải bọc
``asyncio.to_thread``, nếu không là chặn cả event loop của tiến trình.
"""

import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from common.core.config import settings
from common.utils.utils import SQLBotLogUtil

from .excel_import import ExcelImportError

# Mã lỗi. Chuỗi ổn định, KHÔNG đổi khi sửa câu thông báo — hệ ngoài phân loại bằng chính chuỗi này.
# Bốn mã đầu phát hiện được mà không cần chạm mạng nên pha đồng bộ dịch chúng thành HTTP 400; bốn
# mã sau chỉ biết sau khi đã hỏi nguồn, nên chúng đi đường callback.
ERR_URL_INVALID = "URL_INVALID"
ERR_URL_HOST_NOT_ALLOWED = "URL_HOST_NOT_ALLOWED"
ERR_URL_BAD_EXTENSION = "URL_BAD_EXTENSION"
ERR_URL_EXPIRED = "URL_EXPIRED"

ERR_DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
ERR_DOWNLOAD_FORBIDDEN = "DOWNLOAD_FORBIDDEN"
ERR_DOWNLOAD_NOT_FOUND = "DOWNLOAD_NOT_FOUND"
ERR_FILE_TOO_LARGE = "FILE_TOO_LARGE"

ALLOWED_EXTENSIONS = ("xlsx", "xls", "csv")

# Ký tự được giữ lại trong phần gốc tên file tạm. Danh sách CHO PHÉP chứ không phải danh sách cấm:
# phần gốc này chỉ để log dễ đọc, định danh thật nằm ở chuỗi băm ngẫu nhiên ghép sau nó, nên siết
# chặt tới mức nào cũng không mất gì.
_UNSAFE_STEM_CHARS = re.compile(r"[^A-Za-z0-9_-]")

_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class RemoteSource:
    """URL đã qua kiểm tra, kèm những thứ suy ra được từ nó mà không phải hỏi nguồn."""

    url: str
    host: str
    # Path đã giải mã phần trăm. Dùng để log và để đặt tên file tạm, KHÔNG dùng để ghép đường dẫn.
    object_key: str
    extension: str
    # Thời điểm chữ ký hết hiệu lực; ``None`` khi URL không phải dạng ký sẵn kiểu SigV4.
    expires_at: datetime | None

    @property
    def safe_label(self) -> str:
        """Chuỗi được phép đưa vào log: có host và object key, KHÔNG có chữ ký."""
        return f"{self.host}/{self.object_key.lstrip('/')}"


def _allowed_hosts() -> set[str]:
    """Đọc allowlist từ cấu hình, chuẩn hóa để so khớp được cả khi khai kèm scheme hoặc cổng."""
    raw = (settings.EXCEL_DOWNLOAD_ALLOWED_HOSTS or '').split(',')
    hosts = set()
    for item in raw:
        item = item.strip().lower()
        if not item:
            continue
        # Khai cả scheme cũng chấp nhận, vì biến môi trường của MinIO thường ở dạng URL đầy đủ.
        if '://' in item:
            item = urlparse(item).netloc
        hosts.add(item.rstrip('/'))
    return hosts


def _parse_sigv4_expiry(query: dict[str, list[str]]) -> datetime | None:
    """Suy thời điểm hết hạn từ ``X-Amz-Date`` và ``X-Amz-Expires`` trong query string.

    Đây là phép tính thuần chuỗi, không tốn lời gọi mạng nào, nên chặn được URL đã chết ngay ở pha
    đồng bộ. Thiếu tham số hoặc sai định dạng thì trả ``None`` — coi như "không biết hạn" chứ không
    coi là lỗi: hệ ngoài có thể dùng cách ký khác, hoặc object để công khai.
    """
    lowered = {k.lower(): v for k, v in query.items()}
    signed_at_raw = (lowered.get('x-amz-date') or [None])[0]
    ttl_raw = (lowered.get('x-amz-expires') or [None])[0]
    if not signed_at_raw or not ttl_raw:
        return None
    try:
        signed_at = datetime.strptime(signed_at_raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return signed_at + timedelta(seconds=int(ttl_raw))
    except (ValueError, TypeError):
        return None


def validate_source_url(url: str) -> RemoteSource:
    """Kiểm URL nguồn bằng những gì suy được từ chính chuỗi URL, không chạm mạng.

    Gọi ở pha đồng bộ để mọi thứ hệ ngoài có thể gõ sai đều hỏng ngay trong response thay vì lùi
    xuống callback. Ném ``ExcelImportError``; người gọi dịch thành HTTP 400.

    Đuôi file lấy từ path đã cắt query — presigned URL luôn kết thúc bằng chuỗi ký, nên kiểm đuôi
    trên URL thô thì mọi file hợp lệ đều trượt.
    """
    if not url or not url.strip():
        raise ExcelImportError(ERR_URL_INVALID, "fileUrl is required")

    url = url.strip()
    try:
        parsed = urlparse(url)
    except ValueError as e:
        raise ExcelImportError(ERR_URL_INVALID, f"Cannot parse fileUrl: {e}")

    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ExcelImportError(ERR_URL_INVALID, "fileUrl must be an http(s) URL")

    allowed = _allowed_hosts()
    if not allowed:
        # Hỏng theo hướng đóng: chưa cấu hình allowlist thì tính năng coi như chưa mở, chứ không
        # phải mở cho mọi host.
        raise ExcelImportError(
            ERR_URL_HOST_NOT_ALLOWED,
            "File download is not configured (EXCEL_DOWNLOAD_ALLOWED_HOSTS is empty)"
        )

    host = parsed.hostname.lower()
    netloc = parsed.netloc.lower()
    if host not in allowed and netloc not in allowed:
        raise ExcelImportError(ERR_URL_HOST_NOT_ALLOWED, f"Host not allowed: {netloc}")

    object_key = unquote(parsed.path)
    extension = object_key.rsplit('.', 1)[-1].lower() if '.' in object_key else ''
    if extension not in ALLOWED_EXTENSIONS:
        raise ExcelImportError(
            ERR_URL_BAD_EXTENSION,
            f"Only support .{'/.'.join(ALLOWED_EXTENSIONS)}, got '{os.path.basename(object_key)}'"
        )

    expires_at = _parse_sigv4_expiry(parse_qs(parsed.query))
    if expires_at is not None:
        remaining = (expires_at - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            raise ExcelImportError(ERR_URL_EXPIRED, "fileUrl has already expired")
        if remaining < settings.EXCEL_DOWNLOAD_MIN_TTL_SECONDS:
            raise ExcelImportError(
                ERR_URL_EXPIRED,
                f"fileUrl expires in {int(remaining)}s, needs at least "
                f"{settings.EXCEL_DOWNLOAD_MIN_TTL_SECONDS}s"
            )

    return RemoteSource(url=url, host=netloc, object_key=object_key,
                        extension=extension, expires_at=expires_at)


def safe_stem(object_key: str, max_len: int = 40) -> str:
    """Rút phần gốc tên file tạm từ object key, đã khử mọi ký tự có thể lái đường dẫn.

    ``object_key`` là chuỗi do hệ ngoài kiểm soát và nó sắp được ghép vào một đường dẫn trên đĩa.
    Phải giải mã phần trăm TRƯỚC khi cắt theo dấu gạch chéo (``validate_source_url`` đã làm), nếu
    không ``..%2F`` sống sót qua phép cắt.
    """
    base = object_key.replace('\\', '/').split('/')[-1]
    base = base.rsplit('.', 1)[0]
    base = _UNSAFE_STEM_CHARS.sub('_', base).strip('_')
    return base[:max_len] or 'remote'


def max_bytes() -> int:
    """Trần dung lượng tính theo byte, đọc từ cấu hình mỗi lần gọi để test đổi được bằng monkeypatch."""
    return max(1, settings.EXCEL_DOWNLOAD_MAX_MB) * 1024 * 1024


def _definitive_error(status: int, label: str) -> ExcelImportError | None:
    """Dịch HTTP status thành lỗi DỨT KHOÁT, hoặc ``None`` nếu câu trả lời còn mơ hồ.

    Ranh giới này quyết định hai chuyện cùng lúc: pha đồng bộ có được phép từ chối request hay
    không, và worker có được phép tải lại hay không. 4xx là câu trả lời dứt khoát của nguồn — chữ
    ký sai thì thử lại bao nhiêu lần cũng sai, chỉ đốt thời gian và đẩy job tới gần hạn ký hơn. 5xx
    và lỗi mạng thì ngược lại, hoàn toàn có thể tự khỏi.
    """
    if status == 403:
        return ExcelImportError(
            ERR_DOWNLOAD_FORBIDDEN,
            f"Access denied by object storage (expired or invalid signature): {label}"
        )
    if status == 404:
        return ExcelImportError(ERR_DOWNLOAD_NOT_FOUND, f"Object not found: {label}")
    if status == 416:
        return ExcelImportError(ERR_DOWNLOAD_FAILED, f"Object is empty: {label}")
    if 400 <= status < 500:
        return ExcelImportError(ERR_DOWNLOAD_FAILED, f"Object storage returned {status}: {label}")
    return None


def _parse_total_size(content_range: str | None) -> int | None:
    """Rút tổng dung lượng từ header ``Content-Range`` dạng ``bytes 0-0/12345``."""
    if not content_range or '/' not in content_range:
        return None
    total = content_range.rsplit('/', 1)[-1].strip()
    return int(total) if total.isdigit() else None


def probe_source(src: RemoteSource) -> int | None:
    """Hỏi nguồn một byte để biết sớm chữ ký, sự tồn tại và dung lượng thật. Trả về cỡ file hoặc ``None``.

    Dùng ``GET`` kèm header ``Range`` chứ KHÔNG dùng ``HEAD``: chữ ký của presigned URL bao gồm cả
    HTTP method, nên URL ký cho GET mà gọi bằng HEAD sẽ bị từ chối chữ ký — một 403 hoàn toàn giả.

    Quy tắc giữ cho phép thăm dò này không kéo rủi ro timeout trở lại pha đồng bộ: **chỉ câu trả
    lời dứt khoát mới được chặn**. Nguồn nói 403/404 hay khai dung lượng vượt trần thì ném lỗi;
    timeout, đứt kết nối, 5xx thì trả ``None`` — cứ nhận job, để worker lo, vì đó đúng là loại tình
    huống mà cơ chế bất đồng bộ sinh ra để chịu đựng.

    Trả ``None`` cũng có nghĩa "không biết", không có nghĩa "an toàn": trần dung lượng vẫn phải ép
    lại lần nữa trong lúc tải.
    """
    timeout = settings.EXCEL_DOWNLOAD_PROBE_TIMEOUT
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            # Phải là ``stream``: nếu nguồn phớt lờ Range và trả 200, bản ``get`` sẽ nuốt trọn cả
            # file vào RAM ngay tại pha đồng bộ — đúng thứ mà cả thiết kế này tránh.
            with client.stream("GET", src.url, headers={"Range": "bytes=0-0"}) as resp:
                err = _definitive_error(resp.status_code, src.safe_label)
                if err is not None:
                    raise err
                if resp.status_code >= 500:
                    return None
                if resp.status_code == 206:
                    size = _parse_total_size(resp.headers.get("Content-Range"))
                else:
                    # 200: nguồn không hỗ trợ Range, chỉ còn lời khai Content-Length.
                    raw = resp.headers.get("Content-Length")
                    size = int(raw) if raw and raw.isdigit() else None
    except ExcelImportError:
        raise
    except Exception as e:
        SQLBotLogUtil.warning(f"[remote] probe inconclusive for {src.safe_label}: {e}")
        return None

    if size is not None and size > max_bytes():
        raise ExcelImportError(
            ERR_FILE_TOO_LARGE,
            f"File is {size} bytes, limit is {max_bytes()} bytes"
        )
    return size


def _stream_once(src: RemoteSource, part_path: str, limit: int) -> int:
    """Một lượt tải trọn vẹn vào file ``.part``. Ném ``_Retryable`` cho những hỏng hóc có thể tự khỏi."""
    deadline = time.monotonic() + settings.EXCEL_DOWNLOAD_TOTAL_TIMEOUT
    timeout = httpx.Timeout(
        connect=settings.EXCEL_DOWNLOAD_CONNECT_TIMEOUT,
        read=settings.EXCEL_DOWNLOAD_READ_TIMEOUT,
        write=settings.EXCEL_DOWNLOAD_READ_TIMEOUT,
        pool=settings.EXCEL_DOWNLOAD_CONNECT_TIMEOUT,
    )
    written = 0
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        with client.stream("GET", src.url) as resp:
            err = _definitive_error(resp.status_code, src.safe_label)
            if err is not None:
                raise err
            if resp.status_code >= 300:
                raise _Retryable(f"object storage returned {resp.status_code}")

            declared = resp.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > limit:
                raise ExcelImportError(
                    ERR_FILE_TOO_LARGE, f"File is {declared} bytes, limit is {limit} bytes")

            with open(part_path, "wb") as f:
                for chunk in resp.iter_bytes(_CHUNK_SIZE):
                    written += len(chunk)
                    # Ép trần lần thứ hai trên byte THẬT: Content-Length chỉ là lời khai, và một
                    # response chunked có thể không khai gì cả.
                    if written > limit:
                        raise ExcelImportError(
                            ERR_FILE_TOO_LARGE, f"File exceeds limit of {limit} bytes")
                    if time.monotonic() > deadline:
                        # Timeout đọc chỉ bắt được kết nối đứng im; trần tổng này mới chặn được
                        # nguồn nhỏ giọt đều đặn, thứ giữ một worker vô hạn mà không lần đọc nào quá hạn.
                        raise ExcelImportError(
                            ERR_DOWNLOAD_FAILED,
                            f"Download exceeded {settings.EXCEL_DOWNLOAD_TOTAL_TIMEOUT}s"
                        )
                    f.write(chunk)
    return written


class _Retryable(Exception):
    """Hỏng hóc có thể tự khỏi (lỗi mạng, 5xx). Không bao giờ thoát ra khỏi module này."""


def download_to(src: RemoteSource, dest_path: str) -> int:
    """Tải file về ``dest_path``, trả về số byte đã ghi.

    Ghi vào ``<dest_path>.part`` rồi mới đổi tên. Đổi tên trong cùng hệ thống file là thao tác
    nguyên tử, nên tên chính thức chỉ xuất hiện khi file đã đầy đủ — không ai đọc phải file tải dở,
    kể cả vòng quét phục hồi chạy song song.

    Chỉ tải lại với lỗi mạng và 5xx. Mọi 4xx là câu trả lời dứt khoát của nguồn, thử lại vô nghĩa.
    """
    limit = max_bytes()
    part_path = f"{dest_path}.part"
    os.makedirs(os.path.dirname(dest_path) or '.', exist_ok=True)

    attempts = max(0, settings.EXCEL_DOWNLOAD_RETRIES) + 1
    last_error = ''
    for attempt in range(attempts):
        try:
            size = _stream_once(src, part_path, limit)
            os.replace(part_path, dest_path)
            SQLBotLogUtil.info(f"[remote] downloaded {size} bytes from {src.safe_label}")
            return size
        except ExcelImportError:
            _discard(part_path)
            raise
        except (_Retryable, httpx.HTTPError, OSError) as e:
            _discard(part_path)
            last_error = f"{type(e).__name__}: {e}"
            SQLBotLogUtil.warning(
                f"[remote] download attempt {attempt + 1}/{attempts} failed for "
                f"{src.safe_label}: {last_error}"
            )
            if attempt + 1 < attempts:
                time.sleep(min(2 ** attempt, 10))

    raise ExcelImportError(
        ERR_DOWNLOAD_FAILED,
        f"Cannot download after {attempts} attempts: {last_error}"
    )


def _discard(part_path: str) -> None:
    """Xóa file tải dở. Im lặng bỏ qua khi không xóa được — đây là dọn dẹp, không phải điều kiện đúng đắn."""
    try:
        if os.path.exists(part_path):
            os.remove(part_path)
    except Exception as e:
        SQLBotLogUtil.warning(f"[remote] cannot remove partial file {part_path}: {e}")
