"""Test cho lớp dịch và kiểm tra ``extraJdbc`` (``apps/db/extra_params.py``).

Trọng tâm là những ca mà bản cũ hỏng im lặng: giá trị boolean dạng chuỗi, tên tham số trùng với
thứ hệ thống tự truyền, và giá trị có chứa dấu ``=``.
"""

import pytest
from fastapi import HTTPException

from apps.db.extra_params import build_extra_query, parse_extra_params


def _detail(exc_info) -> str:
    return str(exc_info.value.detail)


class TestParseCoBan:
    def test_chuoi_rong_tra_dict_rong(self):
        assert parse_extra_params('mysql', '') == {}
        assert parse_extra_params('mysql', None) == {}

    def test_ep_kieu_theo_bang_tham_so(self):
        out = parse_extra_params('mysql', 'charset=utf8mb4&read_timeout=30&ssl_disabled=true')
        assert out == {'charset': 'utf8mb4', 'read_timeout': 30, 'ssl_disabled': True}

    def test_boolean_dang_chuoi_khong_con_bi_hieu_nguoc(self):
        """Bản cũ truyền thẳng chuỗi "false" cho driver, mà chuỗi khác rỗng là giá trị đúng."""
        assert parse_extra_params('sqlServer', 'read_only=false')['read_only'] is False
        assert parse_extra_params('sqlServer', 'read_only=true')['read_only'] is True

    def test_gia_tri_chua_dau_bang_van_hop_le(self):
        out = parse_extra_params('mysql', "init_command=SET time_zone='+07:00'")
        assert out['init_command'] == "SET time_zone='+07:00'"

    def test_bo_qua_doan_rong_va_khoang_trang_thua(self):
        assert parse_extra_params('mysql', ' charset = utf8mb4 & ') == {'charset': 'utf8mb4'}

    def test_doan_khong_co_dau_bang_thi_400(self):
        with pytest.raises(HTTPException) as e:
            parse_extra_params('mysql', 'charset')
        assert e.value.status_code == 400
        assert 'key=value' in _detail(e)


class TestDichTenJdbc:
    def test_sqlserver_ten_camel_case_quen_dung(self):
        out = parse_extra_params('sqlServer', 'loginTimeout=30&applicationName=sqlbot')
        assert out == {'login_timeout': 30, 'appname': 'sqlbot'}

    def test_sqlserver_application_intent(self):
        assert parse_extra_params('sqlServer', 'ApplicationIntent=ReadOnly') == {'read_only': True}

    def test_mysql_character_encoding_dich_ca_gia_tri(self):
        """UTF-8 là tên IANA; MySQL gọi bộ ký tự đó là utf8mb4."""
        assert parse_extra_params('mysql', 'characterEncoding=UTF-8') == {'charset': 'utf8mb4'}

    def test_charset_chuan_hoa_theo_cach_viet_driver_doi(self):
        """Đo trên server thật: FreeTDS chết với 'utf-8' viết thường, PyMySQL không hiểu tên IANA."""
        assert parse_extra_params('sqlServer', 'charset=utf-8') == {'charset': 'UTF-8'}
        assert parse_extra_params('sqlServer', 'charset=UTF8') == {'charset': 'UTF-8'}
        assert parse_extra_params('mysql', 'charset=UTF-8') == {'charset': 'utf8mb4'}

    def test_pg_ssl_thanh_sslmode(self):
        assert parse_extra_params('pg', 'ssl=true') == {'sslmode': 'require'}


