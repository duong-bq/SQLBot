import datetime
import json
import uuid
from typing import List, Optional

from fastapi import HTTPException
from sqlalchemy import and_, delete, or_, text
from sqlbot_xpack.permissions.models.ds_rules import DsRules
from sqlmodel import select

from apps.datasource.crud.permission import get_column_permission_fields, get_row_permission_filters, is_normal_user
from apps.datasource.embedding.table_embedding import calc_table_embedding
from apps.datasource.utils.utils import aes_decrypt, aes_encrypt
from apps.db.constant import DB
from apps.db.db import get_tables, get_fields, exec_sql, check_connection, get_relations
from apps.db.engine import get_engine_config, get_engine_conn
from apps.system.schemas.auth import CacheName, CacheNamespace
from common.core.config import settings
from common.core.deps import SessionDep, CurrentUser, Trans
from common.utils.embedding_threads import run_save_table_embeddings, run_save_ds_embeddings
from common.utils.utils import SQLBotLogUtil, deepcopy_ignore_extra, equals_ignore_case
from common.core.sqlbot_cache import cache, clear_cache
from .table import get_tables_by_ds_id
from ..crud.field import delete_field_by_ds_id, update_field
from ..crud.table import delete_table_by_ds_id, update_table
from ..models.datasource import CoreDatasource, CreateDatasource, CoreTable, CoreField, ColumnSchema, TableObj, \
    DatasourceConf, DatasourceStatus, TableAndFields
from ..models.excel_job import ExcelImportJob
from apps.db.db import pool_manager, driver_pool_manager


def get_datasource_list(session: SessionDep, user: CurrentUser, oid: Optional[int] = None) -> List[CoreDatasource]:
    current_oid = user.oid if user.oid is not None else 1
    if user.isAdmin and oid:
        current_oid = oid
    return session.exec(
        select(CoreDatasource).where(CoreDatasource.oid == int(current_oid)).order_by(CoreDatasource.name)).all()


def get_ds(session: SessionDep, id: int):
    statement = select(CoreDatasource).where(CoreDatasource.id == id)
    datasource = session.exec(statement).first()
    return datasource


def check_status_by_id(session: SessionDep, trans: Trans, ds_id: int, is_raise: bool = False):
    ds = session.get(CoreDatasource, ds_id)
    if ds is None:
        if is_raise:
            raise HTTPException(status_code=500, detail=trans('i18n_ds_invalid'))
        return False
    return check_status(session, trans, ds, is_raise)


def check_status(session: SessionDep, trans: Trans, ds: CoreDatasource, is_raise: bool = False):
    return check_connection(trans, ds, is_raise)


def usable_ds_condition():
    """Điều kiện lọc các nguồn dữ liệu ĐEM RA HỎI ĐÁP ĐƯỢC, viết theo lối loại trừ.

    Phải viết loại trừ chứ không phải ``status == 'Success'``: tập trạng thái không dùng được là
    tập đóng do code này kiểm soát, còn tập "hợp lệ" thì mở — có NULL của dữ liệu cũ, có "Success",
    và có thể có bất cứ giá trị nào ai đó thêm về sau. Lọc theo tập mở thì mỗi lần thêm một giá trị
    là một lần nguồn dữ liệu lặng lẽ biến mất.

    Nhánh ``is_(None)`` là bắt buộc và KHÔNG thừa: trong SQL, ``NULL NOT IN (...)`` cho kết quả
    UNKNOWN chứ không phải TRUE, nên nếu chỉ viết ``not_in`` thì đúng những dòng NULL — thứ mà
    migration 076 backfill nhằm cứu — lại bị loại. Giữ cả hai lớp phòng.
    """
    return or_(
        CoreDatasource.status.is_(None),
        CoreDatasource.status.not_in(DatasourceStatus.UNUSABLE),
    )


def is_ds_usable(ds: CoreDatasource) -> bool:
    """Bản kiểm tra trên đối tượng đã nạp sẵn, tương đương ``usable_ds_condition`` ở phía SQL."""
    return ds.status not in DatasourceStatus.UNUSABLE


def find_conflicting_ds(session: SessionDep, oid: Optional[int], name: str) -> Optional[CoreDatasource]:
    """Tra nguồn dữ liệu đang chiếm chỗ của ``name`` trong workspace, trả về DÒNG chứ không ném lỗi.

    ``check_name`` chỉ ném ``HTTPException(500)`` nên bên gọi không biết được id của nguồn dữ liệu
    trùng. Luồng bất đồng bộ cần chính id đó: hệ ngoài gọi lại lần hai (retry mạng, người dùng bấm
    hai lần) phải nhận về 409 kèm ``dsId`` và trạng thái, đủ để biết "cái này tôi tạo lúc nãy, đang
    nạp" thay vì tưởng mình gặp lỗi lạ.

    Cùng quy tắc lọc với ``check_name``: chuẩn hóa ``oid`` y như lúc ghi, và nguồn dữ liệu thất bại
    không tính là chiếm chỗ.
    """
    current_oid = oid if oid is not None else 1
    return session.exec(
        select(CoreDatasource).where(and_(
            CoreDatasource.name == name,
            CoreDatasource.oid == current_oid,
            CoreDatasource.status.is_distinct_from(DatasourceStatus.FAILED),
        ))
    ).first()


