"""Thao tác trên bảng ``excel_import_jobs``.

Tách khỏi ``crud/datasource.py`` vì đây là tầng worker dùng, mà worker chạy ngoài request: các hàm
ở đây nhận ``Session`` trần chứ không nhận ``SessionDep``, không đụng ``CurrentUser``, không i18n.

Mỗi hàm tự quản transaction của mình theo lối "transaction ngắn": mở, ghi, commit, đóng. Worker
chạy hàng phút nên tuyệt đối không được giữ một transaction mở suốt job — đó là giữ một connection
Postgres vô ích và làm mọi thay đổi trạng thái ở giữa không ai đọc được.
"""

import datetime
import os
import socket
from collections.abc import Callable

from sqlalchemy import and_, or_, update
from sqlmodel import Session, select

from common.core.db import engine

from ..models.excel_job import CallbackStatus, ExcelImportJob, ExcelImportJobStatus


def _now() -> datetime.datetime:
    """Mốc thời gian CÓ múi giờ.

    Bắt buộc dùng bản aware: các cột thời gian khai ``TIMESTAMP(timezone=True)``, trộn giá trị naive
    vào sẽ khiến mọi phép so sánh ngưỡng (heartbeat quá hạn, tới hạn thử lại) lệch đúng bằng
    chênh lệch múi giờ mà không có lỗi nào báo.
    """
    return datetime.datetime.now(datetime.timezone.utc)


def _heartbeat_expired(threshold: datetime.datetime):
    """Điều kiện "job này đã tắt thở", có tính tới trường hợp chưa từng có heartbeat.

    Nhánh ``is_(None)`` bắt buộc: ``NULL < threshold`` cho UNKNOWN chứ không phải TRUE, nên một
    dòng ``running`` thiếu mốc heartbeat sẽ không bao giờ khớp phép so sánh và không ai dọn được —
    trong khi đó chính là dòng đáng ngờ nhất. Đường đi bình thường luôn ghi heartbeat cùng lúc với
    ``status = running``, nên nhánh này chỉ để phòng dữ liệu bất thường.
    """
    return or_(ExcelImportJob.heartbeat_at.is_(None), ExcelImportJob.heartbeat_at < threshold)


def current_worker_id() -> str:
    """Định danh tiến trình đang chạy, dạng ``hostname:pid``.

    Cần để lúc shutdown một tiến trình chỉ đánh dấu đúng job của chính nó, và để đọc log biết job
    chạy ở tiến trình nào khi triển khai nhiều worker uvicorn.
    """
    return f"{socket.gethostname()}:{os.getpid()}"


def create_job(session: Session, *, ds_id: int, oid: int, create_by: int, ds_name: str,
               file_path: str, sheet_names: list[str],
               file_url: str | None = None) -> ExcelImportJob:
    """Ghi dòng job ở trạng thái chờ. KHÔNG commit — người gọi quyết định thời điểm.

    Pha đồng bộ cần gộp dòng này chung transaction với dòng ``core_datasource``, để hai thứ hoặc
    cùng tồn tại hoặc cùng không.

    ``file_url`` để rỗng nghĩa là file đã nằm sẵn ở ``file_path`` (đường multipart cũ); có giá trị
    nghĩa là worker phải tự tải về đúng ``file_path`` trước khi làm gì khác.
    """
    job = ExcelImportJob(
        ds_id=ds_id,
        oid=oid,
        create_by=create_by,
        ds_name=ds_name,
        file_path=file_path,
        file_url=file_url,
        sheet_names=list(sheet_names),
        created_tables=[],
        status=ExcelImportJobStatus.PENDING,
        callback_status=CallbackStatus.NONE,
    )
    session.add(job)
    session.flush()
    session.refresh(job)
    return job


def get_job_by_ds_id(session: Session, ds_id: int) -> ExcelImportJob | None:
    """Tra job theo ``ds_id`` — khóa đối chiếu duy nhất giữa job, nguồn dữ liệu và hệ ngoài."""
    return session.exec(select(ExcelImportJob).where(ExcelImportJob.ds_id == ds_id)).first()


def mark_running(job_id: int, worker_id: str) -> None:
    """Đánh dấu worker đã nhận job và bắt đầu chạy."""
    now = _now()
    with Session(engine) as session:
        session.execute(
            update(ExcelImportJob)
            .where(ExcelImportJob.id == job_id)
            .values(status=ExcelImportJobStatus.RUNNING, worker_id=worker_id,
                    started_at=now, heartbeat_at=now)
        )
        session.commit()


