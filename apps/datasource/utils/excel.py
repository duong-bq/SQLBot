import io
from dataclasses import dataclass
from typing import List, Optional

import pandas as pd

FIELD_TYPE_MAP = {
    'int64': 'int',
    'int32': 'int',
    'float64': 'float',
    'float32': 'float',
    'datetime64': 'datetime',
    'datetime64[ns]': 'datetime',
    'object': 'string',
    'string': 'string',
    'bool': 'string',
}

USER_TYPE_TO_PANDAS = {
    'int': 'int64',
    'float': 'float64',
    'datetime': 'datetime64[ns]',
    'string': 'string',
}


def infer_field_type(dtype) -> str:
    dtype_str = str(dtype)
    return FIELD_TYPE_MAP.get(dtype_str, 'string')


@dataclass(frozen=True)
class InMemoryWorkbook:
    """File Excel/CSV nằm trong RAM, kèm tên file để biết định dạng.

    Dùng cho ``/createFromExcel`` đồng bộ: file tải từ presigned URL không
    được ghi xuống đĩa. Tên file đi kèm bytes để cờ CSV không thể bị quên
    hay truyền lệch ở một trong ba hàm đọc.
    """

    data: bytes
    filename: str

    @property
    def is_csv(self) -> bool:
        """File là CSV, suy từ đuôi tên file."""
        return self.filename.lower().endswith(".csv")


WorkbookSource = str | InMemoryWorkbook


def is_csv_source(source: WorkbookSource) -> bool:
    """Nguồn là CSV: đường dẫn theo đuôi ``.csv``, bytes theo tên file đi kèm."""
    if isinstance(source, InMemoryWorkbook):
        return source.is_csv
    return source.endswith(".csv")


def open_source(source: WorkbookSource):
    """Trả về thứ pandas đọc được: đường dẫn giữ nguyên, bytes bọc ``BytesIO`` MỚI.

    Mỗi lần đọc phải gọi lại hàm này, không dùng chung một buffer. Bộ đọc
    Excel của pandas tự tua về đầu, nhưng ``read_csv`` đọc từ vị trí con trỏ
    hiện tại: luồng nạp đọc CSV hai lần (xem trước rồi nạp), nên lần hai sẽ
    ném ``EmptyDataError``. Buffer mới cho mỗi lần đọc thì không phụ thuộc
    vào hành vi riêng của từng bộ đọc.
    """
    if isinstance(source, InMemoryWorkbook):
        return io.BytesIO(source.data)
    return source


def list_sheet_names(save_path: WorkbookSource) -> List[str]:
    """Đọc DANH SÁCH TÊN sheet của file, không chạm vào dữ liệu trong ô.

    Tách riêng khỏi ``parse_excel_preview`` để pha đồng bộ của luồng import bất đồng bộ kiểm tra
    được tên sheet client gửi lên mà không phải đọc cả file: với .xlsx, ``pd.ExcelFile`` chỉ đọc
    phần mục lục trong file zip nên chi phí gần như không đổi theo kích thước file. Nhờ vậy tên
    sheet gõ sai trở thành lỗi 400 trả ngay, thay vì một callback thất bại kèm datasource rác.

    CSV không có khái niệm sheet; trả về ``["Sheet1"]`` cho khớp quy ước đặt tên của
    ``parse_excel_preview``.

    ``save_path`` là đường dẫn hoặc ``InMemoryWorkbook`` (file trong RAM).
    """
    if is_csv_source(save_path):
        return ["Sheet1"]
    return list(pd.ExcelFile(open_source(save_path)).sheet_names)


def parse_excel_preview(save_path: WorkbookSource, max_rows: int = 10, sheet_names: Optional[List[str]] = None):
    """Đọc cấu trúc và vài dòng mẫu của từng sheet trong file Excel/CSV.

    ``sheet_names`` lọc sheet cần đọc; để trống là đọc tất cả (giữ nguyên hành vi cũ). Việc lọc
    diễn ra TRƯỚC vòng ``read_excel`` nên các sheet không được chọn hoàn toàn không bị đọc — với
    workbook nhiều sheet nặng, đây là phần lớn thời gian tiết kiệm được.

    Tên sheet không tồn tại thì bị bỏ qua im lặng; người gọi tự đối chiếu bằng ``list_sheet_names``
    nếu cần báo lỗi.

    ``save_path`` là đường dẫn hoặc ``InMemoryWorkbook``; mỗi lần đọc mở nguồn mới qua
    ``open_source``.
    """
    sheets_data = []
    if is_csv_source(save_path):
        df = pd.read_csv(open_source(save_path), engine='c')
        fields = []
        for col in df.columns:
            fields.append({
                "fieldName": col,
                "fieldType": infer_field_type(df[col].dtype)
            })
        preview_df = df.head(max_rows).replace({pd.NA: None, float('nan'): None})
        preview_data = preview_df.to_dict(orient='records')
        sheets_data.append({
            "sheetName": "Sheet1",
            "fields": fields,
            "data": preview_data,
            "rows": len(df)
        })
    else:
        all_sheet_names = pd.ExcelFile(open_source(save_path)).sheet_names
        if sheet_names:
            wanted = set(sheet_names)
            all_sheet_names = [name for name in all_sheet_names if name in wanted]
        for sheet_name in all_sheet_names:
            df = pd.read_excel(open_source(save_path), sheet_name=sheet_name, engine='calamine')
            fields = []
            for col in df.columns:
                fields.append({
                    "fieldName": col,
                    "fieldType": infer_field_type(df[col].dtype)
                })
            preview_df = df.head(max_rows).replace({pd.NA: None, float('nan'): None})
            preview_data = preview_df.to_dict(orient='records')
            sheets_data.append({
                "sheetName": sheet_name,
                "fields": fields,
                "data": preview_data,
                "rows": len(df)
            })
    return sheets_data