def check_name(session: SessionDep, trans: Trans, user: CurrentUser, ds: CoreDatasource):
    """Chặn trùng tên nguồn dữ liệu trong cùng workspace.

    Hai điểm khác bản cũ:

    - ``oid`` phải chuẩn hóa y như lúc ghi (``create_ds`` lưu 1 khi ``user.oid`` rỗng). Bản cũ so
      thẳng ``user.oid``, nên với user có oid rỗng — thường là admin, đúng nhóm cầm token tích hợp
      — mệnh đề thành phép so sánh với NULL, không khớp dòng nào, và việc chặn trùng tên coi như
      không chạy.
    - Nguồn dữ liệu ở trạng thái thất bại không tính là trùng: hệ ngoài được phép gọi lại với cùng
      tên sau một lần import hỏng.

    Lưu ý: đây vẫn chỉ là một câu SELECT, không có ràng buộc DB nào đứng sau, nên hai request song
    song cùng tên vẫn lọt cả hai. Việc tuần tự hóa do advisory lock ở tầng endpoint lo.

    Mã lỗi giữ nguyên 500 vì hàm dùng chung cho ``/datasource/add`` và ``/datasource/update``; đổi
    sang 409 ở đây là đổi hợp đồng của hai endpoint giao diện web đang dùng.
    """
    current_oid = user.oid if user.oid is not None else 1
    conditions = [
        CoreDatasource.name == ds.name,
        CoreDatasource.oid == current_oid,
        CoreDatasource.status.is_distinct_from(DatasourceStatus.FAILED),
    ]
    if ds.id is not None:
        conditions.append(CoreDatasource.id != ds.id)
    ds_list = session.query(CoreDatasource).filter(and_(*conditions)).all()
    if ds_list is not None and len(ds_list) > 0:
        raise HTTPException(status_code=500, detail=trans('i18n_ds_name_exist'))


@clear_cache(namespace=CacheNamespace.AUTH_INFO, cacheName=CacheName.DS_ID_LIST, keyExpression="user.oid")
async def create_ds(session: SessionDep, trans: Trans, user: CurrentUser, create_ds: CreateDatasource):
    ds = CoreDatasource()
    deepcopy_ignore_extra(create_ds, ds)
    check_name(session, trans, user, ds)
    ds.create_time = datetime.datetime.now()
    # status = check_status(session, ds)
    ds.create_by = user.id
    ds.oid = user.oid if user.oid is not None else 1
    ds.status = "Success"
    ds.type_name = DB.get_db(ds.type).db_name
    record = CoreDatasource(**ds.model_dump())
    session.add(record)
    session.flush()
    session.refresh(record)
    ds.id = record.id
    session.commit()

    # save tables and fields
    sync_table(session, ds, create_ds.tables)
    updateNum(session, ds)
    return ds


@clear_cache(namespace=CacheNamespace.AUTH_INFO, cacheName=CacheName.DS_ID_LIST, keyExpression="user.oid")
async def create_ds_importing(session: SessionDep, user: CurrentUser, name: str,
                              description: str = '') -> CoreDatasource:
    """Chèn MỘT dòng ``core_datasource`` giữ chỗ ở trạng thái đang nạp, không làm gì thêm.

    Vì sao không dùng lại ``create_ds``: hàm đó luôn chạy tiếp ``sync_table`` và ``updateNum``, và
    với danh sách bảng rỗng thì cả hai đều gây hại.

    - ``sync_table`` kết thúc bằng ``run_save_ds_embeddings``, sinh embedding cho một nguồn dữ liệu
      chưa có bảng nào. Worker xong sẽ sinh embedding đầy đủ lần nữa, nhưng ``save_ds_embedding``
      ghi cột ``embedding`` bằng một UPDATE trần, không có mốc phiên bản, và cả hai tác vụ nằm
      chung ``ThreadPoolExecutor`` 200 thread. Bản rỗng về sau là ghi đè mất bản đầy đủ — nguồn dữ
      liệu vẫn "thành công", hỏi đáp vẫn chạy, chỉ khâu chọn bảng theo ngữ nghĩa mất tác dụng.
    - ``updateNum`` giải mã ``configuration`` rồi ``len(conf.get('sheets'))``; thiếu khóa ``sheets``
      là ``len(None)`` → TypeError ngay trong pha đồng bộ, 202 không bao giờ trả được.

    ``configuration`` vì thế phải có ``sheets`` là list rỗng TƯỜNG MINH — không trông vào mặc định
    của ``DatasourceConf``, nơi ``sheets`` khai mặc định là chuỗi rỗng.

    KHÔNG commit: người gọi giữ quyền điều khiển transaction, vì pha đồng bộ cần gộp chung
    advisory lock + kiểm trùng tên + chèn dòng này vào một transaction duy nhất. ``flush`` là đủ để
    lấy ``id`` sinh ra.

    Vẫn giữ ``@clear_cache`` trên DS_ID_LIST: nguồn dữ liệu mới cần vào danh sách phân quyền ngay,
    việc chặn hỏi đáp do ``usable_ds_condition`` lo chứ không dựa vào cache vắng mặt.
    """
    record = CoreDatasource(
        name=name,
        description=description,
        type="excel",
        type_name=DB.get_db("excel").db_name,
        configuration=aes_encrypt(json.dumps({"filename": "", "sheets": []})).decode(),
        create_time=datetime.datetime.now(),
        create_by=user.id,
        oid=user.oid if user.oid is not None else 1,
        status=DatasourceStatus.IMPORTING,
        num="0/0",
        recommended_config=1,
    )
    session.add(record)
    session.flush()
    session.refresh(record)
    return record


