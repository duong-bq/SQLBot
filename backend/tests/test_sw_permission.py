"""Unit test cho module hợp nhất quyền SW (`apps/chat/permission/sw_permission.py`).

Không cần DB thật: `get_user_permissions` được monkeypatch trả dòng quyền giả lập, session là stub
chỉ phục vụ truy vấn tên bảng trong `_warn_case_mismatches`. Trọng tâm là NGỮ NGHĨA sàng lọc
(datasource → lĩnh vực → bảng/cột) và tính tất định của phép viết lại SQL theo scope — hai thứ
mang tính access control nên phải khoá chặt bằng test thay vì tin vào đọc code.
"""

from types import SimpleNamespace

import pytest

import apps.chat.permission.sw_permission as swp
from apps.chat.permission.sw_permission import apply_scope_to_sql, resolve_sw_permission


DS = SimpleNamespace(id=7, type="pg")


def make_row(table, scope, domain_code=None, domain_name=None, ds_id="7", is_admin=False,
             form_uuid="form-1"):
    """Dựng một dòng `ai_user_permissions` giả lập với đúng các thuộc tính module tiêu thụ."""
    queries = []
    if scope is not None:
        queries = [{"datasourceId": ds_id, "datasourceType": "pg", "query": scope}]
    return SimpleNamespace(
        database_table_name=table,
        domain_code=domain_code,
        domain_name=domain_name,
        queries=queries,
        is_admin=is_admin,
        form_uuid=form_uuid,
    )


class StubSession:
    """Session giả chỉ trả lời truy vấn tên bảng của `_warn_case_mismatches`."""

    def __init__(self, core_names=()):
        self._core_names = list(core_names)

    def exec(self, _stmt):
        return SimpleNamespace(all=lambda: list(self._core_names))


@pytest.fixture
def patch_rows(monkeypatch):
    """Trả về hàm cài đặt danh sách dòng quyền giả cho `get_user_permissions`."""

    def _install(rows):
        monkeypatch.setattr(swp, "get_user_permissions", lambda _s, _a: rows)

    return _install


@pytest.fixture
def warnings_log(monkeypatch):
    """Bắt các warning log của module để test hành vi cảnh báo."""
    captured = []
    monkeypatch.setattr(swp.SQLBotLogUtil, "warning", captured.append)
    return captured


# ---------- resolve_sw_permission: các nhánh không siết ----------

def test_khong_co_dong_quyen_thi_khong_siet(patch_rows):
    patch_rows([])
    decision = resolve_sw_permission(StubSession(), "u1", None, DS)
    assert decision.restricted is False


def test_admin_sw_thi_khong_siet(patch_rows):
    patch_rows([make_row("t1", "SELECT * FROM t1", is_admin=True)])
    decision = resolve_sw_permission(StubSession(), "u1", None, DS)
    assert decision.restricted is False


# ---------- Sàng datasource ----------

def test_bang_chi_duoc_phep_tren_datasource_co_query(patch_rows):
    patch_rows([
        make_row("t_ok", "SELECT * FROM t_ok", ds_id="7"),
        make_row("t_khac_ds", "SELECT * FROM t_khac_ds", ds_id="8", form_uuid="form-2"),
        make_row("t_khong_query", None, form_uuid="form-3"),
    ])
    decision = resolve_sw_permission(StubSession(), "u1", None, DS)
    assert decision.restricted is True
    assert decision.allowed_tables == ["t_ok"]


def test_so_khop_datasource_id_dang_chuoi_exact(patch_rows):
    # id 7 (int trong CoreDatasource) phải khớp "7" và không khớp "70".
    patch_rows([make_row("t1", "SELECT * FROM t1", ds_id="70")])
    decision = resolve_sw_permission(StubSession(), "u1", None, DS)
    assert decision.allowed_tables == []


# ---------- Sàng lĩnh vực ----------

def test_loc_theo_dung_ma_linh_vuc(patch_rows):
    patch_rows([
        make_row("t_giaoduc", "SELECT * FROM t_giaoduc", domain_code="GD", domain_name="Giáo dục"),
        make_row("t_yte", "SELECT * FROM t_yte", domain_code="YT", domain_name="Y tế",
                 form_uuid="form-2"),
        make_row("t_khong_linhvuc", "SELECT * FROM t_khong_linhvuc", form_uuid="form-3"),
    ])
    decision = resolve_sw_permission(StubSession(), "u1", "GD", DS)
    # Dòng không gắn lĩnh vực bị loại khi có sàng lĩnh vực.
    assert decision.allowed_tables == ["t_giaoduc"]
    # available_domains tính TRƯỚC sàng để báo lỗi liệt kê được mọi lựa chọn, sắp theo mã.
    assert decision.available_domains == [
        {"code": "GD", "name": "Giáo dục"},
        {"code": "YT", "name": "Y tế"},
    ]


def test_khong_gui_ma_linh_vuc_thi_lay_het_bang_duoc_phep(patch_rows):
    patch_rows([
        make_row("t_giaoduc", "SELECT * FROM t_giaoduc", domain_code="GD"),
        make_row("t_khong_linhvuc", "SELECT * FROM t_khong_linhvuc", form_uuid="form-2"),
    ])
    decision = resolve_sw_permission(StubSession(), "u1", None, DS)
    assert decision.allowed_tables == ["t_giaoduc", "t_khong_linhvuc"]


