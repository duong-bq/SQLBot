import asyncio
import hashlib
import io
import os
import time
import traceback
import uuid
from io import StringIO
from typing import List, Optional
from urllib.parse import quote

import pandas as pd
from fastapi import APIRouter, File, Form, UploadFile, HTTPException, Path, Query
from fastapi.responses import StreamingResponse
from psycopg2 import sql
from sqlalchemy import and_

from apps.db.db import get_schema
from apps.db.engine import get_engine_conn
from apps.swagger.i18n import PLACEHOLDER_PREFIX
from apps.system.schemas.permission import SqlbotPermission, require_permissions
from common.audit.models.log_model import OperationType, OperationModules
from common.audit.schemas.logger_decorator import LogConfig, system_log
from common.core.config import settings
from common.core.deps import SessionDep, CurrentUser, Trans
from common.utils.distributed_lock import try_acquire_xact_lock
from common.utils.utils import SQLBotLogUtil
from ..crud.datasource import get_datasource_list, check_status, create_ds, update_ds, delete_ds, getTables, getFields, \
    update_table_and_fields, getTablesByDs, chooseTables, preview, updateTable, updateField, get_ds, fieldEnum, \
    check_status_by_id, sync_single_fields, check_name, create_ds_importing, find_conflicting_ds
from ..crud.excel_job import create_job, get_job_by_ds_id, resend_callback
from ..crud.field import get_fields_by_table_id
from ..crud.table import get_tables_by_ds_id
from ..models.datasource import CoreDatasource, CreateDatasource, TableObj, CoreTable, CoreField, FieldObj, \
    TableSchemaResponse, ColumnSchemaResponse, PreviewResponse, ImportRequest, SheetFields, FieldInfo, CreatedExcelTable, \
    CreateFromExcelResponse, CreateFromExcelAcceptedResponse, CreateFromExcelUrlRequest, DatasourceStatus, \
    ExcelImportStatusResponse
from ..task.excel_import_task import submit_import_job
from ..utils.excel import parse_excel_preview
from ..utils.excel_import import ExcelImportError, import_sheets_to_db
from ..utils.remote_file import probe_source, safe_stem, validate_source_url
from ..utils.utils import normalize_configuration

router = APIRouter(tags=["Datasource"], prefix="/datasource")
path = settings.EXCEL_PATH


def _normalize_ds_configuration(ds) -> None:
    """Chuẩn hóa ``configuration`` tại chỗ trên body client gửi lên, CHỈ gán khi giá trị thật sự đổi.

    Không gán vô điều kiện: ``update_ds`` ghi DB theo ``model_dump(exclude_unset=True)``, mà chạm
    vào thuộc tính của model pydantic sẽ đánh dấu field đó là "đã set". Một request chỉ đổi tên
    (không kèm ``configuration``) sẽ vì thế ghi đè ``configuration`` bằng ``None`` và xóa mất cấu
    hình kết nối của nguồn dữ liệu. Đã kiểm chứng: ``model_fields_set`` nở thêm ngay sau phép gán.

    Phải gọi ở MỌI endpoint nhận cấu hình từ client. Với ``CoreDatasource`` (SQLModel ``table=True``)
    FastAPI không validate body, nên một ``configuration`` dạng object đi thẳng vào hàm dưới dạng
    dict và sẽ nổ ở ``aes_decrypt`` nếu thiếu bước này — không có 422 nào chặn hộ.
    """
    normalized = normalize_configuration(ds.configuration)
    if normalized != ds.configuration:
        ds.configuration = normalized


@router.get("/ws/{oid}", include_in_schema=False)
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def query_by_oid(session: SessionDep, user: CurrentUser, oid: int) -> List[CoreDatasource]:
    """
    Lấy danh sách nguồn dữ liệu của một workspace ``oid`` cụ thể (quyền ws_admin, ẩn khỏi Swagger).

    Khác với /list (dùng workspace hiện tại của user), endpoint này chỉ định thẳng ``oid``.
    """
    return get_datasource_list(session=session, user=user, oid=oid)


@router.get("/list", response_model=List[CoreDatasource], summary=f"{PLACEHOLDER_PREFIX}ds_list")
async def datasource_list(session: SessionDep, user: CurrentUser):
    """
    Lấy danh sách nguồn dữ liệu người dùng hiện tại truy cập được (trong workspace hiện tại).

    Kết quả đã lọc theo quyền của user. Dùng để hiển thị danh sách nguồn dữ liệu và chọn nguồn khi hỏi đáp.
    """
    return get_datasource_list(session=session, user=user)


@router.get("/get/{id}", response_model=CoreDatasource, summary=f"{PLACEHOLDER_PREFIX}ds_get")
@require_permissions(permission=SqlbotPermission(role=['ws_admin'], keyExpression="id", type='ds'))
async def get_datasource(session: SessionDep, id: int = Path(..., description=f"{PLACEHOLDER_PREFIX}ds_id")):
    """
    Lấy chi tiết một nguồn dữ liệu theo ``id`` (quyền ws_admin trên nguồn đó).

    Trả về đầy đủ thông tin ``CoreDatasource`` (loại DB, cấu hình kết nối...) để hiển thị/chỉnh sửa.
    """
    return get_ds(session, id)


@router.post("/check", response_model=bool, summary=f"{PLACEHOLDER_PREFIX}ds_check")
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def check(session: SessionDep, trans: Trans, ds: CoreDatasource):
    """
    Kiểm tra kết nối tới một nguồn dữ liệu theo cấu hình gửi lên (chưa cần lưu) — quyền ws_admin.

    Body ``CoreDatasource`` chứa thông tin kết nối. Trả về true/false cho biết có kết nối được không.
    Dùng khi thêm/sửa nguồn dữ liệu để thử kết nối trước khi lưu.

    ``configuration`` là object lồng; nhánh chuỗi cũ vẫn nhận để không vỡ giao diện dev — xem
    ``normalize_configuration``.
    """
    _normalize_ds_configuration(ds)

    def inner():
        return check_status(session, trans, ds, True)

    return await asyncio.to_thread(inner)