def chooseTables(session: SessionDep, trans: Trans, id: int, tables: List[CoreTable]):
    ds = session.query(CoreDatasource).filter(CoreDatasource.id == id).first()
    check_status(session, trans, ds, True)
    sync_table(session, ds, tables)
    updateNum(session, ds)


def update_ds(session: SessionDep, trans: Trans, user: CurrentUser, ds: CoreDatasource):
    ds.id = int(ds.id)
    check_name(session, trans, user, ds)
    # Không gán status ở đây. Bản cũ ghi cứng "Success" cho MỌI lần sửa, nên chỉ cần đổi mô tả một
    # nguồn dữ liệu đang nạp là nó nhảy sang trạng thái dùng được trong khi worker chưa xong, rồi
    # lọt vào hỏi đáp với 0 bảng. Sửa metadata là việc của người dùng; phán về tiến độ nạp dữ liệu
    # là việc của worker.
    record = session.exec(select(CoreDatasource).where(CoreDatasource.id == ds.id)).first()
    update_data = ds.model_dump(exclude_unset=True)
    # Gạt thẳng khỏi payload, không chỉ dựa vào việc không gán ở trên: giao diện web sửa nguồn dữ
    # liệu gửi lại nguyên đối tượng vừa GET về, nên `status` nằm trong `model_fields_set` và sẽ ghi
    # đè nếu để lọt.
    update_data.pop('status', None)
    for field, value in update_data.items():
        setattr(record, field, value)
    session.add(record)
    session.commit()

    # update pool
    pool_manager.remove_pool(ds.id)
    driver_pool_manager.remove_pool(ds.id)

    run_save_ds_embeddings([ds.id])
    return ds


def update_ds_recommended_config(session: SessionDep, datasource_id: int, recommended_config: int):
    record = session.exec(select(CoreDatasource).where(CoreDatasource.id == datasource_id)).first()
    record.recommended_config = recommended_config
    session.add(record)
    session.commit()


async def delete_ds(session: SessionDep, id: int):
    term = session.exec(select(CoreDatasource).where(CoreDatasource.id == id)).first()
    if term.type == "excel":
        # drop all tables for current datasource
        engine = get_engine_conn()
        conf = DatasourceConf(**json.loads(aes_decrypt(term.configuration)))
        try:
            with engine.connect() as conn:
                for sheet in conf.sheets:
                    conn.execute(text(f'DROP TABLE IF EXISTS "{sheet["tableName"]}"'))
                conn.commit()
        finally:
            # get_engine_conn dựng hẳn một Engine kèm pool riêng mỗi lần gọi và không ai đóng hộ.
            engine.dispose()

    # Xóa luôn dòng job của luồng import bất đồng bộ. Không có bước này thì mỗi lần hệ ngoài xóa
    # một nguồn dữ liệu nạp hỏng sẽ để lại một dòng job trỏ tới id không còn tồn tại, và bảng job
    # phình lên mãi. Xóa TRƯỚC khi xóa nguồn dữ liệu để nếu có lỗi thì hai bên vẫn còn khớp nhau.
    session.execute(delete(ExcelImportJob).where(ExcelImportJob.ds_id == id))

    session.delete(term)
    session.commit()
    delete_table_by_ds_id(session, id)
    delete_field_by_ds_id(session, id)

    # update pool
    pool_manager.remove_pool(id)
    driver_pool_manager.remove_pool(id)

    if term:
        await clear_ws_ds_cache(term.oid)
    return {
        "message": f"Datasource with ID {id} deleted successfully."
    }


def getTables(session: SessionDep, id: int):
    ds = session.exec(select(CoreDatasource).where(CoreDatasource.id == id)).first()
    tables = get_tables(ds)
    return tables


def getTablesByDs(session: SessionDep, ds: CoreDatasource):
    # check_status(session, ds, True)
    tables = get_tables(ds)
    return tables


def getFields(session: SessionDep, id: int, table_name: str):
    ds = session.exec(select(CoreDatasource).where(CoreDatasource.id == id)).first()
    fields = get_fields(ds, table_name)
    return fields


def getFieldsByDs(session: SessionDep, ds: CoreDatasource, table_name: str):
    fields = get_fields(ds, table_name)
    return fields


def execSql(session: SessionDep, id: int, sql: str):
    ds = session.exec(select(CoreDatasource).where(CoreDatasource.id == id)).first()
    return exec_sql(ds, sql, True)


