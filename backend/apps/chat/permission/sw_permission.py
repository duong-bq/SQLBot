"""Hợp nhất quyền khai thác dữ liệu do SW cấp (bảng `ai_user_permissions`) thành một quyết định
duy nhất cho pipeline chat.

Đây là CỬA DUY NHẤT mà mọi endpoint khai thác đi qua khi cần biết "user này được đụng vào những
bảng/cột/hàng nào trên datasource này". Toàn bộ việc sàng lọc, chuẩn hoá và parse nằm ở đây —
`llm.py` chỉ tiêu thụ kết quả, phía hooks chỉ ghi dữ liệu thô.

Nguyên tắc xuyên suốt: **thiếu thông tin thì siết, không nới.** Scope query không parse được,
bảng trùng scope mâu thuẫn, tên bảng lệch hoa-thường… đều dẫn tới BỚT quyền kèm warning log,
không bao giờ dẫn tới thêm quyền.

Ánh xạ danh tính: `ai_user_permissions.user_id` chính là `account` của user trong SQLBot
(id quản trị phía SW được dùng làm account khi đồng bộ user).
"""

from dataclasses import dataclass, field

import sqlglot
from sqlglot import expressions as exp
from sqlmodel import Session, select

from apps.datasource.models.datasource import CoreDatasource, CoreTable
from apps.hooks.crud.ai_user_permission import get_user_permissions
from common.utils.utils import SQLBotLogUtil


def _sqlglot_dialect(ds_type: str | None) -> str | None:
    """Ánh xạ loại datasource → dialect của sqlglot.

    Chép lại từ `apps.db.db.get_sqlglot_dialect` thay vì import: db.py kéo theo chuỗi import xpack
    nặng làm module này không unit-test đơn lẻ được. Bảng ánh xạ chỉ vài dòng và gần như bất biến;
    nếu db.py có thêm dialect mới thì phải bổ sung cả ở đây.
    """
    if not ds_type:
        return None
    lowered = ds_type.lower()
    if lowered in ("mysql", "doris", "starrocks"):
        return "mysql"
    if lowered == "sqlserver":
        return "tsql"
    if lowered == "hive":
        return "hive"
    return None


@dataclass
class SwPermission:
    """Quyết định quyền đã hợp nhất cho một (user, domain_code, datasource).

    - `restricted=False`: user ngoài hệ SW (không có dòng quyền nào) hoặc là admin phía SW —
      pipeline chạy như nguyên bản, các trường còn lại vô nghĩa.
    - `allowed_tables`: tên bảng vật lý được phép, so khớp EXACT phân biệt hoa-thường với
      `core_table.table_name` (cả hai phía cùng chép nguyên văn từ catalog của Postgres).
    - `scope_queries`: bảng → câu SELECT giới hạn phạm vi dữ liệu (hàng + cột). SQL sinh ra sẽ bị
      viết lại để đọc qua derived table này thay vì bảng vật lý.
    - `allowed_columns`: bảng → tập TÊN CỘT ĐẦU RA của scope query (tôn trọng alias). Bảng không
      có mặt trong dict nghĩa là được mọi cột (scope query dùng `SELECT *`).
    - `available_domains`: các lĩnh vực user thực có trên datasource này — dùng cho thông báo lỗi
      khi `domainCode` sai hoặc phễu bảng rỗng.
    """

    restricted: bool
    allowed_tables: list[str] = field(default_factory=list)
    scope_queries: dict[str, str] = field(default_factory=dict)
    allowed_columns: dict[str, set[str]] = field(default_factory=dict)
    available_domains: list[dict] = field(default_factory=list)


UNRESTRICTED = SwPermission(restricted=False)