def touch_heartbeat(job_id: int) -> None:
    """Cập nhật mốc còn sống của job.

    Đây là thứ phân biệt "job chết" với "job chạy lâu": ngưỡng phát hiện chết đặt theo vài chu kỳ
    heartbeat, hoàn toàn độc lập với việc job import 30 giây hay 30 phút.
    """
    with Session(engine) as session:
        session.execute(
            update(ExcelImportJob)
            .where(ExcelImportJob.id == job_id)
            .values(heartbeat_at=_now())
        )
        session.commit()


def append_created_table(job_id: int, table_name: str) -> None:
    """Ghi thêm một tên bảng vừa tạo, COMMIT NGAY.

    Không gom lại ghi một lần ở cuối job: tên bảng mang 10 ký tự hash ngẫu nhiên nên không tái tạo
    được, và ``delete_ds`` thì đọc ``configuration.sheets`` — cột chỉ có nội dung ở cuối worker.
    Worker chết sau khi tạo 2/3 bảng mà chưa ghi thì hai bảng kia thành mồ côi vĩnh viễn.

    Đọc–sửa–ghi trên cột JSONB: chấp nhận được vì mỗi job chỉ có đúng một worker ghi vào dòng của
    chính nó, không có ai tranh chấp.
    """
    with Session(engine) as session:
        job = session.get(ExcelImportJob, job_id)
        if job is None:
            return
        job.created_tables = list(job.created_tables or []) + [table_name]
        job.heartbeat_at = _now()
        session.add(job)
        session.commit()


def finish_job(job_id: int, *, success: bool, error_code: str | None = None,
               error_message: str | None = None, callback_payload: dict | None = None) -> None:
    """Kết thúc job VÀ đưa lá thư callback vào outbox, trong CÙNG một transaction.

    Tính nguyên tử ở đây là điểm chính của cả thiết kế: nếu commit trạng thái trước rồi mới gọi
    HTTP, hai thứ có thể lệch nhau — DB nói xong mà hệ ngoài không biết gì, hoặc ngược lại. Ghi
    chung một transaction thì "job đã xong" và "còn nợ một callback" luôn cùng đúng hoặc cùng sai.

    ``next_attempt_at`` đặt bằng hiện tại để vòng gửi nhặt ở chu kỳ kế tiếp. Bản thân worker không
    bao giờ chạm tới mạng.
    """
    now = _now()
    with Session(engine) as session:
        session.execute(
            update(ExcelImportJob)
            .where(ExcelImportJob.id == job_id)
            .values(
                status=ExcelImportJobStatus.SUCCESS if success else ExcelImportJobStatus.FAILED,
                finished_at=now,
                heartbeat_at=now,
                error_code=error_code,
                error_message=error_message,
                callback_status=CallbackStatus.PENDING,
                callback_payload=callback_payload,
                next_attempt_at=now,
            )
        )
        session.commit()