def sync_single_fields(session: SessionDep, trans: Trans, id: int):
    table = session.query(CoreTable).filter(CoreTable.id == id).first()
    ds = session.query(CoreDatasource).filter(CoreDatasource.id == table.ds_id).first()

    tables = getTablesByDs(session, ds)
    t_name = []
    for _t in tables:
        t_name.append(_t.tableName)

    if not table.table_name in t_name:
        raise HTTPException(status_code=500, detail=trans('i18n_table_not_exist'))

    # sync field
    fields = getFieldsByDs(session, ds, table.table_name)
    sync_fields(session, ds, table, fields)

    # do table embedding
    run_save_table_embeddings([table.id])
    run_save_ds_embeddings([ds.id])


def build_relation_graph(session: SessionDep, ds: CoreDatasource) -> list:
    """Build an X6 graph (nodes + FK edges) from the datasource's declared foreign keys.

    Mirrors the structure the frontend ER editor persists into core_datasource.table_relation:
    er-rect nodes carry ports keyed by CoreField.id; edges reference nodes by CoreTable.id
    (source/target.cell) and columns by CoreField.id (source/target.port).

    Returns an empty list when the source database declares no foreign keys (in which case
    the caller should leave table_relation untouched so behaviour is unchanged).
    """
    try:
        relations = get_relations(ds)
    except Exception as e:
        SQLBotLogUtil.warning(f"build_relation_graph: get_relations failed for ds {ds.id}: {e}")
        return []
    if not relations:
        return []

    tables = session.query(CoreTable).filter(CoreTable.ds_id == ds.id).all()
    if not tables:
        return []
    fields = session.query(CoreField).filter(CoreField.ds_id == ds.id).all()

    table_by_name = {t.table_name: t for t in tables}
    field_by_table_and_name = {}
    fields_by_table_id = {}
    for f in fields:
        field_by_table_and_name[(f.table_id, f.field_name)] = f
        fields_by_table_id.setdefault(f.table_id, []).append(f)

    cells = []
    col_count = 4  # simple grid auto-layout; user can rearrange freely afterwards
    for idx, t in enumerate(tables):
        row, col = divmod(idx, col_count)
        port_items = []
        for f in sorted(fields_by_table_id.get(t.id, []),
                        key=lambda x: x.field_index if x.field_index is not None else 0):
            port_items.append({
                "id": f.id,
                "group": "list",
                "attrs": {
                    "portNameLabel": {"text": f.field_name},
                    "portTypeLabel": {"text": f.field_type},
                },
            })
        cells.append({
            "id": t.id,
            "shape": "er-rect",
            "position": {"x": col * 320, "y": row * 400},
            "size": {"width": 180, "height": 51},
            "zIndex": idx + 1,
            "visible": True,
            "attrs": {
                "text": {"text": t.table_name},
                "label": {
                    "text": t.table_name,
                    "textAnchor": "left",
                    "refX": 34,
                    "refY": 28,
                    "textWrap": {"width": 120, "height": 24, "ellipsis": True},
                },
            },
            "ports": {"items": port_items},
        })

    z_index = len(tables) + 1
    for r in relations:
        src_t = table_by_name.get(r.srcTable)
        tgt_t = table_by_name.get(r.tgtTable)
        if src_t is None or tgt_t is None:
            continue  # FK pointing at a table the user did not select
        src_f = field_by_table_and_name.get((src_t.id, r.srcColumn))
        tgt_f = field_by_table_and_name.get((tgt_t.id, r.tgtColumn))
        if src_f is None or tgt_f is None:
            continue
        cells.append({
            "id": str(uuid.uuid4()),
            "shape": "edge",
            "attrs": {"line": {"stroke": "#DEE0E3"}},
            "source": {"cell": src_t.id, "port": str(src_f.id)},
            "target": {"cell": tgt_t.id, "port": str(tgt_f.id)},
            "zIndex": z_index,
        })
        z_index += 1

    return cells


def seed_table_relation(session: SessionDep, ds: CoreDatasource):
    """Seed core_datasource.table_relation from declared FK, only when it is currently empty.

    Never overwrites a graph the user has already edited (same one-time seeding contract as
    custom_comment). Failures are swallowed so datasource creation is never blocked.
    """
    try:
        record = session.query(CoreDatasource).filter(CoreDatasource.id == ds.id).first()
        if record is None or record.table_relation:
            return
        graph = build_relation_graph(session, record)
        if graph:
            record.table_relation = graph
            session.add(record)
            session.commit()
            ds.table_relation = graph
    except Exception as e:
        SQLBotLogUtil.warning(f"seed_table_relation failed for ds {ds.id}: {e}")


def sync_table(session: SessionDep, ds: CoreDatasource, tables: List[CoreTable]):
    id_list = []
    for item in tables:
        statement = select(CoreTable).where(and_(CoreTable.ds_id == ds.id, CoreTable.table_name == item.table_name))
        record = session.exec(statement).first()
        # update exist table, only update table_comment
        if record is not None:
            item.id = record.id
            id_list.append(record.id)

            record.table_comment = item.table_comment
            session.add(record)
            session.commit()
        else:
            # save new table
            table = CoreTable(ds_id=ds.id, checked=True, table_name=item.table_name, table_comment=item.table_comment,
                              custom_comment=item.table_comment)
            session.add(table)
            session.flush()
            session.refresh(table)
            item.id = table.id
            id_list.append(table.id)
            session.commit()

        # sync field
        fields = getFieldsByDs(session, ds, item.table_name)
        sync_fields(session, ds, item, fields)

    if len(id_list) > 0:
        session.query(CoreTable).filter(and_(CoreTable.ds_id == ds.id, CoreTable.id.not_in(id_list))).delete(
            synchronize_session=False)
        session.query(CoreField).filter(and_(CoreField.ds_id == ds.id, CoreField.table_id.not_in(id_list))).delete(
            synchronize_session=False)
        session.commit()
    else:  # delete all tables and fields in this ds
        session.query(CoreTable).filter(CoreTable.ds_id == ds.id).delete(synchronize_session=False)
        session.query(CoreField).filter(CoreField.ds_id == ds.id).delete(synchronize_session=False)
        session.commit()

    # seed table relations from declared foreign keys (one-time, only when not set yet)
    seed_table_relation(session, ds)

    # do table embedding
    run_save_table_embeddings(id_list)
    run_save_ds_embeddings([ds.id])