@router.get("/check/{ds_id}", response_model=bool, summary=f"{PLACEHOLDER_PREFIX}ds_check")
@require_permissions(permission=SqlbotPermission(type='ds', keyExpression="ds_id"))
async def check_by_id(session: SessionDep, trans: Trans,
                      ds_id: int = Path(..., description=f"{PLACEHOLDER_PREFIX}ds_id")):
    """
    Kiểm tra kết nối tới một nguồn dữ liệu đã lưu theo ``ds_id``.

    Trả về true/false. Dùng để hiển thị trạng thái kết nối của nguồn dữ liệu trong danh sách.
    """
    def inner():
        return check_status_by_id(session, trans, ds_id, True)

    return await asyncio.to_thread(inner)


@router.post("/add", response_model=CoreDatasource, summary=f"{PLACEHOLDER_PREFIX}ds_add")
@system_log(LogConfig(operation_type=OperationType.CREATE, module=OperationModules.DATASOURCE, result_id_expr="id"))
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def add(session: SessionDep, trans: Trans, user: CurrentUser, ds: CreateDatasource):
    """
    Thêm mới một nguồn dữ liệu (quyền ws_admin).

    Body ``CreateDatasource`` gồm loại DB, cấu hình kết nối và danh sách bảng chọn đồng bộ. Sau khi tạo sẽ
    đồng bộ metadata (bảng/cột) và sinh embedding phục vụ RAG. Trả về ``CoreDatasource`` vừa tạo. Có ghi log.

    ``configuration`` là object lồng; nhánh chuỗi cũ vẫn nhận để không vỡ giao diện dev — xem
    ``normalize_configuration``.

    Endpoint này KHÔNG tự kiểm tra kết nối (``status`` gán cứng "Success"), và đã commit bản ghi
    nguồn dữ liệu TRƯỚC khi đồng bộ bảng — sync hỏng thì còn lại một nguồn rỗng, không tự dọn.
    Client phải gọi ``/check`` trước, và gọi ``/delete`` dọn khi endpoint này ném lỗi.
    """
    _normalize_ds_configuration(ds)
    return await create_ds(session, trans, user, ds)


@router.post("/chooseTables/{id}", response_model=None, summary=f"{PLACEHOLDER_PREFIX}ds_choose_tables")
@require_permissions(permission=SqlbotPermission(role=['ws_admin'], type='ds', keyExpression="id"))
async def choose_tables(session: SessionDep, trans: Trans, tables: List[CoreTable],
                        id: int = Path(..., description=f"{PLACEHOLDER_PREFIX}ds_id")):
    """
    Chọn lại tập bảng được đồng bộ cho nguồn dữ liệu ``id`` (quyền ws_admin trên nguồn đó).

    Body ``tables`` là danh sách bảng cần giữ/đồng bộ. Bảng không có trong danh sách sẽ bị loại khỏi phạm
    vi dùng cho hỏi đáp. Sau khi chọn sẽ cập nhật metadata và embedding tương ứng.
    """
    def inner():
        chooseTables(session, trans, id, tables)

    await asyncio.to_thread(inner)


@router.post("/update", response_model=CoreDatasource, summary=f"{PLACEHOLDER_PREFIX}ds_update")
@require_permissions(permission=SqlbotPermission(role=['ws_admin'], type='ds', keyExpression="ds.id"))
@system_log(
    LogConfig(operation_type=OperationType.UPDATE, module=OperationModules.DATASOURCE, resource_id_expr="ds.id"))
async def update(session: SessionDep, trans: Trans, user: CurrentUser, ds: CoreDatasource):
    """
    Cập nhật thông tin/cấu hình một nguồn dữ liệu đã có (quyền ws_admin trên nguồn đó).

    Body ``CoreDatasource`` chứa id và các trường cần sửa (tên, kết nối...). Trả về bản ghi sau cập nhật.
    Có ghi log thao tác.

    Chỉ sửa cấu hình/tên, KHÔNG đồng bộ lại bảng — đổi tập bảng phải dùng ``/chooseTables/{id}``.

    ``configuration`` là object lồng; nhánh chuỗi cũ vẫn nhận để không vỡ giao diện dev — xem
    ``normalize_configuration``.
    """
    _normalize_ds_configuration(ds)

    def inner():
        return update_ds(session, trans, user, ds)

    return await asyncio.to_thread(inner)


@router.post("/delete/{id}/{name}", response_model=None, summary=f"{PLACEHOLDER_PREFIX}ds_delete")
@require_permissions(permission=SqlbotPermission(role=['ws_admin'], type='ds', keyExpression="id"))
@system_log(LogConfig(operation_type=OperationType.DELETE, module=OperationModules.DATASOURCE, resource_id_expr="id",
                      ))
async def delete(session: SessionDep, id: int = Path(..., description=f"{PLACEHOLDER_PREFIX}ds_id"), name: str = None):
    """
    Xóa một nguồn dữ liệu theo ``id`` (``name`` chỉ dùng để ghi log) — quyền ws_admin trên nguồn đó.

    Xóa kèm metadata bảng/cột và embedding liên quan. Có ghi log thao tác.
    """
    return await delete_ds(session, id)


@router.get("/getTables/{id}", response_model=List[TableSchemaResponse], summary=f"{PLACEHOLDER_PREFIX}ds_get_tables")
@require_permissions(permission=SqlbotPermission(type='ds', keyExpression="id"))
async def get_tables(session: SessionDep, id: int = Path(..., description=f"{PLACEHOLDER_PREFIX}ds_id")):
    """
    Lấy danh sách bảng (kèm schema) của nguồn dữ liệu ``id`` bằng cách truy vấn trực tiếp DB nguồn.

    Trả về ``TableSchemaResponse``. Dùng khi chọn bảng để đồng bộ. Yêu cầu quyền trên nguồn dữ liệu.
    """
    return getTables(session, id)


@router.post("/getTablesByConf", response_model=List[TableSchemaResponse], summary=f"{PLACEHOLDER_PREFIX}ds_get_tables")
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def get_tables_by_conf(session: SessionDep, trans: Trans, ds: CoreDatasource):
    """
    Lấy danh sách bảng theo cấu hình kết nối gửi lên (chưa cần lưu nguồn) — quyền ws_admin.

    Body ``CoreDatasource`` chứa thông tin kết nối. Nếu lấy bảng lỗi mà kết nối vẫn OK thì trả 500 kèm chi
    tiết. Dùng ở bước thêm nguồn: sau khi nhập kết nối, xem trước các bảng có thể đồng bộ.

    Danh sách bảng lọc theo ``dbSchema`` trong ``configuration`` (với MySQL/Doris/StarRocks là
    ``database``), nên phải chọn schema trước mới gọi được endpoint này.

    ``configuration`` là object lồng; nhánh chuỗi cũ vẫn nhận để không vỡ giao diện dev — xem
    ``normalize_configuration``.
    """
    _normalize_ds_configuration(ds)
    try:
        def inner():
            return getTablesByDs(session, ds)

        return await asyncio.to_thread(inner)
    except Exception as e:
        # check ds status
        def inner():
            return check_status(session, trans, ds, True)

        status = await asyncio.to_thread(inner)
        if status:
            SQLBotLogUtil.error(f"get table failed: {e}")
            raise HTTPException(status_code=500, detail=f'Get table Failed: {e.args}')


