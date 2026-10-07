"""Test cho ``/createFromExcel`` đồng bộ: nạp từ bytes trong RAM, job ``delivery = response``.

Phủ phần lõi dùng chung với bản async (``run_import_job``, ``finish_job``, vòng phục hồi) và ba hàm
đọc file trên đường bytes. Handler cần Postgres (advisory lock) và mạng nên để cho smoke test.

Dùng SQLite trên FILE tạm, không dùng bộ nhớ: worker mở nhiều ``Session`` và có thread heartbeat,
mà SQLite trong RAM thì mỗi connection là một DB riêng. Hai chỗ phải chỉnh để SQLite dựng được bảng
của Postgres: JSONB biên dịch thành JSON, và BIGINT biên dịch thành INTEGER để cột id tự sinh (SQLite
chỉ tự sinh id cho đúng kiểu ``INTEGER PRIMARY KEY``).
"""

# Import sqlbot_xpack TRƯỚC: apps.datasource.crud.datasource nằm trong một vòng import lẫn nhau,
# chỉ gỡ được khi xpack là module khởi động vòng đó.
import sqlbot_xpack  # noqa: F401  isort:skip

import datetime
import io
import os

import pandas as pd
import pytest
from sqlalchemy import BigInteger, create_engine, inspect, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlmodel import Session

from apps.datasource.crud import excel_job as job_crud
from apps.datasource.models.datasource import CoreDatasource, CoreField, CoreTable, DatasourceStatus
from apps.datasource.models.excel_job import (
    CallbackStatus,
    ExcelImportDelivery,
    ExcelImportJob,
    ExcelImportJobStatus,
)
from apps.datasource.task import excel_import_task as task
from apps.datasource.task import excel_recovery
from apps.datasource.utils import excel_import
from apps.datasource.utils.excel import InMemoryWorkbook, list_sheet_names, parse_excel_preview
from apps.datasource.utils.excel_import import ExcelImportError, import_sheets_to_db
from apps.datasource.models.datasource import FieldInfo, SheetFields
from common.core.config import settings


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(element, compiler, **kw):
    """SQLite không có JSONB; JSON đủ cho mọi phép đọc ghi trong test."""
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_on_sqlite(element, compiler, **kw):
    """Để cột id ``BIGINT PRIMARY KEY`` thành ``INTEGER PRIMARY KEY`` và tự sinh giá trị."""
    return "INTEGER"


