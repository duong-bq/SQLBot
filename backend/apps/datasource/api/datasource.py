import asyncio
import hashlib
import io
import os
import traceback
import uuid
import re
from io import StringIO
from typing import List, Optional
from urllib.parse import quote

import pandas as pd
from fastapi import APIRouter, File, UploadFile, HTTPException, Path, Query
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
from common.utils.utils import SQLBotLogUtil
from ..crud.datasource import get_datasource_list, check_status, create_ds, update_ds, delete_ds, getTables, getFields, \
    update_table_and_fields, getTablesByDs, chooseTables, preview, updateTable, updateField, get_ds, fieldEnum, \
    check_status_by_id, sync_single_fields
from ..crud.field import get_fields_by_table_id
from ..crud.table import get_tables_by_ds_id
from ..models.datasource import CoreDatasource, CreateDatasource, TableObj, CoreTable, CoreField, FieldObj, \
    TableSchemaResponse, ColumnSchemaResponse, PreviewResponse, ImportRequest
from ..utils.excel import parse_excel_preview, USER_TYPE_TO_PANDAS
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
        engine = get_engine_conn()
        results = []

        for sheet_info in import_req.sheets:
            sheet_name = sheet_info.sheetName
            table_name = f"excel_{filter_string(sheet_name)}_{hashlib.sha256(uuid.uuid4().bytes).hexdigest()[:10]}"
            fields = sheet_info.fields

            field_mapping = {f.fieldName: f.fieldType for f in fields}
            dtype_dict = {
                col: USER_TYPE_TO_PANDAS.get(field_mapping.get(col, 'string'), 'string')
                for col in field_mapping.keys()
            }

            try:
                if save_path.endswith(".csv"):
                    df = pd.read_csv(save_path, engine='c', dtype=dtype_dict)
                    sheet_name = "Sheet1"
                else:
                    df = pd.read_excel(save_path, sheet_name=sheet_name, engine='calamine', dtype=dtype_dict)
            except Exception as e:
                raise HTTPException(500, f"{trans('i18n_ds_upload_error')}: {str(e)}")

            conn = engine.raw_connection()
            cursor = conn.cursor()
            try:
                df.to_sql(
                    table_name,
                    engine,
                    if_exists='replace',
                    index=False
                )
                output = StringIO()
                df.to_csv(output, sep='\t', header=False, index=False)

                query = sql.SQL("COPY {} FROM STDIN WITH CSV DELIMITER E'\t'").format(
                    sql.Identifier(table_name)
                )
                cursor.copy_expert(sql=query.as_string(cursor.connection), file=output)
                conn.commit()
                results.append({
                    "sheetName": sheet_name,
                    "tableName": table_name,
                    "tableComment": "",
                    "rows": len(df)
                })
            except Exception as e:
                raise HTTPException(500, f"Insert data failed for {table_name}: {str(e)}")
            finally:
                cursor.close()
                conn.close()

        return {"filename": import_req.filePath, "sheets": results}

    return await asyncio.to_thread(inner)


# only allow chinese, a-z, A-Z, 0-9
def filter_string(text):
    pattern = r'[^\u4e00-\u9fa5a-zA-Z0-9]'
    return re.sub(pattern, '', text)