def claim_callback_batch(limit: int) -> list[dict]:
    """Nhận một lô thư callback tới hạn, đánh dấu ``sending``, trả về bản sao thuần dữ liệu.

    Đây là pha 1 của vòng gửi ba pha. Hai điểm quyết định tính đúng đắn:

    - ``FOR UPDATE SKIP LOCKED``: nhiều tiến trình cùng chạy vòng gửi thì mỗi dòng chỉ về tay một
      tiến trình, và tiến trình đến sau BỎ QUA dòng đã bị giữ thay vì xếp hàng chờ. Không có
      ``SKIP LOCKED`` thì mọi tiến trình tuần tự hóa trên cùng một lô và việc chạy nhiều tiến trình
      trở nên vô nghĩa.
    - Trả về dict chứ không trả về đối tượng ORM: session đóng ngay khi hàm kết thúc, đối tượng ORM
      mang ra ngoài sẽ bị detach và chạm vào thuộc tính là nổ. Lời gọi HTTP nằm ngoài transaction
      nên không được giữ bất cứ thứ gì gắn với session.
    """
    now = _now()
    claimed: list[dict] = []
    with Session(engine) as session:
        jobs = session.exec(
            select(ExcelImportJob)
            .where(and_(
                ExcelImportJob.callback_status == CallbackStatus.PENDING,
                or_(ExcelImportJob.next_attempt_at.is_(None), ExcelImportJob.next_attempt_at <= now),
            ))
            .order_by(ExcelImportJob.next_attempt_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for job in jobs:
            job.callback_status = CallbackStatus.SENDING
            job.callback_claimed_at = now
            session.add(job)
            claimed.append({
                "id": job.id,
                "ds_id": job.ds_id,
                "payload": job.callback_payload,
                "attempts": job.callback_attempts,
            })
        session.commit()
    return claimed


def record_callback_result(job_id: int, *, success: bool, error: str | None,
                           max_attempts: int, delay_for: Callable[[int], int]) -> str:
    """Pha 3 của vòng gửi: ghi kết quả một lần gửi, trả về trạng thái mới.

    ``callback_attempts`` tăng ở CẢ nhánh thành công: nó đếm số lần gọi HTTP đã thực hiện, không
    phải số lần hỏng — đọc log mà thấy ``sent`` sau 3 lần thử là thông tin có giá trị.

    Hết số lần cho phép thì chuyển ``dead`` chứ không thử mãi: một URL sai cấu hình sẽ đốt chu kỳ
    vô hạn, và ``dead`` là thứ câu truy vấn giám sát đếm được để người vận hành biết mà xử lý.
    """
    with Session(engine) as session:
        job = session.get(ExcelImportJob, job_id)
        if job is None:
            return CallbackStatus.NONE
        job.callback_attempts = (job.callback_attempts or 0) + 1
        if success:
            job.callback_status = CallbackStatus.SENT
            job.callback_last_error = None
            job.next_attempt_at = None
        elif job.callback_attempts >= max_attempts:
            job.callback_status = CallbackStatus.DEAD
            job.callback_last_error = error
            job.next_attempt_at = None
        else:
            job.callback_status = CallbackStatus.PENDING
            job.callback_last_error = error
            job.next_attempt_at = _now() + datetime.timedelta(seconds=delay_for(job.callback_attempts))
        session.add(job)
        session.commit()
        return job.callback_status


def requeue_stale_callbacks(stale_seconds: int) -> int:
    """Đưa các thư kẹt ở ``sending`` quá lâu về lại ``pending``, trả về số dòng đã gỡ.

    Trạng thái ``sending`` là cái giá phải trả cho việc gọi HTTP ngoài transaction: tiến trình chết
    giữa lúc chờ mạng thì dòng nằm lại vĩnh viễn, không ai nhặt vì vòng gửi chỉ tìm ``pending``.

    Ngưỡng phải LỚN HƠN HẲN timeout HTTP, nếu không một lần gửi chậm bình thường sẽ bị gỡ ra và
    gửi lần hai trong khi lần một vẫn đang chạy.

    Nhánh ``is_(None)`` không thừa: trong SQL ``NULL < x`` cho UNKNOWN chứ không phải TRUE, nên một
    dòng ``sending`` mà thiếu mốc nhận việc sẽ KHÔNG khớp điều kiện so sánh và nằm kẹt vĩnh viễn —
    đúng loại dòng cần cứu nhất lại là loại lọt lưới. Đường đi bình thường luôn ghi mốc này, nhưng
    hàm cứu hộ thì không được phép giả định dữ liệu sạch.
    """
    threshold = _now() - datetime.timedelta(seconds=stale_seconds)
    with Session(engine) as session:
        result = session.execute(
            update(ExcelImportJob)
            .where(and_(
                ExcelImportJob.callback_status == CallbackStatus.SENDING,
                or_(ExcelImportJob.callback_claimed_at.is_(None),
                    ExcelImportJob.callback_claimed_at < threshold),
            ))
            .values(callback_status=CallbackStatus.PENDING, next_attempt_at=_now())
        )
        session.commit()
        return result.rowcount or 0


def resend_callback(job_id: int) -> bool:
    """Bắn lại thủ công một callback đã ``dead``: đưa về ``pending``, tới hạn ngay.

    Không đụng ``callback_attempts``: người vận hành cần thấy nó đã hỏng bao nhiêu lần trước đó.
    """
    with Session(engine) as session:
        job = session.get(ExcelImportJob, job_id)
        if job is None or job.callback_payload is None:
            return False
        job.callback_status = CallbackStatus.PENDING
        job.next_attempt_at = _now()
        session.add(job)
        session.commit()
        return True


def claim_orphan_jobs(older_than_seconds: int, limit: int = 20) -> list[int]:
    """Nhận lại các job còn ``pending`` quá lâu mà chưa worker nào đụng tới.

    Vì sao tình huống này có thật: pha đồng bộ commit dòng job RỒI mới ``submit``. Tiến trình chết
    đúng giữa hai bước đó — deploy, OOM, kill -9 — thì dòng job nằm lại trong DB mà không có ai
    trong RAM biết đến nó. Không có vòng nhặt này thì nguồn dữ liệu kẹt ``Importing`` vĩnh viễn và
    hệ ngoài chờ một callback không bao giờ tới.

    Nhận việc bằng một câu UPDATE có điều kiện ``status = 'pending'`` rồi soi ``rowcount``: đó là
    phép so-sánh-rồi-đổi nguyên tử của SQL. Chỉ tiến trình thắng mới thấy rowcount 1, nên dù nhiều
    tiến trình cùng quét cũng chỉ một tiến trình chạy job.
    """
    threshold = _now() - datetime.timedelta(seconds=older_than_seconds)
    worker_id = current_worker_id()
    picked: list[int] = []
    with Session(engine) as session:
        candidates = session.exec(
            select(ExcelImportJob.id)
            .where(and_(
                ExcelImportJob.status == ExcelImportJobStatus.PENDING,
                ExcelImportJob.created_at < threshold,
            ))
            .limit(limit)
        ).all()
    for job_id in candidates:
        with Session(engine) as session:
            now = _now()
            result = session.execute(
                update(ExcelImportJob)
                .where(and_(
                    ExcelImportJob.id == job_id,
                    ExcelImportJob.status == ExcelImportJobStatus.PENDING,
                ))
                .values(status=ExcelImportJobStatus.RUNNING, worker_id=worker_id,
                        started_at=now, heartbeat_at=now)
            )
            session.commit()
            if result.rowcount:
                picked.append(job_id)
    return picked


def claim_dead_jobs(stale_seconds: int, limit: int = 20) -> list[dict]:
    """Nhận quyền dọn xác các job ``running`` đã tắt thở, trả về đủ thông tin để dọn.

    "Tắt thở" = heartbeat không nhích trong ``stale_seconds``. Ngưỡng đặt theo heartbeat chứ không
    theo tổng thời gian chạy: một file lớn có thể import 30 phút mà vẫn hoàn toàn khỏe mạnh.

    Cũng nhận việc bằng UPDATE có điều kiện, và điều kiện phải LẶP LẠI mốc heartbeat — nếu chỉ điều
    kiện theo ``status`` thì worker thật vừa hồi tỉnh và cập nhật heartbeat xong vẫn bị người dọn
    cướp job ngay dưới chân.
    """
    threshold = _now() - datetime.timedelta(seconds=stale_seconds)
    worker_id = current_worker_id()
    victims: list[dict] = []
    with Session(engine) as session:
        candidates = session.exec(
            select(ExcelImportJob)
            .where(and_(
                ExcelImportJob.status == ExcelImportJobStatus.RUNNING,
                _heartbeat_expired(threshold),
            ))
            .limit(limit)
        ).all()
        rows = [{"id": j.id, "ds_id": j.ds_id, "created_tables": list(j.created_tables or []),
                 "file_path": j.file_path, "worker_id": j.worker_id} for j in candidates]
    for row in rows:
        with Session(engine) as session:
            result = session.execute(
                update(ExcelImportJob)
                .where(and_(
                    ExcelImportJob.id == row["id"],
                    ExcelImportJob.status == ExcelImportJobStatus.RUNNING,
                    _heartbeat_expired(threshold),
                ))
                .values(worker_id=worker_id, heartbeat_at=_now())
            )
            session.commit()
            if result.rowcount:
                victims.append(row)
    return victims


def active_job_files() -> set:
    """Tập đường dẫn file đang được một job CHƯA XONG dùng tới.

    Việc dọn file tạm phải trừ tập này ra: một job import file lớn có thể chạy lâu hơn mọi ngưỡng
    tuổi file hợp lý, xóa file dưới chân nó là làm hỏng đúng job đang khỏe mạnh.
    """
    with Session(engine) as session:
        rows = session.exec(
            select(ExcelImportJob.file_path)
            .where(ExcelImportJob.status.in_(ExcelImportJobStatus.ACTIVE))
        ).all()
    return {r for r in rows if r}