def _extract_allowed_columns(parsed: exp.Expression) -> set[str] | None:
    """Rút tập tên cột đầu ra từ AST của scope query; `None` nghĩa là "mọi cột".

    Quy tắc: có bất kỳ `*` nào trong projection (kể cả `t.*`) thì coi là mọi cột — không có cách
    an toàn để liệt kê. Ngược lại lấy `alias_or_name` của từng biểu thức: đây là tên cột mà derived
    table thực sự phơi ra, tức đúng thứ DB sẽ cưỡng chế. Biểu thức phức tạp không có alias (không
    đặt tên được) thì bỏ qua — chỉ ảnh hưởng hiển thị M-Schema, không ảnh hưởng an toàn vì DB vẫn
    chặn cột ngoài projection.
    """
    select_expr = parsed
    if isinstance(parsed, exp.Union):
        # UNION: projection của vế trái quyết định tên cột đầu ra.
        select_expr = parsed.this
    if not isinstance(select_expr, exp.Select):
        return None
    columns: set[str] = set()
    for projection in select_expr.expressions:
        if isinstance(projection, exp.Star) or projection.find(exp.Star):
            return None
        name = projection.alias_or_name
        if name:
            columns.add(name)
    return columns or None


def _warn_case_mismatches(session: Session, ds: CoreDatasource, allowed_tables: list[str]) -> None:
    """Phát hiện dòng quyền có tên bảng chỉ khớp `core_table` khi bỏ phân biệt hoa-thường.

    Cố ý KHÔNG tự sửa: Postgres cho phép hai bảng cùng tên khác hoa-thường tồn tại song song, nên
    khớp lỏng có thể cấp nhầm quyền sang bảng khác. Chỉ log warning để vận hành biết phía SW đang
    gửi tên lệch với catalog.
    """
    core_names = set(
        session.exec(select(CoreTable.table_name).where(CoreTable.ds_id == ds.id)).all()
    )
    lower_map: dict[str, str] = {}
    for name in core_names:
        lower_map.setdefault(name.lower(), name)
    for name in allowed_tables:
        if name not in core_names and name.lower() in lower_map:
            SQLBotLogUtil.warning(
                f"[sw_permission] Bảng '{name}' trong ai_user_permissions không khớp exact với "
                f"core_table nhưng khớp khi bỏ hoa-thường ('{lower_map[name.lower()]}') — "
                f"quyền KHÔNG được cấp, kiểm tra lại tên bảng phía SW."
            )


def resolve_sw_permission(
    session: Session,
    account: str,
    domain_code: str | None,
    ds: CoreDatasource,
) -> SwPermission:
    """Hợp nhất các dòng `ai_user_permissions` của một user thành quyết định quyền trên một datasource.

    Chuỗi sàng: dòng quyền theo `account` → giữ dòng có phần tử `queries[]` trỏ đúng datasource
    hiện tại (`datasourceId` phía SW chính là id datasource của SQLBot, so exact dạng chuỗi) →
    nếu có `domain_code` thì giữ dòng đúng lĩnh vực (dòng không có lĩnh vực bị loại). Dòng sống
    sót định nghĩa bảng được phép + scope query + cột được phép.

    Ngữ nghĩa đã chốt:
    - Không có dòng quyền nào cho account → KHÔNG siết (user nội bộ SQLBot dùng như nguyên bản).
    - `is_admin=true` → không siết.
    - Bảng chỉ được phép trên datasource D nếu form của nó có phần tử `queries[]` cho đúng D.
    - Scope query không parse được → bảng bị LOẠI hẳn (kèm warning), không phải "được mọi hàng".
    - Cùng một bảng xuất hiện ở nhiều form với scope KHÁC nhau → lấy scope của form đầu tiên theo
      thứ tự `form_uuid` (ổn định), kèm warning — chọn siết thay vì tự ghép UNION các scope.
    """
    rows = get_user_permissions(session, account)
    if not rows:
        return UNRESTRICTED
    if any(row.is_admin for row in rows):
        return UNRESTRICTED

    ds_id = str(ds.id)
    dialect = _sqlglot_dialect(ds.type)

    # Sàng 1: datasource — dòng nào không có query cho datasource này thì bảng đó vô hình ở đây.
    survivors: list[tuple[str, str, str | None, str | None]] = []  # (table, scope, dom_code, dom_name)
    for row in rows:
        scope = next(
            (
                item.get("query")
                for item in (row.queries or [])
                if str(item.get("datasourceId")) == ds_id and item.get("query")
            ),
            None,
        )
        if scope is None:
            continue
        survivors.append((row.database_table_name, scope, row.domain_code, row.domain_name))

    # Lĩnh vực user thực có trên datasource này — tính TRƯỚC sàng lĩnh vực để thông báo lỗi
    # "domainCode sai" liệt kê được các lựa chọn hợp lệ.
    seen_codes: set[str] = set()
    available_domains: list[dict] = []
    for _, _, dom_code, dom_name in survivors:
        if dom_code and dom_code not in seen_codes:
            seen_codes.add(dom_code)
            available_domains.append({"code": dom_code, "name": dom_name})
    available_domains.sort(key=lambda d: d["code"])

    # Sàng 2: lĩnh vực — chỉ khi client gửi domainCode. Dòng không gắn lĩnh vực bị loại: đã hỏi
    # theo lĩnh vực thì bảng ngoài lĩnh vực không được xuất hiện.
    if domain_code:
        survivors = [item for item in survivors if item[2] == domain_code]

    allowed_tables: list[str] = []
    scope_queries: dict[str, str] = {}
    allowed_columns: dict[str, set[str]] = {}
    for table_name, scope, _, _ in survivors:
        if table_name in scope_queries:
            if scope_queries[table_name] != scope:
                SQLBotLogUtil.warning(
                    f"[sw_permission] Bảng '{table_name}' có nhiều scope query khác nhau cho user "
                    f"'{account}' — giữ scope của form đầu tiên, bỏ qua scope sau."
                )
            continue
        try:
            parsed = sqlglot.parse_one(scope, dialect=dialect)
        except Exception as parse_error:
            SQLBotLogUtil.warning(
                f"[sw_permission] Scope query của bảng '{table_name}' (user '{account}') không "
                f"parse được, bảng bị loại khỏi phạm vi được phép: {parse_error}"
            )
            continue
        allowed_tables.append(table_name)
        scope_queries[table_name] = scope
        columns = _extract_allowed_columns(parsed)
        if columns is not None:
            allowed_columns[table_name] = columns

    _warn_case_mismatches(session, ds, allowed_tables)

    return SwPermission(
        restricted=True,
        allowed_tables=allowed_tables,
        scope_queries=scope_queries,
        allowed_columns=allowed_columns,
        available_domains=available_domains,
    )