def sync_fields(session: SessionDep, ds: CoreDatasource, table: CoreTable, fields: List[ColumnSchema]):
    id_list = []
    for index, item in enumerate(fields):
        statement = select(CoreField).where(
            and_(CoreField.table_id == table.id, CoreField.field_name == item.fieldName))
        record = session.exec(statement).first()
        if record is not None:
            item.id = record.id
            id_list.append(record.id)

            record.field_comment = item.fieldComment
            record.field_index = index
            record.field_type = item.fieldType
            session.add(record)
            session.commit()
        else:
            field = CoreField(ds_id=ds.id, table_id=table.id, checked=True, field_name=item.fieldName,
                              field_type=item.fieldType, field_comment=item.fieldComment,
                              custom_comment=item.fieldComment, field_index=index)
            session.add(field)
            session.flush()
            session.refresh(field)
            item.id = field.id
            id_list.append(field.id)
            session.commit()

    if len(id_list) > 0:
        session.query(CoreField).filter(and_(CoreField.table_id == table.id, CoreField.id.not_in(id_list))).delete(
            synchronize_session=False)
        session.commit()


def update_table_and_fields(session: SessionDep, data: TableObj):
    update_table(session, data.table)
    for field in data.fields:
        update_field(session, field)

    # do table embedding
    run_save_table_embeddings([data.table.id])
    run_save_ds_embeddings([data.table.ds_id])


def updateTable(session: SessionDep, table: CoreTable):
    update_table(session, table)

    # do table embedding
    run_save_table_embeddings([table.id])
    run_save_ds_embeddings([table.ds_id])


def updateField(session: SessionDep, field: CoreField):
    update_field(session, field)

    # do table embedding
    run_save_table_embeddings([field.table_id])
    run_save_ds_embeddings([field.ds_id])


def update_table_comments_by_name(session: SessionDep, payload) -> dict:
    """Ghi chú thích cho nhiều bảng của một nguồn dữ liệu, định danh bảng bằng TÊN thay vì id.

    Khác nhóm ``update_table``/``update_field`` cũ ở ba điểm cố ý:
    - Chỉ ghi ``custom_comment``, không đụng ``checked`` — tránh bẫy "thiếu checked thì tự bật lại".
    - Id bảng/nguồn phân giải phía server nên embedding luôn được tính lại đúng, client không thể
      quên gửi id như nhóm cũ.
    - All-or-nothing: tra đủ tên trước, chỉ cần một tên không khớp là 404 kèm danh sách tên hỏng và
      KHÔNG ghi gì (một commit duy nhất ở cuối). Tên so khớp chính xác, phân biệt hoa thường.

    Body 404 là object phẳng (không bọc ``{"detail": ...}``) vì exception handler của repo trả
    thẳng ``exc.detail`` làm body.
    """
    names = [t.table_name for t in payload.tables]
    records = session.query(CoreTable).filter(
        and_(CoreTable.ds_id == payload.ds_id, CoreTable.table_name.in_(names))).all()
    by_name = {r.table_name: r for r in records}
    missing = [n for n in names if n not in by_name]
    if missing:
        raise HTTPException(status_code=404, detail={
            "message": "Some tables not found in datasource",
            "tables_not_found": missing,
        })

    for item in payload.tables:
        record = by_name[item.table_name]
        record.custom_comment = item.custom_comment
        session.add(record)
    session.commit()

    # do table embedding
    run_save_table_embeddings([r.id for r in records])
    run_save_ds_embeddings([payload.ds_id])
    return {"tables_updated": len(records)}


def update_field_comments_by_name(session: SessionDep, payload) -> dict:
    """Ghi chú thích cho nhiều cột của MỘT bảng, định danh bảng và cột bằng tên.

    Cùng ngữ nghĩa với ``update_table_comments_by_name`` (chỉ ghi ``custom_comment``, all-or-nothing,
    404 có cấu trúc). Bảng không tồn tại trong nguồn cũng trả 404 với ``tables_not_found`` — body
    luôn đủ cả hai danh sách để client parse một khuôn duy nhất.
    """
    table = session.query(CoreTable).filter(
        and_(CoreTable.ds_id == payload.ds_id, CoreTable.table_name == payload.table_name)).first()
    if table is None:
        raise HTTPException(status_code=404, detail={
            "message": "Table not found in datasource",
            "tables_not_found": [payload.table_name],
            "fields_not_found": [],
        })

    names = [f.field_name for f in payload.fields]
    records = session.query(CoreField).filter(
        and_(CoreField.table_id == table.id, CoreField.field_name.in_(names))).all()
    by_name = {r.field_name: r for r in records}
    missing = [n for n in names if n not in by_name]
    if missing:
        raise HTTPException(status_code=404, detail={
            "message": "Some fields not found in table",
            "tables_not_found": [],
            "fields_not_found": missing,
        })

    for item in payload.fields:
        record = by_name[item.field_name]
        record.custom_comment = item.custom_comment
        session.add(record)
    session.commit()

    # do table embedding
    run_save_table_embeddings([table.id])
    run_save_ds_embeddings([payload.ds_id])
    return {"fields_updated": len(records)}


