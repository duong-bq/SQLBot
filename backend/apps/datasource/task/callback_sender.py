"""Vòng gửi callback báo kết quả import về hệ ngoài — phần "người đưa thư" của outbox.

Vì sao tách khỏi worker import: worker đang giữ dataframe trong RAM và một chỗ trong pool import;
để nó ngồi chờ mạng là trả giá bằng đúng tài nguyên đắt nhất. Quan trọng hơn, việc gửi phải sống
sót qua cả những cái chết của worker — job xong rồi tiến trình tắt trước khi kịp gọi HTTP thì lá
thư vẫn nằm trong DB và một tiến trình khác gửi hộ. Gửi ngay trong worker thì mất trắng.

Ba pha, cố ý không gộp:

1. Nhận việc trong một transaction NGẮN (``claim_callback_batch``).
2. Gọi HTTP hoàn toàn NGOÀI transaction — giữ transaction mở suốt thời gian chờ mạng là giữ khóa
   dòng và một connection Postgres trong nhiều giây.
3. Ghi kết quả trong một transaction ngắn khác.

Bảo đảm đạt được là "gửi ít nhất một lần", không bao giờ là "đúng một lần": ta không thể phân biệt
"đối tác chưa nhận" với "đối tác nhận rồi nhưng phản hồi rớt trên đường về". Hệ ngoài vì thế phải
xử lý được cùng một ``externalId`` tới hai lần.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from common.core.config import settings
from common.utils.periodic import PeriodicThread
from common.utils.utils import SQLBotLogUtil

from ..crud.excel_job import claim_callback_batch, record_callback_result

_loop: PeriodicThread | None = None


def _headers() -> dict:
    """Header gửi kèm, có thêm header xác thực nếu cấu hình khai dạng ``Tên: giá trị``."""
    headers = {"Content-Type": "application/json"}
    raw = (settings.AI_CALLBACK_AUTH_HEADER or '').strip()
    if raw and ':' in raw:
        key, value = raw.split(':', 1)
        headers[key.strip()] = value.strip()
    return headers


def _backoff_seconds(attempts: int) -> int:
    """Giãn cách trước lần thử kế tiếp: nhân đôi sau mỗi lần hỏng, có chặn trên.

    Chặn trên là bắt buộc. Không có nó, sau chục lần hỏng khoảng chờ nhảy lên hàng ngày và một sự
    cố mạng ngắn của đối tác biến thành callback không bao giờ tới nữa dù mọi thứ đã bình thường.
    """
    delay = settings.AI_CALLBACK_BACKOFF_BASE_SECONDS * (2 ** max(0, attempts - 1))
    return int(min(delay, settings.AI_CALLBACK_BACKOFF_CAP_SECONDS))


def send_callback(payload: dict) -> tuple[bool, str | None]:
    """Gọi HTTP một lần, trả về ``(thành công, mô tả lỗi)``. KHÔNG bao giờ ném ra ngoài.

    Bọc payload nghiệp vụ trong phong bì ``{actionType, payload}`` theo đặc tả của hệ ngoài.

    ``timeout`` là tham số bắt buộc chứ không phải tùy chọn: ``requests`` mặc định chờ vô hạn, và
    một connection treo sẽ giữ thread gửi mãi mãi — đúng cái mà kiến trúc outbox sinh ra để tránh.

    Chỉ mã 2xx tính là thành công. 4xx cũng bị coi là hỏng và vẫn thử lại: phân biệt "lỗi của ta,
    thử lại vô ích" với "đối tác đang trục trặc" đòi hỏi hiểu mã lỗi riêng của họ, mà đặc tả chưa
    nói gì; thử lại thừa vài lần rẻ hơn là bỏ rơi một callback đúng.
    """
    url = (settings.AI_CALLBACK_URL or '').strip()
    if not url:
        return False, "AI_CALLBACK_URL is not configured"

    body = {"actionType": settings.AI_CALLBACK_ACTION_TYPE, "payload": payload}
    try:
        response = requests.post(url, json=body, headers=_headers(),
                                 timeout=settings.AI_CALLBACK_TIMEOUT)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

    if 200 <= response.status_code < 300:
        return True, None
    return False, f"HTTP {response.status_code}: {response.text[:500]}"


def _send_one(item: dict) -> None:
    """Gửi một lá thư rồi ghi kết quả. Đây là đơn vị việc chạy trong pool gửi."""
    ok, error = send_callback(item["payload"] or {})
    status = record_callback_result(
        item["id"], success=ok, error=error,
        max_attempts=settings.AI_CALLBACK_MAX_ATTEMPTS, delay_for=_backoff_seconds,
    )
    if ok:
        SQLBotLogUtil.info(f"excel callback sent, job={item['id']} ds={item['ds_id']}")
    else:
        level = SQLBotLogUtil.error if status == "dead" else SQLBotLogUtil.warning
        level(f"excel callback failed, job={item['id']} ds={item['ds_id']} "
              f"status={status} attempts={item['attempts'] + 1}: {error}")


def callback_tick() -> int:
    """Một vòng: nhận lô thư tới hạn, gửi song song, trả về số lá đã xử lý.

    Cả lô gửi song song vì các lá thư độc lập nhau và phần lớn thời gian là chờ mạng. Không có bảo
    đảm thứ tự: hai kết quả import xong gần nhau có thể tới hệ ngoài theo thứ tự bất kỳ. Điều đó
    chấp nhận được vì mỗi lá thư nói về một ``externalId`` riêng.

    URL rỗng thì thoát ngay và KHÔNG đụng vào các dòng chờ: giữ nguyên ``pending`` để khi cấu hình
    xong là gửi được luôn, thay vì đốt hết số lần thử vào lúc hệ thống còn chưa khai báo địa chỉ.
    """
    if not (settings.AI_CALLBACK_URL or '').strip():
        return 0

    batch = claim_callback_batch(settings.AI_CALLBACK_BATCH_SIZE)
    if not batch:
        return 0

    with ThreadPoolExecutor(max_workers=settings.AI_CALLBACK_WORKERS,
                            thread_name_prefix="excel-callback") as pool:
        futures = [pool.submit(_send_one, item) for item in batch]
        for future in as_completed(futures):
            # Bắt buộc gọi result(): Future nuốt exception im lặng, mà nếu ghi kết quả hỏng thì lá
            # thư sẽ kẹt ở "sending" và chỉ vòng quét phục hồi mới gỡ ra được.
            try:
                future.result()
            except Exception as e:
                SQLBotLogUtil.exception(f"excel callback worker crashed: {e}")
    return len(batch)


def start_callback_sender() -> None:
    """Bật vòng gửi. Chạy ở MỌI tiến trình worker, không giới hạn một tiến trình.

    Khác các tác vụ khởi động dùng ``SingleWorkerGuard``: ở đây chạy nhiều tiến trình là có lợi —
    ``SKIP LOCKED`` bảo đảm không ai gửi trùng của ai, và mất một tiến trình không làm việc gửi
    ngừng lại.
    """
    global _loop
    if _loop is not None:
        return
    _loop = PeriodicThread("excel-callback-sender", settings.AI_CALLBACK_POLL_SECONDS, callback_tick)
    _loop.start()


def stop_callback_sender() -> None:
    """Dừng vòng gửi lúc ứng dụng tắt."""
    global _loop
    if _loop is not None:
        _loop.stop()
        _loop = None
