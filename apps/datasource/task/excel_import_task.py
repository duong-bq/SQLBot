"""Worker nạp file excel chạy nền, và pool riêng của nó.

Vì sao worker không gọi lại route handler: ``require_permissions`` đọc user hiện tại qua contextvar
và NÉM ``RuntimeError`` khi không có request, còn handler thì là ``async def``. Ngoài ra
``executor.submit`` không mang contextvar sang thread mới (khác ``asyncio.to_thread``). Nên toàn bộ
việc nặng phải nằm ở các hàm thuần trong ``utils/excel_import.py``, và worker chỉ ghép chúng lại.

Mọi thứ worker cần đều đọc từ dòng job trong DB, không có gì được truyền qua bộ nhớ: worker có thể
chạy ở lần khởi động khác của ứng dụng so với lúc request tạo job.
"""

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import update
from sqlmodel import Session

from common.core.config import settings
from common.core.db import engine
from common.utils.utils import SQLBotLogUtil

from ..crud.datasource import sync_table
from ..crud.excel_job import (
    append_created_table,
    current_worker_id,
    finish_job,
    mark_running,
    touch_heartbeat,
)
from ..models.datasource import (
    CoreDatasource,
    CoreTable,
    DatasourceStatus,
    FieldInfo,
    SheetFields,
)
from ..models.excel_job import ExcelImportJob
from ..utils.excel import list_sheet_names, parse_excel_preview
from ..utils.excel_import import ExcelImportError, drop_tables, import_sheets_to_db
from ..utils.remote_file import download_to, validate_source_url
from ..utils.utils import aes_encrypt

# Pool RIÊNG, cố ý nhỏ. Không dùng chung pool 200 thread của embedding: mỗi job import nạp cả
# dataframe vào RAM nên số thread phải nhỏ, trong khi embedding thì ngược lại. Gộp chung thì một
# lần import file lớn chiếm chỗ và làm chậm mọi thứ khác đi qua pool đó.
excel_import_executor = ThreadPoolExecutor(
    max_workers=settings.EXCEL_IMPORT_WORKERS, thread_name_prefix="excel-import"
)

ERR_JOB_MISSING = "JOB_NOT_FOUND"
ERR_FILE_MISSING = "FILE_NOT_FOUND"
ERR_NO_SHEET = "NO_SHEET_PARSED"
ERR_INTERNAL = "INTERNAL_ERROR"
ERR_JOB_STALE = "JOB_ABANDONED"
# Hai mã dưới đây từng là HTTP 400 của pha đồng bộ, lúc pha đó còn cầm file trong tay. Từ khi file
# tới bằng đường tải từ URL thì chúng chỉ lộ ra sau khi worker tải xong, nên chúng đi đường callback.
ERR_CANNOT_READ = "CANNOT_READ_WORKBOOK"
ERR_SHEET_NOT_FOUND = "SHEET_NOT_FOUND"


class _Heartbeat:
    """Thread phụ cập nhật mốc còn sống của job theo chu kỳ.

    Không thể chỉ cập nhật giữa các sheet: một workbook một sheet nặng có thể chạy hàng chục phút
    trong đúng một lệnh ``read_excel``, và vòng quét phục hồi sẽ tưởng job đã chết rồi drop bảng
    ngay dưới chân worker đang chạy.

    Dùng ``Event.wait`` thay cho ``sleep`` để dừng được ngay khi job xong, không phải đợi hết chu kỳ.
    """

    def __init__(self, job_id: int, interval: int):
        self._job_id = job_id
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"excel-hb-{job_id}")

    def _run(self):
        while not self._stop.wait(self._interval):
            try:
                touch_heartbeat(self._job_id)
            except Exception as e:
                SQLBotLogUtil.warning(f"heartbeat failed for excel job {self._job_id}: {e}")

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()