def preview(session: SessionDep, current_user: CurrentUser, id: int, table_id: int):
    """Lấy 100 dòng dữ liệu đầu của một bảng để xem trước.

    Nhận ``table_id`` thay vì cả object bảng do client gửi: bảng luôn được nạp lại từ DB, nên phân
    quyền dòng/cột không còn phụ thuộc vào dữ liệu client tự khai. Hai hàm phân quyền chỉ đọc
    ``table.id`` nên hành vi không đổi.

    Bảng không tồn tại, không còn cột nào, hoặc bị phân quyền cắt hết cột đều trả kết quả rỗng chứ
    không ném lỗi — đây là màn hình xem trước, không phải nơi báo lỗi cấu hình.
    """
    ds = session.query(CoreDatasource).filter(CoreDatasource.id == id).first()
    # check_status(session, ds, True)

    table = session.query(CoreTable).filter(CoreTable.id == table_id).first()
    if table is None:
        return {"fields": [], "data": [], "sql": ''}

    fields = session.query(CoreField).filter(CoreField.table_id == table_id).order_by(
        CoreField.field_index.asc()).all()

    if fields is None or len(fields) == 0:
        return {"fields": [], "data": [], "sql": ''}

    where = ''
    f_list = [f for f in fields if f.checked]
    if is_normal_user(current_user):
        # column is checked, and, column permission for data.fields
        contain_rules = session.query(DsRules).all()
        f_list = get_column_permission_fields(session=session, current_user=current_user, table=table,
                                              fields=f_list, contain_rules=contain_rules)

        # row permission tree
        where_str = ''
        filter_mapping = get_row_permission_filters(session=session, current_user=current_user, ds=ds, tables=None,
                                                    single_table=table)
        if filter_mapping:
            mapping_dict = filter_mapping[0]
            where_str = mapping_dict.get('filter')
        where = (' where ' + where_str) if where_str is not None and where_str != '' else ''

    fields = [f.field_name for f in f_list]
    if fields is None or len(fields) == 0:
        return {"fields": [], "data": [], "sql": ''}

    conf = DatasourceConf(**json.loads(aes_decrypt(ds.configuration))) if ds.type != "excel" else get_engine_config()
    sql: str = ""
    if ds.type == "mysql" or ds.type == "doris" or ds.type == "starrocks" or ds.type == "hive":
        sql = f"""SELECT `{"`, `".join(fields)}` FROM `{table.table_name}` 
            {where} 
            LIMIT 100"""
    elif ds.type == "sqlServer":
        sql = f"""SELECT TOP 100 [{"], [".join(fields)}] FROM [{conf.dbSchema}].[{table.table_name}]
            {where} 
            """
    elif ds.type == "pg" or ds.type == "excel" or ds.type == "redshift" or ds.type == "kingbase":
        sql = f"""SELECT "{'", "'.join(fields)}" FROM "{conf.dbSchema}"."{table.table_name}" 
            {where} 
            LIMIT 100"""
    elif ds.type == "oracle":
        # sql = f"""SELECT "{'", "'.join(fields)}" FROM "{conf.dbSchema}"."{data.table.table_name}"
        #     {where}
        #     ORDER BY "{fields[0]}"
        #     OFFSET 0 ROWS FETCH NEXT 100 ROWS ONLY"""
        sql = f"""SELECT * FROM
                    (SELECT "{'", "'.join(fields)}" FROM "{conf.dbSchema}"."{table.table_name}"
                    {where} 
                    ORDER BY "{fields[0]}")
                    WHERE ROWNUM <= 100
                    """
    elif ds.type == "ck":
        sql = f"""SELECT "{'", "'.join(fields)}" FROM "{table.table_name}" 
            {where} 
            LIMIT 100"""
    elif ds.type == "dm":
        sql = f"""SELECT "{'", "'.join(fields)}" FROM "{conf.dbSchema}"."{table.table_name}"
            {where}
            LIMIT 100"""
    elif ds.type == "es":
        sql = f"""SELECT "{'", "'.join(fields)}" FROM "{table.table_name}"
            {where}
            LIMIT 100"""
    return exec_sql(ds, sql, True)


def fieldEnum(session: SessionDep, id: int):
    field = session.query(CoreField).filter(CoreField.id == id).first()
    if field is None:
        return []
    table = session.query(CoreTable).filter(CoreTable.id == field.table_id).first()
    if table is None:
        return []
    ds = session.query(CoreDatasource).filter(CoreDatasource.id == table.ds_id).first()
    if ds is None:
        return []

    db = DB.get_db(ds.type)
    sql = f"""SELECT DISTINCT {db.prefix}{field.field_name}{db.suffix} FROM {db.prefix}{table.table_name}{db.suffix}"""
    res = exec_sql(ds, sql, True)
    return [item.get(res.get('fields')[0]) for item in res.get('data')]