@router.post("/getSchemaByConf", response_model=List[str], summary=f"{PLACEHOLDER_PREFIX}ds_get_schema")
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def get_schema_by_conf(session: SessionDep, trans: Trans, ds: CoreDatasource):
    """
    Lấy danh sách schema/database theo cấu hình kết nối gửi lên (chưa cần lưu nguồn) — quyền ws_admin.

    Body ``CoreDatasource`` chứa thông tin kết nối. Trả về danh sách tên schema để người dùng chọn trước
    khi lấy bảng (với các DB hỗ trợ nhiều schema như PostgreSQL). Lỗi mà kết nối vẫn OK thì trả 500.

    Chỉ cài đặt cho pg, sqlServer, oracle, dm, redshift, kingbase. MySQL và nhóm Doris/StarRocks/
    ClickHouse/Hive không có khái niệm schema tách rời — ``database`` đã là namespace, bỏ qua bước này.

    ``configuration`` là object lồng; nhánh chuỗi cũ vẫn nhận để không vỡ giao diện dev — xem
    ``normalize_configuration``.
    """
    _normalize_ds_configuration(ds)
    try:
        def inner():
            return get_schema(ds)

        return await asyncio.to_thread(inner)
    except Exception as e:
        # check ds status
        def inner():
            return check_status(session, trans, ds, True)

        status = await asyncio.to_thread(inner)
        if status:
            SQLBotLogUtil.error(f"get table failed: {e}")
            raise HTTPException(status_code=500, detail=f'Get table Failed: {e.args}')


@router.get("/getFields/{id}/{table_name}", response_model=List[ColumnSchemaResponse],
            summary=f"{PLACEHOLDER_PREFIX}ds_get_fields")
@require_permissions(permission=SqlbotPermission(role=['ws_admin'], type='ds', keyExpression="id"))
async def get_fields(session: SessionDep,
                     id: int = Path(..., description=f"{PLACEHOLDER_PREFIX}ds_id"),
                     table_name: str = Path(..., description=f"{PLACEHOLDER_PREFIX}ds_table_name")):
    """
    Lấy danh sách cột (field) của một bảng ``table_name`` thuộc nguồn dữ liệu ``id`` — quyền ws_admin.

    Truy vấn trực tiếp DB nguồn, trả về ``ColumnSchemaResponse`` (tên cột, kiểu dữ liệu...).
    """
    return getFields(session, id, table_name)


@router.post("/syncFields/{id}", response_model=None, summary=f"{PLACEHOLDER_PREFIX}ds_sync_fields")
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def sync_fields(session: SessionDep, trans: Trans,
                      id: int = Path(..., description=f"{PLACEHOLDER_PREFIX}ds_table_id")):
    """
    Đồng bộ lại danh sách cột của một bảng (``id`` là id bảng, không phải id nguồn) — quyền ws_admin.

    Đọc lại cấu trúc cột từ DB nguồn và cập nhật metadata/embedding. Dùng khi cấu trúc bảng ở DB thay đổi.
    """
    return sync_single_fields(session, trans, id)


from pydantic import BaseModel


class TestObj(BaseModel):
    sql: str = None


# not used, just do test
""" @router.post("/execSql/{id}", include_in_schema=False)
async def exec_sql(session: SessionDep, id: int, obj: TestObj):
    def inner():
        data = execSql(session, id, obj.sql)
        try:
            data_obj = data.get('data')
            # print(orjson.dumps(data, option=orjson.OPT_NON_STR_KEYS).decode())
            print(orjson.dumps(data_obj).decode())
        except Exception:
            traceback.print_exc()

        return data

    return await asyncio.to_thread(inner) """


@router.get("/tableList/{id}", response_model=List[CoreTable], summary=f"{PLACEHOLDER_PREFIX}ds_table_list")
@require_permissions(permission=SqlbotPermission(role=['ws_admin'], type='ds', keyExpression="id"))
async def table_list(session: SessionDep, id: int = Path(..., description=f"{PLACEHOLDER_PREFIX}ds_id")):
    """
    Lấy danh sách bảng ĐÃ đồng bộ (lưu trong SQLBot) của nguồn dữ liệu ``id`` — quyền ws_admin.

    Khác với /getTables (đọc trực tiếp DB nguồn): endpoint này trả về ``CoreTable`` đã lưu, kèm chú thích
    tùy chỉnh. Dùng ở màn hình quản lý bảng/chú thích.
    """
    return get_tables_by_ds_id(session, id)


@router.get("/fieldList/{id}", response_model=List[CoreField], summary=f"{PLACEHOLDER_PREFIX}ds_field_list")
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def field_list(session: SessionDep,
                     id: int = Path(..., description=f"{PLACEHOLDER_PREFIX}ds_table_id"),
                     fieldName: Optional[str] = Query(None,
                                                      description=f"{PLACEHOLDER_PREFIX}ds_field_name")):
    """
    Lấy danh sách cột ĐÃ đồng bộ của một bảng (``id`` là id bảng) — quyền ws_admin.

    ``fieldName`` là query param không bắt buộc, lọc theo tên cột kiểu chứa. Trước đây điều kiện lọc
    nằm trong body và pydantic bắt buộc phải có, nên client không lọc vẫn phải gửi ``fieldName: null``
    — bỏ trống là 422. Chuyển sang GET nên không còn cái bẫy đó.
    """
    return get_fields_by_table_id(session, id, FieldObj(fieldName=fieldName))


@router.post("/editLocalComment", include_in_schema=False)
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def edit_local(session: SessionDep, data: TableObj):
    """
    Cập nhật chú thích tùy chỉnh của một bảng cùng các cột của nó (quyền ws_admin, ẩn khỏi Swagger).

    Body ``TableObj`` gồm thông tin bảng và danh sách cột. Chú thích này bổ sung ngữ nghĩa giúp LLM sinh
    SQL chính xác hơn.
    """
    update_table_and_fields(session, data)