def submit_import_job(job_id: int) -> None:
    """Đẩy job vào pool. Chỉ được gọi SAU khi transaction ghi dòng job đã commit.

    Gọi trước khi commit là lỗi kinh điển "phát tín hiệu trước khi chốt sổ": worker mở session
    khác, và ở mức READ COMMITTED nó sẽ không thấy dòng job dù id đã có giá trị thật.
    """
    excel_import_executor.submit(_run_import_job_safe, job_id)


def shutdown_import_executor() -> None:
    """Tắt pool import lúc ứng dụng đóng, KHÔNG chờ job đang chạy.

    Chờ cho xong là để deploy treo tùy hứng theo kích thước file người dùng vừa tải lên. Bỏ dở an
    toàn được là nhờ mọi trạng thái đều nằm trong DB: job đang chạy sẽ ngừng heartbeat và vòng quét
    phục hồi kết liễu nó kèm dọn bảng; job còn xếp hàng vẫn ở ``pending`` và được nhặt lại sau.

    ``cancel_futures`` để các job chưa khởi động không bị chạy dở trong lúc tiến trình đang đóng
    connection pool.
    """
    try:
        excel_import_executor.shutdown(wait=False, cancel_futures=True)
    except Exception as e:
        SQLBotLogUtil.warning(f"cannot shutdown excel import executor: {e}")


def _run_import_job_safe(job_id: int) -> None:
    """Lớp bọc bắt mọi exception ở mức ngoài cùng.

    Bắt buộc phải có: ``executor.submit`` trả về một ``Future`` mà không ai gọi ``result()``, nên
    exception ném ra trong thread này bị NUỐT hoàn toàn — không log, không traceback. Repo đang có
    sẵn hành vi đó ở các tác vụ embedding; với import thì không chấp nhận được vì hệ ngoài đang chờ
    một callback.
    """
    try:
        _run_import_job(job_id)
    except Exception as e:
        SQLBotLogUtil.exception(f"excel import job {job_id} crashed: {e}")
        try:
            finish_job(job_id, success=False, error_code=ERR_INTERNAL, error_message=str(e))
        except Exception:
            SQLBotLogUtil.exception(f"cannot mark excel job {job_id} failed")


def _fetch_source_file(file_url: str, save_path: str) -> None:
    """Tải file nguồn về ``save_path``. Không làm gì nếu file đã có sẵn.

    Phép kiểm "đã có sẵn" không thừa: vòng quét phục hồi có thể nhặt lại một job mà worker trước đã
    tải xong rồi mới chết. Tải lại lúc đó vừa tốn thời gian vừa có thể hỏng hẳn — chữ ký của URL
    thường đã hết hạn khi tới lượt chạy thứ hai.

    Kiểm URL lại lần nữa ở đây thay vì tin dòng trong DB: giữa lúc nhận việc và lúc chạy có thể đã
    qua hàng giờ (job xếp hàng, hoặc job được phục hồi), nên hạn chữ ký phải soi lại theo hiện tại.
    Nó cũng chặn luôn trường hợp allowlist bị siết lại sau khi job đã vào hàng đợi.
    """
    if os.path.exists(save_path):
        SQLBotLogUtil.info(f"source file already on disk, skip download: {save_path}")
        return
    src = validate_source_url(file_url)
    SQLBotLogUtil.info(f"downloading source file from {src.safe_label}")
    download_to(src, save_path)


