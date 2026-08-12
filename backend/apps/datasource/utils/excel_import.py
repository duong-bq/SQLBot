"""Lõi thuần của việc nạp file Excel/CSV vào PostgreSQL nội bộ.

Tách khỏi ``api/datasource.py`` vì thread nền không gọi được route handler: ``require_permissions``
đọc user qua contextvar và NÉM ``RuntimeError`` khi không có request, còn handler thì là ``async def``.
Đặt ở tầng utils (không phải api) để worker import được mà không kéo theo cả router FastAPI.

Mọi hàm ở đây không chạm session của SQLBot, không chạm request, không i18n — chỉ nhận đường dẫn
file với mô tả sheet, và ném ``ExcelImportError`` mang mã lỗi để tầng gọi quyết định dịch thành
HTTP response hay thành payload callback.
"""

import hashlib
import re
import uuid
from collections.abc import Callable

import pandas as pd
from sqlalchemy import text

from apps.db.engine import get_engine_conn

from ..models.datasource import SheetFields
from .excel import USER_TYPE_TO_PANDAS

# Mã lỗi gửi kèm callback cho hệ ngoài. Chuỗi ổn định, KHÔNG đổi khi sửa câu thông báo.
ERR_READ_FILE = "EXCEL_READ_FAILED"
ERR_WRITE_TABLE = "TABLE_WRITE_FAILED"


class ExcelImportError(Exception):
    """Lỗi trong quá trình nạp file, mang theo mã lỗi cho máy đọc.

    Không dùng ``HTTPException``: exception đó ném từ thread nền thì không tầng nào của FastAPI bắt
    được, chỉ tạo traceback lạ trong log. Ngoài ra callback về hệ ngoài cần một ``error_code`` phân
    loại được bằng máy, mà ``HTTPException`` chỉ có ``detail`` là chuỗi tiếng người.
    """

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


def filter_string(sheet_name: str) -> str:
    """Bỏ mọi ký tự không phải chữ Trung/Latin/số khỏi tên sheet, để ghép thành tên bảng.

    Chuyển từ ``api/datasource.py`` sang đây khi tách lõi import; chỉ có luồng import dùng.
    """
    pattern = r'[^一-龥a-zA-Z0-9]'
    return re.sub(pattern, '', sheet_name)


def build_table_name(sheet_name: str) -> str:
    """Sinh tên bảng vật lý cho một sheet: ``excel_<tên đã lọc>_<10 ký tự hash ngẫu nhiên>``.

    Phần hash sinh mới mỗi lần nên tên bảng KHÔNG tái tạo lại được từ đầu vào. Đó là lý do luồng
    bất đồng bộ phải ghi tên bảng xuống DB ngay khi tạo xong: mất tên là bảng mồ côi vĩnh viễn,
    không câu truy vấn nào tìm lại được chủ của nó.
    """
    return f"excel_{filter_string(sheet_name)}_{hashlib.sha256(uuid.uuid4().bytes).hexdigest()[:10]}"


def import_sheets_to_db(
    save_path: str,
    sheets: list[SheetFields],
    on_table_created: Callable[[str], None] | None = None,
) -> list[dict]:
    """Nạp từng sheet thành một bảng trong PostgreSQL nội bộ, trả về mô tả các bảng đã tạo.

    ``on_table_created`` được gọi với tên bảng NGAY SAU khi bảng đó ghi xong. Luồng bất đồng bộ
    truyền vào một hàm ghi tên bảng xuống dòng job kèm commit, để khi tiến trình chết giữa chừng
    vẫn còn dấu vết dọn dẹp — bảng đã tạo thì không chờ tới cuối job mới được ghi nhận.

    Engine luôn được ``dispose`` ở cuối: ``get_engine_conn`` dựng hẳn một Engine kèm pool riêng cho
    mỗi lời gọi, không ai đóng hộ. Đường đồng bộ gọi thưa nên rò chậm, nhưng worker chạy liên tục
    thì mỗi job để lại một pool và số connection tới Postgres tăng tuyến tính cho tới khi cạn.

    Lưu ý về cách ghi dữ liệu: ``to_sql`` đã nạp trọn dataframe. Bản cũ còn một khối ``COPY`` chạy
    sau đó nhưng quên ``seek(0)`` nên COPY đọc chuỗi rỗng và nạp 0 dòng — vô hại nhưng là mìn: ai
    "sửa" bằng cách thêm ``seek(0)`` sẽ làm mọi bảng nhân đôi dữ liệu. Khối đó đã bỏ.
    """
    engine = get_engine_conn()
    results = []
    try:
        for sheet_info in sheets:
            sheet_name = sheet_info.sheetName
            table_name = build_table_name(sheet_name)

            field_mapping = {f.fieldName: f.fieldType for f in sheet_info.fields}
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
                raise ExcelImportError(ERR_READ_FILE, f"Read sheet '{sheet_name}' failed: {e}") from e

            try:
                df.to_sql(table_name, engine, if_exists='replace', index=False)
            except Exception as e:
                raise ExcelImportError(ERR_WRITE_TABLE, f"Insert data failed for {table_name}: {e}") from e

            if on_table_created is not None:
                on_table_created(table_name)

            results.append({
                "sheetName": sheet_name,
                "tableName": table_name,
                "tableComment": "",
                "rows": len(df),
            })
    finally:
        engine.dispose()

    return results


def drop_tables(table_names: list[str]) -> None:
    """Xóa các bảng vật lý đã tạo — dùng khi job thất bại hoặc khi dọn job chết.

    Mỗi bảng xóa trong một lần thử riêng: một bảng lỗi không được phép chặn việc xóa các bảng còn
    lại, vì mục đích của hàm này chính là giảm rác chứ không phải bảo đảm tính nguyên tử.
    """
    if not table_names:
        return
    engine = get_engine_conn()
    try:
        with engine.connect() as conn:
            for table_name in table_names:
                try:
                    conn.execute(text(f'DROP TABLE IF EXISTS "{table_name}"'))
                    conn.commit()
                except Exception:
                    conn.rollback()
    finally:
        engine.dispose()