@router.post("/editTable", response_model=None, summary=f"{PLACEHOLDER_PREFIX}ds_edit_table")
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def edit_table(session: SessionDep, table: CoreTable):
    """
    Cập nhật thông tin một bảng đã đồng bộ (ví dụ chú thích tùy chỉnh, bật/tắt dùng cho hỏi đáp) — quyền ws_admin.

    Body ``CoreTable``. Thay đổi chú thích sẽ cập nhật lại embedding tương ứng để dùng cho RAG.
    """
    updateTable(session, table)


@router.post("/editField", response_model=None, summary=f"{PLACEHOLDER_PREFIX}ds_edit_field")
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def edit_field(session: SessionDep, field: CoreField):
    """
    Cập nhật thông tin một cột đã đồng bộ (chú thích tùy chỉnh, bật/tắt...) — quyền ws_admin.

    Body ``CoreField``. Chú thích cột giúp LLM hiểu ý nghĩa cột khi sinh SQL; thay đổi sẽ cập nhật embedding.
    """
    updateField(session, field)


@router.get("/previewData/{id}", response_model=PreviewResponse, summary=f"{PLACEHOLDER_PREFIX}ds_preview_data")
@require_permissions(permission=SqlbotPermission(type='ds', keyExpression="id"))
async def preview_data(session: SessionDep, trans: Trans, current_user: CurrentUser,
                       id: int = Path(..., description=f"{PLACEHOLDER_PREFIX}ds_id"),
                       table_id: int = Query(..., description=f"{PLACEHOLDER_PREFIX}ds_table_id")):
    """
    Xem trước 100 dòng dữ liệu của một bảng thuộc nguồn dữ liệu ``id`` (yêu cầu quyền trên nguồn đó).

    ``table_id`` là query param bắt buộc. Trước đây bảng được xác định bằng cả một object ``TableObj``
    trong body, nhưng chỉ ``table.id`` được dùng thật — phần còn lại (kể cả ``fields``) bị bỏ qua.
    Nếu truy vấn lỗi mà kết nối vẫn OK thì trả 500.
    """
    def inner():
        try:
            return preview(session, current_user, id, table_id)
        except Exception as e:
            ds = session.query(CoreDatasource).filter(CoreDatasource.id == id).first()
            # check ds status
            status = check_status(session, trans, ds, True)
            if status:
                SQLBotLogUtil.error(f"Preview failed: {e}")
                raise HTTPException(status_code=500, detail=f'Preview Failed: {e.args}')

    return await asyncio.to_thread(inner)


# not used
@router.post("/fieldEnum/{ds_id}/{id}", include_in_schema=False)
@require_permissions(permission=SqlbotPermission(type='ds', keyExpression="ds_id"))
async def field_enum(session: SessionDep, ds_id: int, id: int):
    """
    Lấy danh sách giá trị phân biệt (enum) của một cột ``id`` thuộc nguồn ``ds_id`` (không dùng, ẩn khỏi Swagger).

    Truy vấn các giá trị khác nhau của cột, phục vụ gợi ý lọc. Hiện được đánh dấu "not used".
    """
    def inner():
        return fieldEnum(session, id)

    return await asyncio.to_thread(inner)


# @router.post("/uploadExcel")
# async def upload_excel(session: SessionDep, file: UploadFile = File(...)):
#     ALLOWED_EXTENSIONS = {"xlsx", "xls", "csv"}
#     if not file.filename.lower().endswith(tuple(ALLOWED_EXTENSIONS)):
#         raise HTTPException(400, "Only support .xlsx/.xls/.csv")
#
#     os.makedirs(path, exist_ok=True)
#     filename = f"{file.filename.split('.')[0]}_{hashlib.sha256(uuid.uuid4().bytes).hexdigest()[:10]}.{file.filename.split('.')[1]}"
#     save_path = os.path.join(path, filename)
#     with open(save_path, "wb") as f:
#         f.write(await file.read())
#
#     def inner():
#         sheets = []
#         with get_data_engine() as conn:
#             if filename.endswith(".csv"):
#                 df = pd.read_csv(save_path, engine='c')
#                 tableName = f"sheet1_{hashlib.sha256(uuid.uuid4().bytes).hexdigest()[:10]}"
#                 sheets.append({"tableName": tableName, "tableComment": ""})
#                 column_len = len(df.dtypes)
#                 fields = []
#                 for i in range(column_len):
#                     # build fields
#                     fields.append({"name": df.columns[i], "type": str(df.dtypes[i]), "relType": ""})
#                 # create table
#                 create_table(conn, tableName, fields)
#
#                 data = [
#                     {df.columns[i]: None if pd.isna(row[i]) else (int(row[i]) if "int" in str(df.dtypes[i]) else row[i])
#                      for i in range(len(row))}
#                     for row in df.values
#                 ]
#                 # insert data
#                 insert_data(conn, tableName, fields, data)
#             else:
#                 excel_engine = 'xlrd' if filename.endswith(".xls") else 'openpyxl'
#                 df_sheets = pd.read_excel(save_path, sheet_name=None, engine=excel_engine)
#                 # build columns and data to insert db
#                 for sheet_name, df in df_sheets.items():
#                     tableName = f"{sheet_name}_{hashlib.sha256(uuid.uuid4().bytes).hexdigest()[:10]}"
#                     sheets.append({"tableName": tableName, "tableComment": ""})
#                     column_len = len(df.dtypes)
#                     fields = []
#                     for i in range(column_len):
#                         # build fields
#                         fields.append({"name": df.columns[i], "type": str(df.dtypes[i]), "relType": ""})
#                     # create table
#                     create_table(conn, tableName, fields)
#
#                     data = [
#                         {df.columns[i]: None if pd.isna(row[i]) else (
#                             int(row[i]) if "int" in str(df.dtypes[i]) else row[i])
#                          for i in range(len(row))}
#                         for row in df.values
#                     ]
#                     # insert data
#                     insert_data(conn, tableName, fields, data)
#
#         os.remove(save_path)
#         return {"filename": filename, "sheets": sheets}
#
#     return await asyncio.to_thread(inner)