def updateNum(session: SessionDep, ds: CoreDatasource):
    all_tables = get_tables(ds) if ds.type != 'excel' else json.loads(aes_decrypt(ds.configuration)).get('sheets')
    selected_tables = get_tables_by_ds_id(session, ds.id)
    num = f'{len(selected_tables)}/{len(all_tables)}'

    record = session.exec(select(CoreDatasource).where(CoreDatasource.id == ds.id)).first()
    update_data = ds.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(record, field, value)
    record.num = num
    session.add(record)
    session.commit()


def get_table_obj_by_ds(session: SessionDep, current_user: CurrentUser, ds: CoreDatasource) -> List[TableAndFields]:
    _list: List = []
    tables = session.query(CoreTable).filter(
        and_(CoreTable.ds_id == ds.id, CoreTable.checked == True)
    ).all()
    conf = DatasourceConf(**json.loads(aes_decrypt(ds.configuration))) if ds.type != "excel" else get_engine_config()
    schema = conf.dbSchema if conf.dbSchema is not None and conf.dbSchema != "" else conf.database

    # get all field
    table_ids = [table.id for table in tables]
    all_fields = session.query(CoreField).filter(
        and_(CoreField.table_id.in_(table_ids), CoreField.checked == True)).all()
    # build dict
    fields_dict = {}
    for field in all_fields:
        if fields_dict.get(field.table_id):
            fields_dict.get(field.table_id).append(field)
        else:
            fields_dict[field.table_id] = [field]

    contain_rules = session.query(DsRules).all()
    for table in tables:
        # fields = session.query(CoreField).filter(and_(CoreField.table_id == table.id, CoreField.checked == True)).all()
        fields = fields_dict.get(table.id)

        # do column permissions, filter fields
        fields = get_column_permission_fields(session=session, current_user=current_user, table=table, fields=fields,
                                              contain_rules=contain_rules)
        _list.append(TableAndFields(schema=schema, table=table, fields=fields))
    return _list