def _xlsx(sheets: dict[str, pd.DataFrame]) -> bytes:
    """Dựng bytes của một file .xlsx nhiều sheet."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for name, df in sheets.items():
            df.to_excel(writer, sheet_name=name, index=False)
    return buf.getvalue()


TWO_SHEETS = _xlsx({
    "Quy1": pd.DataFrame({"tinh": ["A", "B", "C"], "thu": [1, 2, 3]}),
    "Quy2": pd.DataFrame({"tinh": ["D", "E"], "thu": [4, 5]}),
})
CSV = b"tinh,thu\nA,1\nB,2\n"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """DB metadata + kho dữ liệu Excel bằng hai file SQLite; ``EXCEL_PATH`` trỏ vào thư mục tạm."""
    meta = create_engine(f"sqlite:///{tmp_path / 'meta.db'}")
    for model in (ExcelImportJob, CoreDatasource, CoreTable, CoreField):
        model.__table__.create(meta)
    monkeypatch.setattr(job_crud, "engine", meta)
    monkeypatch.setattr(task, "engine", meta)

    warehouse_url = f"sqlite:///{tmp_path / 'warehouse.db'}"
    monkeypatch.setattr(excel_import, "get_engine_conn", lambda: create_engine(warehouse_url))

    excel_dir = tmp_path / "excel"
    excel_dir.mkdir()
    monkeypatch.setattr(settings, "EXCEL_PATH", str(excel_dir))

    synced = []
    monkeypatch.setattr(task, "sync_table", lambda session, ds, tables: synced.append(ds.id))

    class Env:
        pass

    e = Env()
    e.meta = meta
    e.warehouse = create_engine(warehouse_url)
    e.excel_dir = excel_dir
    e.synced = synced
    yield e
    meta.dispose()
    e.warehouse.dispose()


def _new_ds(env) -> int:
    """Chèn một datasource giữ chỗ ``Importing`` như ``create_ds_importing``."""
    with Session(env.meta) as s:
        ds = CoreDatasource(name="ds", description="", type="excel", configuration="",
                            status=DatasourceStatus.IMPORTING, oid=1)
        s.add(ds)
        s.commit()
        return ds.id


def _new_job(env, ds_id: int, delivery: str, sheet_names=(), file_path: str = "") -> int:
    with Session(env.meta) as s:
        job = job_crud.create_job(s, ds_id=ds_id, oid=1, create_by=1, ds_name="ds",
                                  file_path=file_path, sheet_names=list(sheet_names),
                                  delivery=delivery)
        s.commit()
        return job.id


def _job(env, job_id: int) -> ExcelImportJob:
    with Session(env.meta) as s:
        return s.get(ExcelImportJob, job_id)


def _ds(env, ds_id: int) -> CoreDatasource | None:
    with Session(env.meta) as s:
        return s.get(CoreDatasource, ds_id)


def _tables(env) -> set[str]:
    return set(inspect(env.warehouse).get_table_names())


class TestDocTuBytes:
    def test_list_sheet_names(self):
        assert list_sheet_names(InMemoryWorkbook(TWO_SHEETS, "a.xlsx")) == ["Quy1", "Quy2"]
        assert list_sheet_names(InMemoryWorkbook(CSV, "a.csv")) == ["Sheet1"]

    def test_preview_doc_du_moi_sheet(self):
        sheets = parse_excel_preview(InMemoryWorkbook(TWO_SHEETS, "a.xlsx"))
        assert [(s["sheetName"], s["rows"]) for s in sheets] == [("Quy1", 3), ("Quy2", 2)]

    def test_preview_csv(self):
        sheets = parse_excel_preview(InMemoryWorkbook(CSV, "a.CSV"))
        assert [(s["sheetName"], s["rows"]) for s in sheets] == [("Sheet1", 2)]

    def test_import_nap_du_moi_sheet(self, env):
        fields = [FieldInfo(fieldName="tinh", fieldType="string"),
                  FieldInfo(fieldName="thu", fieldType="int")]
        results = import_sheets_to_db(
            InMemoryWorkbook(TWO_SHEETS, "a.xlsx"),
            [SheetFields(sheetName="Quy1", fields=fields), SheetFields(sheetName="Quy2", fields=fields)],
        )
        assert [r["rows"] for r in results] == [3, 2]
        with env.warehouse.connect() as conn:
            counts = [pd.read_sql_table(r["tableName"], conn).shape[0] for r in results]
        assert counts == [3, 2]


class TestResolveSheets:
    def test_csv_bo_qua_ten_sheet(self):
        assert task.resolve_sheets(InMemoryWorkbook(CSV, "a.csv"), ["Bat ky"]) == ["Sheet1"]

    def test_sai_ten_kem_danh_sach_sheet(self):
        with pytest.raises(ExcelImportError) as ei:
            task.resolve_sheets(InMemoryWorkbook(TWO_SHEETS, "a.xlsx"), ["Quy1", "Quy9"])
        assert ei.value.error_code == "SHEET_NOT_FOUND"
        assert ei.value.details == {"availableSheets": ["Quy1", "Quy2"]}

    def test_file_hong(self):
        with pytest.raises(ExcelImportError) as ei:
            task.resolve_sheets(InMemoryWorkbook(b"not a zip", "a.xlsx"), [])
        assert ei.value.error_code == "CANNOT_READ_WORKBOOK"


class TestJobCrud:
    def test_job_response_tao_thang_running(self, env):
        job = _job(env, _new_job(env, 1, ExcelImportDelivery.RESPONSE))
        assert job.status == ExcelImportJobStatus.RUNNING
        assert job.worker_id and job.started_at and job.heartbeat_at
        assert job.file_url is None and job.file_path == ""

    def test_job_callback_van_pending(self, env):
        job = _job(env, _new_job(env, 1, ExcelImportDelivery.CALLBACK, file_path="/x.xlsx"))
        assert job.status == ExcelImportJobStatus.PENDING
        assert job.delivery == ExcelImportDelivery.CALLBACK

    def test_finish_job_response_khong_vao_outbox(self, env):
        job_id = _new_job(env, 1, ExcelImportDelivery.RESPONSE)
        job_crud.finish_job(job_id, success=False, error_code="X", callback_payload={"Id": 1})
        job = _job(env, job_id)
        assert job.status == ExcelImportJobStatus.FAILED and job.error_code == "X"
        assert job.callback_status == CallbackStatus.NONE and job.callback_payload is None
        assert job_crud.resend_callback(job_id) is False

    def test_finish_job_callback_nhu_cu(self, env):
        job_id = _new_job(env, 1, ExcelImportDelivery.CALLBACK, file_path="/x.xlsx")
        job_crud.finish_job(job_id, success=True, callback_payload={"Id": 1})
        job = _job(env, job_id)
        assert job.callback_status == CallbackStatus.PENDING
        assert job.callback_payload == {"Id": 1}


class TestRunImportJob:
    def test_thanh_cong(self, env):
        ds_id = _new_ds(env)
        job_id = _new_job(env, ds_id, ExcelImportDelivery.RESPONSE, ["Quy2"])
        results = task.run_import_job(job_id, InMemoryWorkbook(TWO_SHEETS, "thu.xlsx"))

        assert [(r["sheetName"], r["rows"]) for r in results] == [("Quy2", 2)]
        assert _tables(env) == {results[0]["tableName"]}
        assert _ds(env, ds_id).status == DatasourceStatus.SUCCESS
        assert env.synced == [ds_id]
        job = _job(env, job_id)
        assert job.status == ExcelImportJobStatus.SUCCESS
        assert job.callback_status == CallbackStatus.NONE
        assert os.listdir(env.excel_dir) == []

    def test_csv_doc_hai_lan_tu_cung_bytes(self, env):
        """Xem trước rồi nạp là hai lần đọc; dùng chung buffer thì lần hai ném ``EmptyDataError``."""
        ds_id = _new_ds(env)
        job_id = _new_job(env, ds_id, ExcelImportDelivery.RESPONSE, ["Ten bat ky"])
        results = task.run_import_job(job_id, InMemoryWorkbook(CSV, "thu.csv"))
        assert [(r["sheetName"], r["rows"]) for r in results] == [("Sheet1", 2)]

    def test_hong_o_sheet_thu_hai_drop_bang_sheet_dau(self, env, monkeypatch):
        real_to_sql = pd.DataFrame.to_sql
        calls = []

        def flaky_to_sql(self, *args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("disk full")
            return real_to_sql(self, *args, **kwargs)

        monkeypatch.setattr(pd.DataFrame, "to_sql", flaky_to_sql)
        ds_id = _new_ds(env)
        job_id = _new_job(env, ds_id, ExcelImportDelivery.RESPONSE)

        with pytest.raises(ExcelImportError) as ei:
            task.run_import_job(job_id, InMemoryWorkbook(TWO_SHEETS, "thu.xlsx"))

        assert ei.value.error_code == "TABLE_WRITE_FAILED"
        assert _tables(env) == set()
        assert _ds(env, ds_id).status == DatasourceStatus.FAILED
        job = _job(env, job_id)
        assert job.status == ExcelImportJobStatus.FAILED
        assert job.callback_status == CallbackStatus.NONE
        assert os.listdir(env.excel_dir) == []

    def test_datasource_bi_xoa_giua_chung(self, env):
        ds_id = _new_ds(env)
        job_id = _new_job(env, ds_id, ExcelImportDelivery.RESPONSE)
        with Session(env.meta) as s:
            s.delete(s.get(CoreDatasource, ds_id))
            s.commit()

        with pytest.raises(ExcelImportError) as ei:
            task.run_import_job(job_id, InMemoryWorkbook(TWO_SHEETS, "thu.xlsx"))

        assert ei.value.error_code == "DATASOURCE_DELETED"
        assert _tables(env) == set()
        assert env.synced == []

    def test_loi_la_boc_thanh_internal_error(self, env, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(task, "parse_excel_preview", boom)
        ds_id = _new_ds(env)
        job_id = _new_job(env, ds_id, ExcelImportDelivery.RESPONSE)
        with pytest.raises(ExcelImportError) as ei:
            task.run_import_job(job_id, InMemoryWorkbook(TWO_SHEETS, "thu.xlsx"))
        assert ei.value.error_code == "INTERNAL_ERROR"
        assert _job(env, job_id).status == ExcelImportJobStatus.FAILED

    def test_duong_worker_van_doc_tu_dia_va_vao_outbox(self, env):
        """Hồi quy cho bản async: file trên đĩa, job ``callback``, lỗi bị nuốt ở lớp safe."""
        path = env.excel_dir / "thu_abc.xlsx"
        path.write_bytes(TWO_SHEETS)
        ds_id = _new_ds(env)
        job_id = _new_job(env, ds_id, ExcelImportDelivery.CALLBACK, file_path=str(path))

        task._run_import_job_safe(job_id)

        job = _job(env, job_id)
        assert job.status == ExcelImportJobStatus.SUCCESS
        assert job.callback_status == CallbackStatus.PENDING
        assert len(_tables(env)) == 2
        assert not path.exists()


class TestPhucHoi:
    def test_job_response_chet_bi_ket_lieu_khong_sinh_callback(self, env, monkeypatch):
        ds_id = _new_ds(env)
        job_id = _new_job(env, ds_id, ExcelImportDelivery.RESPONSE)
        with env.warehouse.connect() as conn:
            pd.DataFrame({"a": [1]}).to_sql("excel_mo_coi", conn, index=False)
            conn.commit()
        job_crud.append_created_table(job_id, "excel_mo_coi")
        long_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
        with Session(env.meta) as s:
            s.execute(update(ExcelImportJob).where(ExcelImportJob.id == job_id)
                      .values(heartbeat_at=long_ago, created_at=long_ago))
            s.commit()
        submitted = []
        monkeypatch.setattr(excel_recovery, "submit_import_job", submitted.append)

        assert excel_recovery._recover_orphan_jobs() == 0
        assert excel_recovery._bury_dead_jobs() == 1

        assert submitted == []
        assert _tables(env) == set()
        assert _ds(env, ds_id).status == DatasourceStatus.FAILED
        job = _job(env, job_id)
        assert job.status == ExcelImportJobStatus.FAILED and job.error_code == "JOB_ABANDONED"
        assert job.callback_status == CallbackStatus.NONE


class TestMaLoiHttp:
    @pytest.mark.parametrize("code, status", [
        ("URL_EXPIRED", 400),
        ("DOWNLOAD_NOT_FOUND", 400),
        ("FILE_TOO_LARGE", 400),
        ("CANNOT_READ_WORKBOOK", 400),
        ("DOWNLOAD_FAILED", 502),
        ("TABLE_WRITE_FAILED", 500),
        ("DATASOURCE_DELETED", 500),
        ("INTERNAL_ERROR", 500),
    ])
    def test_status(self, code, status):
        from apps.datasource.api.datasource import _import_error

        exc = _import_error(ExcelImportError(code, "m"))
        assert exc.status_code == status
        assert exc.detail == {"code": code, "message": "m"}

    def test_sheet_not_found_kem_available_sheets(self):
        from apps.datasource.api.datasource import _import_error

        exc = _import_error(ExcelImportError("SHEET_NOT_FOUND", "m",
                                             details={"availableSheets": ["Quy1"]}))
        assert exc.status_code == 400
        assert exc.detail == {"code": "SHEET_NOT_FOUND", "message": "m", "availableSheets": ["Quy1"]}