# deprecated
@router.post("/uploadExcel", response_model=None, summary=f"{PLACEHOLDER_PREFIX}ds_upload_excel")
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def upload_excel(session: SessionDep, file: UploadFile = File(..., description=f"{PLACEHOLDER_PREFIX}ds_excel")):
    """
    (Deprecated) Tải file Excel/CSV lên và nạp trực tiếp thành bảng trong PostgreSQL nội bộ — quyền ws_admin.

    Chỉ nhận .xlsx/.xls/.csv (sai định dạng trả 400). Mỗi sheet thành một bảng. Đã bị thay bằng luồng
    /parseExcel + /importToDb (xem trước rồi mới import), nên endpoint này không còn khuyến nghị dùng.
    """
    ALLOWED_EXTENSIONS = {"xlsx", "xls", "csv"}
    if not file.filename.lower().endswith(tuple(ALLOWED_EXTENSIONS)):
        raise HTTPException(400, "Only support .xlsx/.xls/.csv")

    os.makedirs(path, exist_ok=True)
    filename = f"{file.filename.split('.')[0]}_{hashlib.sha256(uuid.uuid4().bytes).hexdigest()[:10]}.{file.filename.split('.')[1]}"
    save_path = os.path.join(path, filename)
    with open(save_path, "wb") as f:
        f.write(await file.read())

    def inner():
        sheets = []
        engine = get_engine_conn()
        if filename.endswith(".csv"):
            df = pd.read_csv(save_path, engine='c')
            tableName = f"sheet1_{hashlib.sha256(uuid.uuid4().bytes).hexdigest()[:10]}"
            sheets.append({"tableName": tableName, "tableComment": ""})
            insert_pg(df, tableName, engine)
        else:
            sheet_names = pd.ExcelFile(save_path).sheet_names
            for sheet_name in sheet_names:
                tableName = f"{sheet_name}_{hashlib.sha256(uuid.uuid4().bytes).hexdigest()[:10]}"
                sheets.append({"tableName": tableName, "tableComment": ""})
                # df_temp = pd.read_excel(save_path, nrows=5)
                # non_empty_cols = df_temp.columns[df_temp.notna().any()].tolist()
                df = pd.read_excel(save_path, sheet_name=sheet_name, engine='calamine')
                insert_pg(df, tableName, engine)

        # os.remove(save_path)
        return {"filename": filename, "sheets": sheets}

    return await asyncio.to_thread(inner)


def insert_pg(df, tableName, engine):
    # fix field type
    for i in range(len(df.dtypes)):
        if str(df.dtypes[i]) == 'uint64':
            df[str(df.columns[i])] = df[str(df.columns[i])].astype('string')

    conn = engine.raw_connection()
    cursor = conn.cursor()
    try:
        df.to_sql(
            tableName,
            engine,
            if_exists='replace',
            index=False
        )
        # trans csv
        output = StringIO()
        df.to_csv(output, sep='\t', header=False, index=False)
        # output.seek(0)

        # pg copy
        query = sql.SQL("COPY {} FROM STDIN WITH CSV DELIMITER E'\t'").format(
            sql.Identifier(tableName)
        )
        cursor.copy_expert(sql=query.as_string(cursor.connection), file=output)
        conn.commit()
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(400, str(e))
    finally:
        cursor.close()
        conn.close()


t_sheet = "数据表列表"
t_s_col = "Sheet名称"
t_n_col = "表名"
t_c_col = "表备注"
f_n_col = "字段名"
f_c_col = "字段备注"