def _resolve_sheets(save_path: str, wanted: list[str]) -> list[str]:
    """Đối chiếu tên sheet client gửi với mục lục thật của file, trả về danh sách sẽ nạp.

    Bước này BẮT BUỘC dù ``parse_excel_preview`` cũng nhận ``sheet_names``: hàm đó bỏ qua IM LẶNG
    mọi tên không tồn tại. Thiếu phép đối chiếu ở đây thì một tên sheet gõ sai sẽ cho ra callback
    báo THÀNH CÔNG kèm một nguồn dữ liệu thiếu bảng — kiểu hỏng tệ nhất, vì không ai biết là đã hỏng.

    Trước đây việc này nằm ở pha đồng bộ và trả về 400. Nay pha đó không còn cầm file nên nó lùi
    xuống worker và về bằng callback.

    Danh sách trả về theo THỨ TỰ TRONG FILE, không theo thứ tự client gửi.
    """
    try:
        available = list_sheet_names(save_path)
    except Exception as e:
        raise ExcelImportError(ERR_CANNOT_READ, f"Cannot read workbook: {e}")

    if not wanted:
        return available

    missing = [s for s in wanted if s not in available]
    if missing:
        raise ExcelImportError(
            ERR_SHEET_NOT_FOUND,
            f"Sheet not found: {', '.join(missing)}. Available: {', '.join(available)}"
        )
    return [s for s in available if s in set(wanted)]


def _run_import_job(job_id: int) -> None:
    """Chạy trọn một job: tải file → đối chiếu sheet → tạo bảng → đồng bộ metadata → ghi outbox."""
    with Session(engine) as session:
        job = session.get(ExcelImportJob, job_id)
        if job is None:
            SQLBotLogUtil.error(f"excel import job {job_id} not found")
            return
        ds_id = job.ds_id
        save_path = job.file_path
        file_url = job.file_url
        sheet_names = list(job.sheet_names or [])

    mark_running(job_id, current_worker_id())

    created: list[str] = []
    try:
        # Tải NẰM TRONG khối heartbeat: một file lớn qua đường truyền chậm có thể mất hàng chục
        # phút, và nếu không có nhịp báo sống thì vòng quét phục hồi sẽ kết liễu đúng job đang khỏe.
        with _Heartbeat(job_id, settings.EXCEL_IMPORT_HEARTBEAT_SECONDS):
            if file_url:
                _fetch_source_file(file_url, save_path)

            if not os.path.exists(save_path):
                raise ExcelImportError(ERR_FILE_MISSING, f"Source file is gone: {save_path}")

            sheet_names = _resolve_sheets(save_path, sheet_names)
            if not sheet_names:
                raise ExcelImportError(ERR_NO_SHEET, "Workbook has no sheet to import")

            sheets_meta = parse_excel_preview(save_path, sheet_names=sheet_names or None)
            if not sheets_meta:
                raise ExcelImportError(ERR_NO_SHEET, "No sheet could be parsed from the uploaded file")

            sheet_fields = [
                SheetFields(sheetName=s["sheetName"], fields=[FieldInfo(**f) for f in s["fields"]])
                for s in sheets_meta
            ]

            def on_created(table_name: str) -> None:
                created.append(table_name)
                append_created_table(job_id, table_name)

            results = import_sheets_to_db(save_path, sheet_fields, on_table_created=on_created)
            _finalize_datasource(ds_id, os.path.basename(save_path), results)

        finish_job(job_id, success=True, callback_payload=_payload(ds_id, True, "Import succeeded"))
        SQLBotLogUtil.info(f"excel import job {job_id} finished, ds={ds_id}, tables={len(results)}")

    except ExcelImportError as e:
        fail_import_job(job_id, ds_id, created, e.error_code, e.message)
    except Exception as e:
        fail_import_job(job_id, ds_id, created, ERR_INTERNAL, str(e))
    finally:
        # Worker là bên duy nhất xóa file: từ khi file do chính worker tải về, không còn ai khác
        # đụng tới nó. Hai bên cùng xóa thì sinh lỗi lạ; không bên nào xóa thì rò đĩa.
        # Xóa cả file ``.part``: một lượt tải hỏng giữa chừng để lại nó, và nó không nằm trong tập
        # được vòng quét phục hồi bảo vệ nên cứ nằm đó chiếm đĩa cho tới lần dọn theo tuổi file.
        for leftover in (save_path, f"{save_path}.part"):
            try:
                if os.path.exists(leftover):
                    os.remove(leftover)
            except Exception as e:
                SQLBotLogUtil.warning(f"cannot remove temp file {leftover}: {e}")