def apply_scope_to_sql(sql: str, scope_queries: dict[str, str], ds_type: str | None = None) -> str:
    """Viết lại SQL để mọi tham chiếu tới bảng có scope đi qua derived table `(scope) AS <bảng>`.

    Tất định bằng sqlglot, không qua LLM: đây là bước cưỡng chế quyền nên không chấp nhận xác suất.
    Alias gốc được giữ nguyên (không có alias thì lấy chính tên bảng, quote để bảo toàn hoa-thường)
    nên mọi tham chiếu cột dạng `t.col` / `"Bảng".col` trong câu gốc vẫn resolve được. Tên trùng
    với CTE do chính câu SQL định nghĩa thì bỏ qua — đó không phải bảng vật lý.

    Optimizer của DB tự đẩy predicate vào trong derived table (predicate pushdown) nên không phải
    trả giá vật chất cho việc bọc.

    NÉM exception khi parse thất bại — caller phải chặn thực thi, tuyệt đối không fallback về câu
    SQL chưa bọc.
    """
    if not scope_queries:
        return sql
    dialect = _sqlglot_dialect(ds_type)
    statements = [stmt for stmt in sqlglot.parse(sql, dialect=dialect) if stmt]
    if not statements:
        raise ValueError("SQL is empty or contains no statement")

    rewritten: list[str] = []
    for stmt in statements:
        cte_names = {cte.alias_or_name for cte in stmt.find_all(exp.CTE)}

        def _wrap(node: exp.Expression) -> exp.Expression:
            if (
                isinstance(node, exp.Table)
                and node.name in scope_queries
                and node.name not in cte_names
            ):
                scope_ast = sqlglot.parse_one(scope_queries[node.name], dialect=dialect)
                alias_name = node.alias or node.name
                return exp.Subquery(
                    this=scope_ast,
                    alias=exp.TableAlias(this=exp.to_identifier(alias_name, quoted=True)),
                )
            return node

        rewritten.append(stmt.transform(_wrap).sql(dialect=dialect))
    return ";\n".join(rewritten)