def get_table_sample_data(ds: CoreDatasource, table_name: str, fields: list) -> str:
    """Get 3 sample rows from a table in JSON format to help AI understand the data"""
    if not fields:
        return ""

    db = DB.get_db(ds.type)
    # Get prefix/suffix for identifier quoting
    prefix = db.prefix if hasattr(db, 'prefix') else '"'
    suffix = db.suffix if hasattr(db, 'suffix') else '"'

    # Build field list with proper quoting
    field_names = []
    for field in fields[:10]:  # Limit to first 10 fields to avoid too wide results
        field_name = f"{prefix}{field.field_name}{suffix}"
        field_names.append(field_name)

    # Build LIMIT query based on database type
    if equals_ignore_case(ds.type, "sqlServer"):
        query = f"SELECT TOP 3 {','.join(field_names)} FROM {prefix}{table_name}{suffix}"
    elif equals_ignore_case(ds.type, "ck"):
        query = f"SELECT {','.join(field_names)} FROM {table_name} LIMIT 3"
    elif equals_ignore_case(ds.type, "hive"):
        query = f"SELECT {','.join(field_names)} FROM {table_name} LIMIT 3"
    elif equals_ignore_case(ds.type, "oracle"):
        query = f"SELECT {','.join(field_names)} FROM \"{table_name}\" WHERE ROWNUM <= 3"
    elif equals_ignore_case(ds.type, "dm"):
        query = f"SELECT {','.join(field_names)} FROM \"{table_name}\" WHERE ROWNUM <= 3"
    else:
        query = f"SELECT {','.join(field_names)} FROM {prefix}{table_name}{suffix} LIMIT 3"

    try:
        result = exec_sql(ds=ds, sql=query, origin_column=True)
        if result and result.get('data') and len(result['data']) > 0:
            import json
            # Truncate long string values for readability
            json_rows = []
            for row in result['data'][:3]:
                truncated_row = {}
                for key, value in row.items():
                    if value is None:
                        truncated_row[key] = None
                    elif isinstance(value, str):
                        # Truncate long strings
                        if len(value) > 100:
                            value = value[:100] + '...'
                        truncated_row[key] = value.replace('\n', ' ').replace('\r', ' ')
                    else:
                        truncated_row[key] = value
                json_rows.append(truncated_row)
            return json.dumps(json_rows, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return ""


def get_tables_sample_data(session: SessionDep, current_user: CurrentUser, ds: CoreDatasource,
                           table_list: list[str] = None) -> str:
    """Get sample data (3 rows) for all tables to help AI understand the data"""
    table_objs = get_table_obj_by_ds(session=session, current_user=current_user, ds=ds)
    if len(table_objs) == 0:
        return ""

    sample_data_parts = []
    for obj in table_objs:
        if table_list is not None and obj.table.table_name not in table_list:
            continue
        if obj.fields:
            sample = get_table_sample_data(ds, obj.table.table_name, obj.fields)
            if sample:
                sample_data_parts.append(f"# Table: {obj.table.table_name}\n{sample}")
    return "\n".join(sample_data_parts)


def get_table_schema(session: SessionDep, current_user: CurrentUser, ds: CoreDatasource, question: str,
                     embedding: bool = True, table_list: list[str] = None) -> tuple[str, list]:
    schema_str = ""
    table_objs = get_table_obj_by_ds(session=session, current_user=current_user, ds=ds)
    if len(table_objs) == 0:
        return schema_str, []
    db_name = table_objs[0].schema
    schema_str += f"【DB_ID】 {db_name}\n【Schema】\n"
    tables = []
    all_tables = []  # temp save all tables
    table_name_list = []
    for obj in table_objs:
        # 如果传入了table_list，则只处理在列表中的表
        if table_list is not None and obj.table.table_name not in table_list:
            continue

        schema_table = ''
        no_schema_types = ["mysql", "es", "sqlite", "hive", "doris", "starrocks"]
        schema_table += f"# Table: {db_name}.{obj.table.table_name}" if ds.type not in no_schema_types and db_name else f"# Table: {obj.table.table_name}"
        table_comment = ''
        if obj.table.custom_comment:
            table_comment = obj.table.custom_comment.strip()
        if table_comment == '':
            schema_table += '\n[\n'
        else:
            schema_table += f", {table_comment}\n[\n"

        if obj.fields:
            field_list = []
            for field in obj.fields:
                field_comment = ''
                if field.custom_comment:
                    field_comment = field.custom_comment.strip()
                if field_comment == '':
                    field_list.append(f"({field.field_name}:{field.field_type})")
                else:
                    field_list.append(f"({field.field_name}:{field.field_type}, {field_comment})")
            schema_table += ",\n".join(field_list)
        schema_table += '\n]\n'

        t_obj = {"id": obj.table.id, "table_name": obj.table.table_name, "schema_table": schema_table,
                 "embedding": obj.table.embedding}
        tables.append(t_obj)
        all_tables.append(t_obj)

    # 如果没有符合过滤条件的表，直接返回
    if not tables:
        return schema_str, []

    # do table embedding
    if embedding and tables and settings.TABLE_EMBEDDING_ENABLED:
        tables = calc_table_embedding(tables, question)
    # splice schema
    if tables:
        for s in tables:
            schema_str += s.get('schema_table')
            table_name_list.append(s.get('table_name'))

    # field relation
    if tables and ds.table_relation:
        relations = list(filter(lambda x: x.get('shape') == 'edge', ds.table_relation))
        if relations:
            # Complete the missing table
            # get tables in relation, remove irrelevant relation
            embedding_table_ids = [s.get('id') for s in tables]
            all_relations = list(
                filter(lambda x: x.get('source').get('cell') in embedding_table_ids or x.get('target').get(
                    'cell') in embedding_table_ids, relations))

            # get relation table ids, sub embedding table ids
            relation_table_ids = []
            for r in all_relations:
                relation_table_ids.append(r.get('source').get('cell'))
                relation_table_ids.append(r.get('target').get('cell'))
            relation_table_ids = list(set(relation_table_ids))
            # get table dict
            table_records = session.query(CoreTable).filter(CoreTable.id.in_(list(map(int, relation_table_ids)))).all()
            table_dict = {}
            for ele in table_records:
                table_dict[ele.id] = ele.table_name

            # get lost table ids
            lost_table_ids = list(set(relation_table_ids) - set(embedding_table_ids))
            # get lost table schema and splice it
            lost_tables = list(filter(lambda x: x.get('id') in lost_table_ids, all_tables))
            if lost_tables:
                for s in lost_tables:
                    schema_str += s.get('schema_table')
                    table_name_list.append(s.get('table_name'))

            # get field dict
            relation_field_ids = []
            for relation in all_relations:
                relation_field_ids.append(relation.get('source').get('port'))
                relation_field_ids.append(relation.get('target').get('port'))
            relation_field_ids = list(set(relation_field_ids))
            field_records = session.query(CoreField).filter(CoreField.id.in_(list(map(int, relation_field_ids)))).all()
            field_dict = {}
            for ele in field_records:
                field_dict[ele.id] = ele.field_name

            if all_relations:
                schema_str += '【Foreign keys】\n'
                for ele in all_relations:
                    schema_str += f"{table_dict.get(int(ele.get('source').get('cell')))}.{field_dict.get(int(ele.get('source').get('port')))}={table_dict.get(int(ele.get('target').get('cell')))}.{field_dict.get(int(ele.get('target').get('port')))}\n"

    return schema_str, table_name_list


@cache(namespace=CacheNamespace.AUTH_INFO, cacheName=CacheName.DS_ID_LIST, keyExpression="oid")
async def get_ws_ds(session, oid) -> list:
    stmt = select(CoreDatasource.id).distinct().where(CoreDatasource.oid == oid)
    db_list = session.exec(stmt).all()
    return db_list


@clear_cache(namespace=CacheNamespace.AUTH_INFO, cacheName=CacheName.DS_ID_LIST, keyExpression="oid")
async def clear_ws_ds_cache(oid):
    SQLBotLogUtil.info(f"ds cache for ws [{oid}] has been cleaned")
