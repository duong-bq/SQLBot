"""Vòng quét phục hồi của luồng import excel: nhặt job mồ côi, kết liễu job chết, dọn rác.

Lý do tồn tại: mọi bảo đảm của luồng bất đồng bộ đều dừng lại ở ranh giới "tiến trình còn sống".
Ba cách hỏng dưới đây không cái nào tự khỏi, và cả ba đều kết thúc bằng cùng một hậu quả — nguồn
dữ liệu kẹt ``Importing`` vĩnh viễn và hệ ngoài chờ một callback không bao giờ tới:

- Tiến trình chết giữa lúc job đang chạy → dòng ``running`` không ai nhích heartbeat nữa.
- Tiến trình chết ngay sau khi commit dòng job nhưng trước khi ``submit`` → dòng ``pending`` không
  ai trong RAM biết tới.
- Tiến trình chết trong lúc chờ phản hồi HTTP của callback → dòng ``sending`` không ai gửi tiếp.

Vòng này chạy ở MỌI tiến trình worker. Chạy trùng nhau vô hại vì mọi thao tác nhận việc đều là
UPDATE có điều kiện rồi soi ``rowcount``, tức là so-sánh-rồi-đổi nguyên tử ở phía DB.
"""

import os
import time

from common.core.config import settings
from common.utils.periodic import PeriodicThread
from common.utils.utils import SQLBotLogUtil

from ..crud.excel_job import (
    active_job_files,
    claim_dead_jobs,
    claim_orphan_jobs,
    requeue_stale_callbacks,
)
from .excel_import_task import ERR_JOB_STALE, fail_import_job, submit_import_job

_loop: PeriodicThread | None = None


def _recover_orphan_jobs() -> int:
    """Nhặt lại các job chưa ai chạy và đẩy vào pool của tiến trình này."""
    job_ids = claim_orphan_jobs(settings.EXCEL_IMPORT_STALE_SECONDS)
    for job_id in job_ids:
        SQLBotLogUtil.warning(f"excel import job {job_id} was orphaned, re-submitting")
        submit_import_job(job_id)
    return len(job_ids)


def _bury_dead_jobs() -> int:
    """Kết liễu các job đã tắt thở: dọn bảng đã tạo, đánh dấu thất bại, đẩy callback báo hỏng.

    Dùng lại đúng đường thất bại của worker (``fail_import_job``) chứ không viết riêng: hệ ngoài
    không cần biết job hỏng vì file sai hay vì tiến trình bị kill, nó chỉ cần một callback báo
    ``status=false`` như mọi lần hỏng khác.

    Xóa file tạm ở đây vì worker cũ đã chết nên khối ``finally`` của nó không bao giờ chạy.
    """
    victims = claim_dead_jobs(settings.EXCEL_IMPORT_STALE_SECONDS)
    for job in victims:
        SQLBotLogUtil.error(
            f"excel import job {job['id']} abandoned by worker {job['worker_id']}, cleaning up"
        )
        fail_import_job(job["id"], job["ds_id"], job["created_tables"], ERR_JOB_STALE,
                        "Import worker died before finishing")
        # Cả file ``.part``: worker chết giữa lúc tải để lại một file tải dở mang tên đó.
        for leftover in (job["file_path"], f"{job['file_path']}.part") if job["file_path"] else ():
            try:
                if os.path.exists(leftover):
                    os.remove(leftover)
            except Exception as e:
                SQLBotLogUtil.warning(f"cannot remove temp file of dead job {job['id']}: {e}")
    return len(victims)


def _recover_stale_callbacks() -> int:
    """Gỡ các callback kẹt ở ``sending``.

    Ngưỡng lấy gấp ba lần timeout HTTP: phải chắc chắn lớn hơn thời gian một lần gửi bình thường,
    nếu không ta sẽ gỡ ra và gửi lần hai trong khi lần một còn đang chờ phản hồi.
    """
    stale_seconds = max(settings.AI_CALLBACK_TIMEOUT * 3, 60)
    count = requeue_stale_callbacks(stale_seconds)
    if count:
        SQLBotLogUtil.warning(f"requeued {count} stuck excel callbacks")
    return count


def _cleanup_temp_files() -> int:
    """Xóa file tạm quá hạn trong thư mục upload, trừ file của các job chưa xong.

    Không dựa vào tuổi file một mình: một job import file lớn có thể chạy lâu hơn bất kỳ ngưỡng
    tuổi hợp lý nào, xóa file dưới chân nó là làm hỏng đúng job đang khỏe mạnh.

    Thư mục này dùng chung với luồng ``/parseExcel`` → ``/importToDb`` của giao diện web, nơi file
    nằm chờ người dùng bấm nút và không có dòng job nào bảo vệ. Đó là lý do TTL đặt dài (mặc định
    24 giờ) chứ không tính bằng phút.
    """
    folder = settings.EXCEL_PATH
    if not folder or not os.path.isdir(folder):
        return 0

    ttl_seconds = settings.EXCEL_IMPORT_TEMP_TTL_HOURS * 3600
    deadline = time.time() - ttl_seconds
    protected = active_job_files()
    removed = 0
    for name in os.listdir(folder):
        full_path = os.path.join(folder, name)
        try:
            if not os.path.isfile(full_path) or full_path in protected:
                continue
            if os.path.getmtime(full_path) >= deadline:
                continue
            os.remove(full_path)
            removed += 1
        except Exception as e:
            SQLBotLogUtil.warning(f"cannot remove stale temp file {full_path}: {e}")
    if removed:
        SQLBotLogUtil.info(f"removed {removed} stale excel temp files")
    return removed


def recovery_tick() -> dict:
    """Một vòng quét đầy đủ. Trả về số lượng từng loại đã xử lý (để test và để log).

    Bốn việc gọi tách nhau và mỗi việc tự bắt lỗi: một việc hỏng — chẳng hạn thư mục upload chưa
    tồn tại — không được phép chặn ba việc còn lại, vì ba việc kia mới là phần cứu được job kẹt.
    """
    result = {"orphans": 0, "dead": 0, "callbacks": 0, "files": 0}
    for key, fn in (("orphans", _recover_orphan_jobs), ("dead", _bury_dead_jobs),
                    ("callbacks", _recover_stale_callbacks), ("files", _cleanup_temp_files)):
        try:
            result[key] = fn()
        except Exception as e:
            SQLBotLogUtil.exception(f"excel recovery step {key} failed: {e}")
    return result


def start_excel_recovery() -> None:
    """Bật vòng quét.

    Có ``initial_delay``: ngay sau khi tiến trình khởi động, các job của CHÍNH lần chạy trước còn
    chưa kịp được nhận lại, và ta cũng chưa muốn quét ngay giữa lúc ứng dụng còn đang dựng. Chờ một
    chu kỳ rồi hãy bắt đầu.
    """
    global _loop
    if _loop is not None:
        return
    _loop = PeriodicThread("excel-recovery", settings.EXCEL_IMPORT_RECOVERY_SECONDS, recovery_tick,
                           initial_delay=settings.EXCEL_IMPORT_RECOVERY_SECONDS)
    _loop.start()


def stop_excel_recovery() -> None:
    """Dừng vòng quét lúc ứng dụng tắt."""
    global _loop
    if _loop is not None:
        _loop.stop()
        _loop = None