@router.get("/exportDsSchema/{id}", response_model=None, summary=f"{PLACEHOLDER_PREFIX}ds_export_ds_schema")
@require_permissions(permission=SqlbotPermission(role=['ws_admin'], type='ds', keyExpression="id"))
async def export_ds_schema(session: SessionDep, id: int = Path(..., description=f"{PLACEHOLDER_PREFIX}ds_id")):
    """
    Xuất cấu trúc + chú thích của nguồn dữ liệu ``id`` ra file Excel (quyền ws_admin trên nguồn đó).

    Truyền ``id=0`` để tải file mẫu (template) trống. Ngược lại xuất danh sách bảng và cột kèm chú thích
    hiện có (nguồn không có bảng thì trả 400). Dùng để chỉnh chú thích hàng loạt offline rồi upload lại
    qua /uploadDsSchema. Trả về file .xlsx dạng stream.
    """
    # {
    #     'sheet':'', sheet name
    #     'c1_h':'', column1 column name
    #     'c2_h':'', column2 column name
    #     'c1':[], column1 data
    #     'c2':[], column2 data
    # }
    def inner():
        if id == 0:  # download template
            file_name = '批量上传备注'
            df_list = [
                {'sheet': t_sheet, 'c0_h': t_s_col, 'c1_h': t_n_col, 'c2_h': t_c_col, 'c0': ["数据表1", "数据表2"],
                 'c1': ["user", "score"],
                 'c2': ["用来存放用户信息的数据表", "用来存放用户课程信息的数据表"]},
                {'sheet': '数据表1', 'c1_h': f_n_col, 'c2_h': f_c_col, 'c1': ["id", "name"],
                 'c2': ["用户id", "用户姓名"]},
                {'sheet': '数据表2', 'c1_h': f_n_col, 'c2_h': f_c_col, 'c1': ["course", "user_id", "score"],
                 'c2': ["课程名称", "用户ID", "课程得分"]},
            ]
        else:
            ds = session.query(CoreDatasource).filter(CoreDatasource.id == id).first()
            file_name = ds.name
            tables = session.query(CoreTable).filter(CoreTable.ds_id == id).order_by(
                CoreTable.table_name.asc()).all()
            if len(tables) == 0:
                raise HTTPException(400, "No tables")

            df_list = []
            df1 = {'sheet': t_sheet, 'c0_h': t_s_col, 'c1_h': t_n_col, 'c2_h': t_c_col, 'c0': [], 'c1': [], 'c2': []}
            df_list.append(df1)
            for index, table in enumerate(tables):
                df1['c0'].append(f"Sheet{index}")
                df1['c1'].append(table.table_name)
                df1['c2'].append(table.custom_comment)

                fields = session.query(CoreField).filter(CoreField.table_id == table.id).order_by(
                    CoreField.field_index.asc()).all()
                df_fields = {'sheet': f"Sheet{index}", 'c1_h': f_n_col, 'c2_h': f_c_col, 'c1': [], 'c2': []}
                for field in fields:
                    df_fields['c1'].append(field.field_name)
                    df_fields['c2'].append(field.custom_comment)
                df_list.append(df_fields)

        # build dataframe and export
        output = io.BytesIO()

        with (pd.ExcelWriter(output, engine='xlsxwriter') as writer):
            for index, df in enumerate(df_list):
                if index == 0:
                    pd.DataFrame({df['c0_h']: df['c0'], df['c1_h']: df['c1'], df['c2_h']: df['c2']}
                                 ).to_excel(writer, sheet_name=df['sheet'], index=False)
                else:
                    pd.DataFrame({df['c1_h']: df['c1'], df['c2_h']: df['c2']}).to_excel(writer, sheet_name=df['sheet'],
                                                                                        index=False)

        output.seek(0)

        filename = f'{file_name}.xlsx'
        encoded_filename = quote(filename)
        return io.BytesIO(output.getvalue())

    # headers = {
    #     'Content-Disposition': f"attachment; filename*=UTF-8''{encoded_filename}"
    # }

    result = await asyncio.to_thread(inner)
    return StreamingResponse(
        result,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


@router.post("/uploadDsSchema/{id}", response_model=None, summary=f"{PLACEHOLDER_PREFIX}ds_upload_ds_schema")
@require_permissions(permission=SqlbotPermission(role=['ws_admin'], type='ds', keyExpression="id"))
async def upload_ds_schema(session: SessionDep, id: int = Path(..., description=f"{PLACEHOLDER_PREFIX}ds_id"),
                           file: UploadFile = File(...)):
    """
    Nhập chú thích bảng/cột hàng loạt cho nguồn dữ liệu ``id`` từ file Excel (quyền ws_admin trên nguồn đó).

    Chỉ nhận .xlsx/.xls (sai định dạng trả 400). File theo mẫu do /exportDsSchema tạo: sheet danh sách
    bảng + các sheet chú thích cột. Cập nhật ``custom_comment`` cho bảng và cột tương ứng. Lỗi parse trả
    500. Trả về true nếu thành công.
    """
    ALLOWED_EXTENSIONS = {"xlsx", "xls"}
    if not file.filename.lower().endswith(tuple(ALLOWED_EXTENSIONS)):
        raise HTTPException(400, "Only support .xlsx/.xls")

    try:
        contents = await file.read()
        excel_file = io.BytesIO(contents)

        sheet_names = pd.ExcelFile(excel_file, engine="openpyxl").sheet_names

        excel_file.seek(0)

        field_sheets = []
        table_sheet = None  # []
        for sheet in sheet_names:
            df = pd.read_excel(excel_file, sheet_name=sheet, engine="openpyxl").fillna('')
            if sheet == t_sheet:
                table_sheet = df.where(pd.notnull(df), None).to_dict(orient="records")
            else:
                field_sheets.append(
                    {'sheet_name': sheet, 'data': df.where(pd.notnull(df), None).to_dict(orient="records")})

        # print(field_sheets)

        # sheet table mapping
        sheet_table_map = {}

        # get data and update
        # update table comment
        if table_sheet and len(table_sheet) > 0:
            for table in table_sheet:
                sheet_table_map[table[t_s_col]] = table[t_n_col]
                session.query(CoreTable).filter(
                    and_(CoreTable.ds_id == id, CoreTable.table_name == table[t_n_col])).update(
                    {'custom_comment': table[t_c_col]})

        # update field comment
        if field_sheets and len(field_sheets) > 0:
            for fields in field_sheets:
                if len(fields['data']) > 0:
                    # get table id
                    table_name = sheet_table_map.get(fields['sheet_name'])
                    table = session.query(CoreTable).filter(
                        and_(CoreTable.ds_id == id, CoreTable.table_name == table_name)).first()
                    if table:
                        for field in fields['data']:
                            session.query(CoreField).filter(
                                and_(CoreField.ds_id == id,
                                     CoreField.table_id == table.id,
                                     CoreField.field_name == field[f_n_col])).update(
                                {'custom_comment': field[f_c_col]})
        session.commit()

        return True
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Parse Excel Failed: {str(e)}")


@router.post("/parseExcel", response_model=None, summary=f"{PLACEHOLDER_PREFIX}ds_parse_excel")
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def parse_excel(file: UploadFile = File(..., description=f"{PLACEHOLDER_PREFIX}ds_excel")):
    """
    Tải file Excel/CSV lên và phân tích trước (preview) cấu trúc từng sheet — quyền ws_admin.

    Chỉ nhận .xlsx/.xls/.csv (sai định dạng trả 400). Lưu file tạm và trả về ``filePath`` cùng dữ liệu
    xem trước (tên sheet, cột, kiểu, vài dòng mẫu). Bước 1 của luồng import: người dùng xác nhận/chỉnh
    kiểu cột trước khi gọi /importToDb.
    """
    ALLOWED_EXTENSIONS = {".xlsx", ".xls", ".csv"}
    if not file.filename.lower().endswith(tuple(ALLOWED_EXTENSIONS)):
        raise HTTPException(400, "Only support .xlsx/.xls/.csv")

    os.makedirs(path, exist_ok=True)
    filename = f"{file.filename.split('.')[0].split('/')[-1]}_{hashlib.sha256(uuid.uuid4().bytes).hexdigest()[:10]}.{file.filename.split('.')[-1]}"
    save_path = os.path.join(path, filename)
    with open(save_path, "wb") as f:
        f.write(await file.read())

    def inner():
        sheets_data = parse_excel_preview(save_path)
        return {
            "filePath": filename,
            "data": sheets_data
        }

    return await asyncio.to_thread(inner)


@router.post("/importToDb", response_model=None, summary=f"{PLACEHOLDER_PREFIX}ds_import_to_db")
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def import_to_db(session: SessionDep, trans: Trans, import_req: ImportRequest):
    """
    Nạp dữ liệu từ file đã parse vào PostgreSQL nội bộ thành các bảng — quyền ws_admin.

    Body ``ImportRequest`` gồm ``filePath`` (từ /parseExcel) và danh sách sheet kèm cấu hình cột (tên,
    kiểu). File không tồn tại trả 400. Mỗi sheet tạo một bảng ``excel_<tên>_<hash>`` và nạp dữ liệu bằng
    COPY. Bước 2 của luồng import. Trả về danh sách bảng đã tạo kèm số dòng.
    """
    save_path = os.path.join(path, import_req.filePath)
    if not os.path.exists(save_path):
        raise HTTPException(400, "File not found")

    def inner():
        try:
            results = import_sheets_to_db(save_path, import_req.sheets)
        except ExcelImportError as e:
            raise HTTPException(500, f"{trans('i18n_ds_upload_error')}: {e.message}")
        return {"filename": import_req.filePath, "sheets": results}

    return await asyncio.to_thread(inner)


@router.post(
    "/createFromExcel",
    response_model=CreateFromExcelResponse,
    summary=f"{PLACEHOLDER_PREFIX}ds_create_from_excel"
)
@system_log(LogConfig(
    operation_type=OperationType.CREATE,
    module=OperationModules.DATASOURCE,
    result_id_expr="dsId"
))
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def create_from_excel(
    session: SessionDep,
    trans: Trans,
    user: CurrentUser,
    file: UploadFile = File(..., description=f"{PLACEHOLDER_PREFIX}ds_excel"),
    name: str = Form(..., description=f"{PLACEHOLDER_PREFIX}ds_name"),
    sheetNames: List[str] = Form([], description=f"{PLACEHOLDER_PREFIX}ds_sheet_names"),
    description: str = Form('', description=f"{PLACEHOLDER_PREFIX}ds_description")
):
    """
    Tạo nguồn dữ liệu từ file Excel/CSV trong đúng một lời gọi.
    Ghép từ 3 hàm /parseExcel, /importToDb, /add
    """
    # Kiểm tra có tồn tại tên nguồn dữ liệu trùng không
    check_name(session, trans, user, CoreDatasource(name=name))

    parsed = await parse_excel(file=file)

    # Lọc sheet theo sheetNames nếu có, hoặc lấy tất cả nếu là CSV  
    if file.filename.lower().endswith('.csv') or not sheetNames:
        sheets = parsed["data"]
    else:
        sheets = [sheet for sheet in parsed["data"] if sheet["sheetName"] in sheetNames]
        if len(sheets) != len(set(sheetNames)):
            raise HTTPException(
                400, f"Sheet not found. Available: {', '.join(s['sheetName'] for s in parsed['data'])}"
            )

    imported = await import_to_db(session=session, trans=trans, import_req=ImportRequest(
        filePath=parsed["filePath"],
        sheets=[
            SheetFields(
                sheetName=s["sheetName"],
                fields=[FieldInfo(**f) for f in s["fields"]]
            )
            for s in sheets
        ],
    ))

    tables = [
        CoreTable(table_name=s["tableName"], table_comment=s["tableComment"])
        for s in imported["sheets"]
    ]

    create_obj = CreateDatasource(
        name=name, description=description, type="excel",
        configuration={"filename": imported["filename"], "sheets": imported["sheets"]},
        tables=tables
    )

    _normalize_ds_configuration(create_obj)
    ds = await create_ds(session, trans, user, create_obj)

    return CreateFromExcelResponse(
        dsId=ds.id, name=ds.name,
        tables=[
            CreatedExcelTable(
                tableId=t.id,
                tableName=t.table_name,
                sheetName=s["sheetName"],
                rows=s["rows"]
            ) for t, s in zip(tables, imported["sheets"])
        ],
    )


def _conflict_error(ds: CoreDatasource) -> HTTPException:
    """Dựng lỗi 409 mang theo đủ thông tin để hệ ngoài tự phân biệt gọi trùng với lỗi thật."""
    return HTTPException(status_code=409, detail={
        "code": "DATASOURCE_NAME_EXISTS",
        "message": f"Datasource name already exists: {ds.name}",
        "dsId": ds.id,
        "status": ds.status,
    })


def _bad_request(e: ExcelImportError) -> HTTPException:
    """Dịch lỗi kiểm URL thành 400 mang theo ``code`` để hệ ngoài phân loại bằng máy.

    Dùng lại đúng bộ mã của ``remote_file`` chứ không đặt bộ mã riêng cho HTTP: cùng một hỏng hóc
    có thể lộ ra ở pha đồng bộ hoặc ở callback tùy thời điểm phát hiện, mà hai nơi trả hai tên khác
    nhau cho cùng một chuyện thì phía đối tác phải viết hai nhánh xử lý.
    """
    return HTTPException(status_code=400, detail={"code": e.error_code, "message": e.message})


@router.post(
    "/createFromExcelAsync",
    response_model=CreateFromExcelAcceptedResponse,
    status_code=202,
    summary=f"{PLACEHOLDER_PREFIX}ds_create_from_excel_async"
)
@system_log(LogConfig(
    operation_type=OperationType.CREATE,
    module=OperationModules.DATASOURCE,
    result_id_expr="dsId"
))
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def create_from_excel_async(
    session: SessionDep,
    user: CurrentUser,
    req: CreateFromExcelUrlRequest,
):
    """
    Tạo nguồn dữ liệu từ file Excel/CSV ở object storage, TRẢ NGAY và nạp ở nền — quyền ws_admin.

    Bản bất đồng bộ của /createFromExcel. Nhận một URL ký sẵn (presigned) trỏ tới file thay vì nhận
    bytes: việc tải file, đọc file, tạo bảng và đồng bộ metadata đều chạy ở thread nền, kết quả báo
    về bằng một callback HTTP. Trả 202 kèm ``dsId`` ngay.

    Nguồn dữ liệu tồn tại ngay từ lúc trả 202 nhưng ở trạng thái ``Importing`` nên chưa vào hỏi đáp
    được; nó chuyển sang ``Success`` hoặc ``Failed`` khi worker xong.

    Ranh giới giữa hai pha đã DỊCH so với bản multipart, và đây là điều dễ hiểu nhầm nhất ở endpoint
    này. Trước kia pha đồng bộ cầm sẵn file nên đối chiếu được tên sheet; nay nó chưa có file, nên
    chỉ giữ lại được những phép kiểm suy ra từ chính chuỗi URL (scheme, host trong allowlist, đuôi
    file, hạn chữ ký) cộng một phép thăm dò một byte. Tên sheet gõ sai vì thế về bằng callback thất
    bại chứ không còn là 400.

    Phép thăm dò chỉ chặn khi nguồn trả lời DỨT KHOÁT — 403/404 hoặc dung lượng vượt trần. Timeout
    hay 5xx thì vẫn nhận việc: chặn ở đó là kéo đúng cái rủi ro truyền tải chậm trở lại pha đồng bộ,
    thứ mà cả thiết kế này sinh ra để tránh.
    """
    name = (req.name or '').strip()
    if not name:
        raise HTTPException(400, "Datasource name is required")

    # Kiểm URL bằng những gì đọc được từ chính chuỗi, không chạm mạng. Làm trước cả việc tra tên
    # trùng vì nó rẻ hơn một câu SELECT.
    try:
        src = validate_source_url(req.fileUrl)
    except ExcelImportError as e:
        raise _bad_request(e)

    # Vòng kiểm tra trùng tên SỚM, chưa cần khóa: chặn ở đây thì không phải đi hỏi object storage.
    # Vẫn phải kiểm lại lần nữa dưới khóa vì giữa hai thời điểm này có thể chen request khác.
    existing = find_conflicting_ds(session, user.oid, name)
    if existing is not None:
        raise _conflict_error(existing)

    if settings.EXCEL_DOWNLOAD_PROBE_ENABLED:
        try:
            # ``to_thread`` bắt buộc: probe là lời gọi mạng ĐỒNG BỘ, chạy thẳng trong ``async def``
            # là chặn cả event loop của tiến trình trong suốt thời gian chờ.
            await asyncio.to_thread(probe_source, src)
        except ExcelImportError as e:
            raise _bad_request(e)

    # Đặt TRƯỚC tên file mà worker sẽ ghi vào. Pha này không tạo ra file nào; cái tên chỉ là chỗ hẹn
    # giữa hai bên. Phần gốc lấy từ object key đã khử ký tự lái đường dẫn, chỉ để đọc log cho dễ —
    # định danh thật nằm ở chuỗi băm ngẫu nhiên.
    os.makedirs(path, exist_ok=True)
    filename = f"{safe_stem(src.object_key)}_{hashlib.sha256(uuid.uuid4().bytes).hexdigest()[:10]}.{src.extension}"
    save_path = os.path.join(path, filename)

    # Một transaction duy nhất: khóa → kiểm lại → dòng nguồn dữ liệu → dòng job. Hai dòng phải
    # cùng sinh ra hoặc cùng không: có nguồn dữ liệu mà không có job là một dòng Importing vĩnh
    # viễn không ai nhặt; có job mà không có nguồn dữ liệu là worker chạy vào hư không.
    try:
        session.rollback()  # đóng transaction ngầm do các câu SELECT ở trên mở, để khóa giữ đúng phạm vi

        # Chờ khóa bằng cách thử-rồi-ngủ, KHÔNG dùng bản chờ sẵn của Postgres: câu lệnh chờ là lời
        # gọi DB đồng bộ, đứng im trong ``async def`` là chặn cả event loop của tiến trình. ``await
        # asyncio.sleep`` giữa hai lần thử thì ngược lại — nhường quyền điều khiển, các request khác
        # vẫn chạy. Hầu như luôn chiếm được ngay ở lần thử đầu; vòng lặp chỉ dùng tới khi trùng tên.
        lock_key = f"sqlbot:ds:name:{user.oid if user.oid is not None else 1}:{name}"
        deadline = time.monotonic() + settings.EXCEL_IMPORT_LOCK_TIMEOUT_MS / 1000
        while not try_acquire_xact_lock(session, lock_key):
            session.rollback()
            if time.monotonic() >= deadline:
                raise HTTPException(409, {
                    "code": "DATASOURCE_NAME_LOCKED",
                    "message": f"Another import for name '{name}' is in progress",
                })
            await asyncio.sleep(0.2)

        existing = find_conflicting_ds(session, user.oid, name)
        if existing is not None:
            session.rollback()
            raise _conflict_error(existing)

        ds = await create_ds_importing(session, user, name, req.description)
        # Danh sách sheet ghi xuống NGUYÊN VĂN, chưa đối chiếu: pha này không có file để đối chiếu.
        job = create_job(session, ds_id=ds.id, oid=ds.oid, create_by=user.id, ds_name=ds.name,
                         file_path=save_path, sheet_names=list(dict.fromkeys(req.sheetNames or [])),
                         file_url=src.url)
        ds_id, ds_name, job_id = ds.id, ds.name, job.id
        session.commit()
    except Exception:
        session.rollback()
        raise

    # submit SAU commit, không bao giờ trước: worker mở session riêng, ở mức READ COMMITTED nó sẽ
    # không thấy dòng job của một transaction chưa chốt.
    submit_import_job(job_id)

    return CreateFromExcelAcceptedResponse(dsId=ds_id, name=ds_name, status=DatasourceStatus.IMPORTING)


@router.get(
    "/excelImportStatus/{ds_id}",
    response_model=ExcelImportStatusResponse,
    summary=f"{PLACEHOLDER_PREFIX}ds_excel_import_status"
)
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def excel_import_status(session: SessionDep,
                              ds_id: int = Path(..., description=f"{PLACEHOLDER_PREFIX}ds_id")):
    """
    Tra trạng thái một lần import excel bất đồng bộ — quyền ws_admin.

    Đây là đường thoát khi callback thất lạc: callback chỉ bảo đảm gửi ít nhất một lần và dừng hẳn
    sau khi hết số lần thử, nên hệ ngoài cần một cách tự hỏi thay vì chờ mãi.

    Trạng thái đọc từ chính nguồn dữ liệu (``Importing``/``Success``/``Failed``) chứ không từ dòng
    job, để nguồn dữ liệu tạo bằng đường đồng bộ cũ cũng tra được.
    """
    ds = get_ds(session, ds_id)
    if ds is None:
        raise HTTPException(404, "Datasource not found")

    job = get_job_by_ds_id(session, ds_id)
    return ExcelImportStatusResponse(
        dsId=ds.id,
        name=ds.name,
        status=ds.status or DatasourceStatus.SUCCESS,
        errorCode=job.error_code if job else None,
        errorMessage=job.error_message if job else None,
        tableCount=len(get_tables_by_ds_id(session, ds_id) or []),
    )


@router.post("/resendExcelCallback/{ds_id}", response_model=bool, include_in_schema=False)
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def resend_excel_callback(session: SessionDep,
                                ds_id: int = Path(..., description=f"{PLACEHOLDER_PREFIX}ds_id")):
    """
    Bắn lại callback của một lần import — dùng cho vận hành, không công bố ra ngoài.

    Cần vì callback dừng hẳn ở trạng thái ``dead`` sau khi hết số lần thử. Khi nguyên nhân đã được
    xử lý (đối tác sập, cấu hình URL sai) thì đây là cách đưa lá thư cũ trở lại hàng đợi mà không
    phải sửa DB bằng tay.
    """
    job = get_job_by_ds_id(session, ds_id)
    if job is None:
        raise HTTPException(404, "Import job not found")
    return resend_callback(job.id)