def fail_import_job(job_id: int, ds_id: int, created: list[str], error_code: str, message: str) -> None:
    """Đưa job và nguồn dữ liệu về trạng thái thất bại, dọn các bảng đã lỡ tạo.

    Dùng chung cho hai người gọi: chính worker khi gặp lỗi, và vòng quét phục hồi khi kết liễu một
    job mà worker của nó đã chết. Cả hai đều cần đúng ba việc này theo đúng thứ tự này, và việc
    cuối — ``finish_job`` — mới là thứ đẩy lá thư báo hỏng vào outbox.
    """
    SQLBotLogUtil.error(f"excel import job {job_id} failed [{error_code}]: {message}")
    try:
        drop_tables(created)
    except Exception as e:
        SQLBotLogUtil.warning(f"cannot drop tables of failed job {job_id}: {e}")
    try:
        with Session(engine) as session:
            session.execute(
                update(CoreDatasource)
                .where(CoreDatasource.id == ds_id)
                .values(status=DatasourceStatus.FAILED)
            )
            session.commit()
    except Exception as e:
        SQLBotLogUtil.exception(f"cannot mark datasource {ds_id} failed: {e}")
    finish_job(job_id, success=False, error_code=error_code, error_message=message,
               callback_payload=_payload(ds_id, False, message))


def _payload(ds_id: int, ok: bool, message: str) -> dict:
    """Dựng payload callback theo đặc tả của hệ ngoài.

    Khóa đối chiếu tên là ``Id`` — viết hoa chữ I, đúng như cổng của đối tác đòi. Giá trị là dsId
    của SQLBot, tức hệ ngoài phải lưu lại ``dsId`` nhận được ở response 202 lúc tạo thì sau này mới
    khớp được bản tin này với bản ghi của họ.

    Tên trường này từng là ``externalId`` và sai: cổng của họ vẫn trả HTTP 200 kèm
    ``{"data": false, "message": "Không tìm thấy kho dữ liệu..."}``, nên phía ta ghi nhận là gửi
    thành công trong khi bên kia không nhận được gì. Đổi tên trường ở đây thì phải bắn thử lại bằng
    curl, đừng tin mỗi cột ``callback_status``.
    """
    return {"Id": ds_id, "status": ok, "message": message}


def _finalize_datasource(ds_id: int, filename: str, results: list[dict]) -> None:
    """Ghi cấu hình thật, đồng bộ bảng/field, rồi mới chuyển nguồn dữ liệu sang trạng thái dùng được.

    Thứ tự bắt buộc: ``configuration`` phải đúng TRƯỚC khi gọi ``sync_table``, vì hàm đó đọc cấu
    trúc bảng thật qua chính cấu hình này.

    Không dùng ``updateNum``: hàm đó chép ``ds.model_dump(exclude_unset=True)`` đè lên dòng trong
    DB, tức là một lost update nếu có ai vừa sửa nguồn dữ liệu. Ở đây ghi thẳng từng cột cần đổi.

    ``status`` đặt CUỐI CÙNG, sau khi mọi thứ đã sẵn sàng: đó là công tắc duy nhất cho phép nguồn
    dữ liệu lọt vào hỏi đáp.
    """
    configuration = aes_encrypt(json.dumps({"filename": filename, "sheets": results}, default=str)).decode()

    with Session(engine) as session:
        session.execute(
            update(CoreDatasource).where(CoreDatasource.id == ds_id).values(configuration=configuration)
        )
        session.commit()

        ds = session.get(CoreDatasource, ds_id)
        tables = [CoreTable(table_name=s["tableName"], table_comment=s["tableComment"]) for s in results]
        sync_table(session, ds, tables)

        session.execute(
            update(CoreDatasource)
            .where(CoreDatasource.id == ds_id)
            .values(status=DatasourceStatus.SUCCESS,
                    num=f"{len(results)}/{len(results)}")
        )
        session.commit()
