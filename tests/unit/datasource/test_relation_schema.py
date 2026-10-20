"""Test cho ``_resolve_relations``: lọc edge ``table_relation`` vào khối Foreign keys của M-Schema.

Trọng tâm là tính kín của prompt: edge chỉ được in khi cả hai đầu (bảng + cột) nằm trong tập đã
qua mọi lớp lọc (``checked``, ``table_list``, quyền cột SW). Trước đây tên bảng/cột được tra thẳng
``CoreTable``/``CoreField`` theo id nên bảng đã tắt hoặc ngoài quyền vẫn lọt vào prompt.

Hàm thuần, không cần DB: ``all_tables`` dựng tay đúng hình dạng ``t_obj`` trong ``get_table_schema``.
"""

# Import sqlbot_xpack TRƯỚC: apps.datasource.crud.datasource nằm trong một vòng import lẫn nhau,
# chỉ gỡ được khi xpack là module khởi động vòng đó.
import sqlbot_xpack  # noqa: F401  isort:skip

from apps.datasource.crud.datasource import _resolve_relations


def t_obj(table_id, name, fields):
    return {"id": table_id, "table_name": name, "schema_table": f"# Table: {name}\n", "fields": fields}


def edge(src_cell, src_port, tgt_cell, tgt_port):
    return {
        "shape": "edge",
        "source": {"cell": src_cell, "port": src_port},
        "target": {"cell": tgt_cell, "port": tgt_port},
    }


ORDERS = t_obj(1, "orders", {11: "id", 12: "customer_id"})
CUSTOMERS = t_obj(2, "customers", {21: "id", 22: "name"})
ITEMS = t_obj(3, "order_items", {31: "id", 32: "order_id"})


def test_edge_giua_hai_bang_duoc_chon_in_du_dong():
    lost, lines = _resolve_relations([edge(1, "12", 2, "21")], [ORDERS, CUSTOMERS], [1, 2])

    assert lost == []
    assert lines == ["orders.customer_id=customers.id"]


def test_edge_toi_bang_khong_co_trong_all_tables_bi_bo():
    # customers đã tắt / không có quyền → không có trong all_tables
    lost, lines = _resolve_relations([edge(1, "12", 2, "21")], [ORDERS], [1])

    assert lost == []
    assert lines == []


def test_edge_toi_cot_bi_loc_khong_in():
    # customers.id bị column_filter loại → không có trong fields
    customers = t_obj(2, "customers", {22: "name"})

    lost, lines = _resolve_relations([edge(1, "12", 2, "21")], [ORDERS, customers], [1, 2])

    assert lost == []
    assert lines == []


def test_edge_toi_bang_ngoai_top_embedding_keo_bang_vao():
    relations = [edge(3, "32", 1, "11"), edge(1, "12", 2, "21")]

    lost, lines = _resolve_relations(relations, [ORDERS, CUSTOMERS, ITEMS], [1])

    # giữ thứ tự all_tables
    assert [t["id"] for t in lost] == [2, 3]
    assert lines == ["order_items.order_id=orders.id", "orders.customer_id=customers.id"]


def test_edge_khong_cham_bang_duoc_chon_bi_bo():
    lost, lines = _resolve_relations([edge(1, "12", 2, "21")], [ORDERS, CUSTOMERS, ITEMS], [3])

    assert lost == []
    assert lines == []


def test_cell_port_kieu_chuoi_khop_nhu_int():
    # đồ thị do frontend X6 lưu lại có cell kiểu chuỗi
    relations = [edge("1", "12", "2", 21)]

    lost, lines = _resolve_relations(relations, [ORDERS, CUSTOMERS], [1])

    assert [t["id"] for t in lost] == [2]
    assert lines == ["orders.customer_id=customers.id"]


def test_edge_hong_dinh_dang_bi_bo_qua():
    relations = [
        {"shape": "edge", "target": {"cell": 2, "port": "21"}},
        {"shape": "edge", "source": None, "target": {"cell": 2, "port": "21"}},
        edge(1, "abc", 2, "21"),
        edge(None, "12", 2, "21"),
        edge(1, "12", 2, "21"),
    ]

    lost, lines = _resolve_relations(relations, [ORDERS, CUSTOMERS], [1, 2])

    assert lost == []
    assert lines == ["orders.customer_id=customers.id"]


def test_edge_trung_chi_in_mot_lan():
    relations = [edge(1, "12", 2, "21"), edge("1", 12, "2", "21")]

    _, lines = _resolve_relations(relations, [ORDERS, CUSTOMERS], [1, 2])

    assert lines == ["orders.customer_id=customers.id"]
