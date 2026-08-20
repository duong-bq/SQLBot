"""Test cho ``editTableStatusByName``: bật/tắt bảng theo tên (``apps/datasource/crud/datasource.py``).

Trọng tâm là những ràng buộc mà endpoint này hứa với bên tích hợp: chỉ đụng ``checked``, không đụng
chú thích, không tràn sang nguồn dữ liệu khác, và all-or-nothing khi có tên bảng không khớp.

Dùng SQLite trong RAM — hàm được test là SQL thuần, không phụ thuộc phương ngữ PostgreSQL. Phải cấp
``id`` tường minh vì cột id khai báo ``Identity(always=True)``, SQLite không sinh giúp.
"""

# Import sqlbot_xpack TRƯỚC: apps.datasource.crud.datasource nằm trong một vòng import lẫn nhau,
# chỉ gỡ được khi xpack là module khởi động vòng đó.
import sqlbot_xpack  # noqa: F401  isort:skip

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlmodel import Session, create_engine, select

from apps.datasource.crud.datasource import update_table_status_by_name
from apps.datasource.models.datasource import CoreTable, EditTableStatusByNameRequest


@pytest.fixture()
def session():
    """Session SQLite chứa 3 bảng của nguồn 1 và 1 bảng trùng tên của nguồn 2."""
    engine = create_engine("sqlite://")
    CoreTable.__table__.create(engine)
    with Session(engine) as s:
        rows = [(1, 1, "orders", True), (2, 1, "users", True), (3, 1, "logs", False), (4, 2, "orders", True)]
        for _id, ds_id, name, checked in rows:
            s.add(CoreTable(id=_id, ds_id=ds_id, table_name=name, table_comment="",
                            custom_comment=f"cmt_{name}", checked=checked))
        s.commit()
        yield s
    engine.dispose()


def _snapshot(session, ds_id: int = 1) -> dict:
    return {t.table_name: (t.checked, t.custom_comment)
            for t in session.exec(select(CoreTable).where(CoreTable.ds_id == ds_id)).all()}


def _payload(ds_id: int, tables: list) -> EditTableStatusByNameRequest:
    return EditTableStatusByNameRequest(ds_id=ds_id, tables=tables)


class TestGhiTrangThai:
    def test_bat_va_tat_trong_mot_lan_goi(self, session):
        res = update_table_status_by_name(session, _payload(1, [
            {"table_name": "orders", "checked": False},
            {"table_name": "logs", "checked": True},
        ]))
        assert res == {"tables_updated": 2, "enabled_count": 2}
        assert _snapshot(session)["orders"][0] is False
        assert _snapshot(session)["logs"][0] is True

    def test_khong_dung_toi_chu_thich(self, session):
        """Khác ``editTable`` theo id: thiếu chú thích trong body không làm mất chú thích đang có."""
        update_table_status_by_name(session, _payload(1, [{"table_name": "orders", "checked": False}]))
        assert _snapshot(session)["orders"][1] == "cmt_orders"

    def test_bang_khong_liet_ke_thi_giu_nguyen(self, session):
        update_table_status_by_name(session, _payload(1, [{"table_name": "orders", "checked": False}]))
        snap = _snapshot(session)
        assert snap["users"][0] is True and snap["logs"][0] is False

    def test_khong_tran_sang_nguon_khac_du_trung_ten_bang(self, session):
        update_table_status_by_name(session, _payload(1, [{"table_name": "orders", "checked": False}]))
        assert _snapshot(session, ds_id=2)["orders"][0] is True

    def test_cho_phep_tat_het_va_bao_enabled_count_bang_0(self, session):
        """Tắt hết là hợp lệ (M-Schema rỗng); client tự thấy qua ``enabled_count``."""
        res = update_table_status_by_name(session, _payload(1, [
            {"table_name": "orders", "checked": False},
            {"table_name": "users", "checked": False},
            {"table_name": "logs", "checked": False},
        ]))
        assert res == {"tables_updated": 3, "enabled_count": 0}

    def test_ghi_lai_dung_gia_tri_da_dung_thi_khong_doi_gi(self, session):
        res = update_table_status_by_name(session, _payload(1, [{"table_name": "users", "checked": True}]))
        assert res == {"tables_updated": 1, "enabled_count": 2}


class TestTenKhongKhop:
    def test_tra_404_kem_danh_sach_ten_hong(self, session):
        with pytest.raises(HTTPException) as e:
            update_table_status_by_name(session, _payload(1, [
                {"table_name": "users", "checked": False},
                {"table_name": "khong_ton_tai", "checked": False},
            ]))
        assert e.value.status_code == 404
        assert e.value.detail["tables_not_found"] == ["khong_ton_tai"]

    def test_khong_ghi_gi_khi_co_ten_hong(self, session):
        """All-or-nothing: bảng hợp lệ đứng cùng request cũng không được ghi."""
        truoc = _snapshot(session)
        with pytest.raises(HTTPException):
            update_table_status_by_name(session, _payload(1, [
                {"table_name": "users", "checked": False},
                {"table_name": "khong_ton_tai", "checked": False},
            ]))
        session.rollback()
        assert _snapshot(session) == truoc

    def test_bang_cua_nguon_khac_coi_nhu_khong_ton_tai(self, session):
        with pytest.raises(HTTPException) as e:
            update_table_status_by_name(session, _payload(2, [{"table_name": "users", "checked": False}]))
        assert e.value.detail["tables_not_found"] == ["users"]


class TestValidateBody:
    def test_mang_rong_bi_chan(self):
        with pytest.raises(ValidationError, match="must not be empty"):
            _payload(1, [])

    def test_ten_bang_lap_bi_chan(self):
        """Hai phần tử cùng ghi một bản ghi thì kết quả phụ thuộc thứ tự — bắt client nói rõ ý định."""
        with pytest.raises(ValidationError, match="duplicated table_name"):
            _payload(1, [{"table_name": "orders", "checked": True},
                         {"table_name": "orders", "checked": False}])

    def test_thieu_checked_la_422_chu_khong_am_tham_bat_lai(self):
        """Bẫy của ``editTable`` theo id (SQLModel ``default=True``) không được lặp lại ở đây."""
        with pytest.raises(ValidationError):
            _payload(1, [{"table_name": "orders"}])