def test_ma_linh_vuc_sai_thi_phieu_rong_nhung_van_biet_linh_vuc_hop_le(patch_rows):
    patch_rows([make_row("t1", "SELECT * FROM t1", domain_code="GD", domain_name="Giáo dục")])
    decision = resolve_sw_permission(StubSession(), "u1", "XX", DS)
    assert decision.restricted is True
    assert decision.allowed_tables == []
    assert decision.available_domains == [{"code": "GD", "name": "Giáo dục"}]


# ---------- Scope query và quyền cột ----------

def test_scope_khong_parse_duoc_thi_loai_bang(patch_rows, warnings_log):
    patch_rows([make_row("t1", "SELEKT nonsense FROM FROM")])
    decision = resolve_sw_permission(StubSession(), "u1", None, DS)
    assert decision.allowed_tables == []
    assert any("không" in msg and "parse" in msg for msg in warnings_log)


def test_trung_bang_khac_scope_thi_giu_form_dau_tien(patch_rows, warnings_log):
    patch_rows([
        make_row("t1", "SELECT a FROM t1 WHERE x = 1", form_uuid="form-1"),
        make_row("t1", "SELECT a FROM t1 WHERE x = 2", form_uuid="form-2"),
    ])
    decision = resolve_sw_permission(StubSession(), "u1", None, DS)
    assert decision.scope_queries["t1"] == "SELECT a FROM t1 WHERE x = 1"
    assert any("nhiều scope" in msg for msg in warnings_log)


def test_projection_tuong_minh_thanh_quyen_cot_ton_trong_alias(patch_rows):
    patch_rows([make_row("t1", "SELECT ho_ten, nam_sinh AS ns FROM t1 WHERE p = '01'")])
    decision = resolve_sw_permission(StubSession(), "u1", None, DS)
    # Tên cột ĐẦU RA của derived table: alias thay tên gốc.
    assert decision.allowed_columns["t1"] == {"ho_ten", "ns"}


def test_select_star_nghia_la_moi_cot(patch_rows):
    patch_rows([make_row("t1", "SELECT * FROM t1 WHERE p = '01'")])
    decision = resolve_sw_permission(StubSession(), "u1", None, DS)
    assert "t1" not in decision.allowed_columns
    assert decision.scope_queries["t1"] == "SELECT * FROM t1 WHERE p = '01'"


# ---------- Cảnh báo lệch hoa-thường ----------

def test_ten_bang_lech_hoa_thuong_chi_canh_bao_khong_cap_quyen(patch_rows, warnings_log):
    patch_rows([make_row("kdl_nhankhau_ROW_VALUES", "SELECT * FROM x")])
    session = StubSession(core_names=["KDL_NhanKhau_row_values"])
    decision = resolve_sw_permission(session, "u1", None, DS)
    # Tên vẫn nằm trong allowed_tables (so khớp exact diễn ra ở get_table_schema và sẽ trượt),
    # nhưng phải có warning để vận hành biết vì sao user không thấy bảng.
    assert "kdl_nhankhau_ROW_VALUES" in decision.allowed_tables
    assert any("hoa-thường" in msg for msg in warnings_log)


# ---------- apply_scope_to_sql ----------

SCOPES = {"NhanKhau": 'SELECT ho_ten, province_id FROM "NhanKhau" WHERE province_id = \'01\''}


def test_rewrite_bang_don_gian_thanh_derived_table():
    out = apply_scope_to_sql('SELECT ho_ten FROM "NhanKhau"', SCOPES, "pg")
    assert '(SELECT ho_ten, province_id FROM "NhanKhau" WHERE province_id = \'01\') AS "NhanKhau"' in out


def test_rewrite_giu_alias_goc_trong_join():
    out = apply_scope_to_sql(
        'SELECT t.ho_ten, d.name FROM "NhanKhau" t JOIN dm_tinh d ON t.province_id = d.id',
        SCOPES, "pg")
    assert 'AS "t"' in out
    # Bảng không có scope giữ nguyên.
    assert "dm_tinh AS d" in out
    # Tham chiếu cột qua alias vẫn resolve được.
    assert "t.ho_ten" in out


def test_rewrite_khong_dung_vao_cte_trung_ten():
    sql = 'WITH "NhanKhau" AS (SELECT 1 AS a) SELECT a FROM "NhanKhau"'
    out = apply_scope_to_sql(sql, SCOPES, "pg")
    # Tham chiếu tới CTE không bị bọc scope (không xuất hiện derived table thứ hai).
    assert out.count("province_id") == 0


def test_rewrite_boc_ca_bang_trong_cte_va_subquery():
    sql = 'WITH x AS (SELECT * FROM "NhanKhau") SELECT COUNT(*) FROM x'
    out = apply_scope_to_sql(sql, SCOPES, "pg")
    assert "province_id = '01'" in out


def test_rewrite_khong_co_scope_thi_tra_nguyen_van():
    sql = "SELECT 1"
    assert apply_scope_to_sql(sql, {}, "pg") == sql


def test_rewrite_sql_hong_thi_nem_loi():
    with pytest.raises(Exception):
        apply_scope_to_sql("", SCOPES, "pg")