class TestTenBiCam:
    def test_ten_he_thong_tu_truyen_bi_tu_choi_kem_huong_dan(self):
        with pytest.raises(HTTPException) as e:
            parse_extra_params('sqlServer', 'timeout=10')
        assert e.value.status_code == 400
        assert "'timeout' field" in _detail(e)

    def test_sqlserver_as_dict_bi_cam_vi_doi_kieu_dong_ket_qua(self):
        with pytest.raises(HTTPException) as e:
            parse_extra_params('sqlServer', 'as_dict=true')
        assert 'row type' in _detail(e)

    def test_mysql_local_infile_van_bi_cam(self):
        """Danh sách cấm cũ khai trong DB.mysql.illegalParams phải còn hiệu lực."""
        with pytest.raises(HTTPException) as e:
            parse_extra_params('mysql', 'local_infile=1')
        assert 'security' in _detail(e)

    def test_alias_tro_toi_ten_bi_cam_thi_bao_dung_cho(self):
        with pytest.raises(HTTPException) as e:
            parse_extra_params('mysql', 'useSSL=true')
        assert "'ssl' field" in _detail(e)

    def test_tham_so_bi_ghi_de_am_tham_nay_bao_loi_ro_rang(self):
        """connect_timeout từng bị connect_args của hệ thống đè mất mà không báo gì."""
        with pytest.raises(HTTPException) as e:
            parse_extra_params('mysql', 'connect_timeout=5')
        assert "'timeout' field" in _detail(e)

    def test_pg_options_bi_giu_cho_search_path(self):
        with pytest.raises(HTTPException) as e:
            parse_extra_params('pg', 'options=-c statement_timeout=5000')
        assert 'dbSchema' in _detail(e)

    def test_tham_so_pha_tang_doc_ket_qua_bi_cam(self):
        """Cùng lý do với as_dict: tầng đọc kết quả duyệt dòng theo vị trí và tự đoán kiểu cho bytes."""
        with pytest.raises(HTTPException) as e:
            parse_extra_params('mysql', 'use_unicode=false')
        assert 'bytes' in _detail(e)
        with pytest.raises(HTTPException):
            parse_extra_params('mysql', 'binary_prefix=true')

    def test_tham_so_driver_nhan_nhung_khong_lam_gi_bi_cam(self):
        """Cả hai đều đã đo trên server thật: driver nhận rồi im lặng hoặc gãy, không giữ lời hứa.

        ``encryption`` của pymssql không đổi được byte nào trong gói PRELOGIN — phiên vẫn chạy trần;
        ``compress`` thì PyMySQL ném NotImplementedError ngay lúc mở kết nối.
        """
        for raw in ('encryption=require', 'encrypt=true'):
            with pytest.raises(HTTPException) as e:
                parse_extra_params('sqlServer', raw)
            assert 'unencrypted' in _detail(e)
        for raw in ('compress=true', 'useCompression=true'):
            with pytest.raises(HTTPException) as e:
                parse_extra_params('mysql', raw)
            assert 'not supported' in _detail(e)

    def test_doris_cam_them_read_timeout_so_voi_mysql(self):
        assert parse_extra_params('mysql', 'read_timeout=30') == {'read_timeout': 30}
        with pytest.raises(HTTPException):
            parse_extra_params('doris', 'read_timeout=30')


class TestGiaTriSaiKieu:
    def test_int_sai_dinh_dang(self):
        with pytest.raises(HTTPException) as e:
            parse_extra_params('sqlServer', 'login_timeout=ba muoi')
        assert 'integer' in _detail(e)

    def test_bool_sai_dinh_dang(self):
        with pytest.raises(HTTPException) as e:
            parse_extra_params('mysql', 'ssl_disabled=maybe')
        assert 'boolean' in _detail(e)

    def test_enum_liet_ke_gia_tri_hop_le(self):
        with pytest.raises(HTTPException) as e:
            parse_extra_params('sqlServer', 'charset=latin1')
        assert 'utf-8' in _detail(e)

    def test_ten_la_liet_ke_ten_hop_le(self):
        with pytest.raises(HTTPException) as e:
            parse_extra_params('sqlServer', 'khong_ton_tai=1')
        assert 'Unknown extra parameter' in _detail(e)
        assert 'login_timeout' in _detail(e)


class TestLoaiChuaKhaiSpec:
    def test_giu_hanh_vi_noi_nhu_cu(self):
        """dm/redshift/hive chưa khảo sát đủ tham số nên không khóa whitelist."""
        assert parse_extra_params('hive', 'foo=bar') == {'foo': 'bar'}


class TestBuildQueryChoClickHouse:
    def test_render_va_escape(self):
        assert build_extra_query('ck', 'protocol=https&verify=false') == 'protocol=https&verify=false'

    def test_escape_ky_tu_dac_biet(self):
        assert build_extra_query('ck', 'endpoint=a b') == 'endpoint=a%20b'

    def test_rong_tra_chuoi_rong(self):
        assert build_extra_query('ck', '') == ''
