"""Test cho hai bước mới ở đầu worker import: tải file nguồn và đối chiếu tên sheet.

Chỉ nhắm vào hai hàm thuần ``_fetch_source_file`` và ``_resolve_sheets``. Phần còn lại của
``_run_import_job`` cần Postgres thật (mark_running, finish_job, outbox) nên không đụng tới ở đây.

Hai bước này là chỗ ranh giới giữa pha đồng bộ và pha nền vừa DỊCH sang. Trước kia pha đồng bộ cầm
file trong tay nên tên sheet sai là 400 trả ngay; nay nó chỉ có URL, nên phép đối chiếu lùi xuống
worker. Nếu bước đối chiếu này hỏng thì hậu quả không phải là một lỗi — nó là một callback báo
THÀNH CÔNG kèm nguồn dữ liệu thiếu bảng, vì ``parse_excel_preview`` bỏ qua im lặng tên sheet lạ.

File xlsx dựng bằng openpyxl thật thay vì mock ``pd.ExcelFile``: thứ cần kiểm là thứ tự sheet đọc
ra từ file và cách pandas nổ khi file hỏng, mock đi thì chỉ còn kiểm chính cái mock.
"""

import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import openpyxl

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from apps.datasource.task.excel_import_task import (  # noqa: E402
    ERR_CANNOT_READ,
    ERR_SHEET_NOT_FOUND,
    _fetch_source_file,
    _resolve_sheets,
)
from apps.datasource.utils import remote_file  # noqa: E402
from apps.datasource.utils.excel_import import ExcelImportError  # noqa: E402

from test_excel_remote_file import override, sigv4_query  # noqa: E402


def make_workbook(path: str, sheet_names: list[str]) -> str:
    """Dựng file .xlsx thật với đúng danh sách sheet theo thứ tự cho trước."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name in sheet_names:
        wb.create_sheet(name).append(["col_a", "col_b"])
    wb.save(path)
    return path


class ResolveSheetsTest(unittest.TestCase):
    """Đối chiếu tên sheet client gửi với mục lục thật của file."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="excel-worker-test-")

    def path(self, name: str) -> str:
        return os.path.join(self.tmpdir, name)

    def test_empty_selection_takes_every_sheet(self):
        p = make_workbook(self.path("a.xlsx"), ["Thu", "Chi", "DauTu"])
        self.assertEqual(_resolve_sheets(p, []), ["Thu", "Chi", "DauTu"])

    def test_selection_is_filtered(self):
        p = make_workbook(self.path("a.xlsx"), ["Thu", "Chi", "DauTu"])
        self.assertEqual(_resolve_sheets(p, ["Chi"]), ["Chi"])

    def test_result_follows_file_order_not_client_order(self):
        """Thứ tự lấy từ file: nó quyết định thứ tự tạo bảng, phải tái lập được giữa các lần chạy."""
        p = make_workbook(self.path("a.xlsx"), ["Thu", "Chi", "DauTu"])
        self.assertEqual(_resolve_sheets(p, ["DauTu", "Thu"]), ["Thu", "DauTu"])

    def test_unknown_sheet_raises_instead_of_being_dropped(self):
        """Bẫy chính: ``parse_excel_preview`` bỏ qua im lặng tên lạ, nên phải chặn TRƯỚC nó.

        Không có phép kiểm này thì gõ sai một tên sheet cho ra callback báo thành công kèm một
        nguồn dữ liệu thiếu bảng — hỏng mà không ai biết là đã hỏng.
        """
        p = make_workbook(self.path("a.xlsx"), ["Thu", "Chi"])
        with self.assertRaises(ExcelImportError) as ctx:
            _resolve_sheets(p, ["Thu", "ChiTieu"])
        self.assertEqual(ctx.exception.error_code, ERR_SHEET_NOT_FOUND)
        # Thông điệp phải kèm danh sách sheet có thật, nếu không đối tác không tự sửa được.
        self.assertIn("ChiTieu", ctx.exception.message)
        self.assertIn("Chi", ctx.exception.message)

    def test_csv_has_a_single_conventional_sheet(self):
        p = self.path("a.csv")
        with open(p, "w", encoding="utf-8") as f:
            f.write("a,b\n1,2\n")
        self.assertEqual(_resolve_sheets(p, []), ["Sheet1"])

    def test_corrupt_workbook_becomes_a_typed_error(self):
        """File hỏng phải ra mã lỗi phân loại được, không phải một traceback của pandas."""
        p = self.path("a.xlsx")
        with open(p, "wb") as f:
            f.write(b"this is definitely not a zip archive")
        with self.assertRaises(ExcelImportError) as ctx:
            _resolve_sheets(p, [])
        self.assertEqual(ctx.exception.error_code, ERR_CANNOT_READ)


class _Handler(BaseHTTPRequestHandler):
    """Server đóng vai MinIO, chỉ cần trả nguyên một khối bytes."""

    body = b"col_a,col_b\n1,2\n"
    hits: list = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).hits.append(self.path)
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)


class FetchSourceFileTest(unittest.TestCase):
    """Bước tải file ở đầu worker."""

    def setUp(self):
        _Handler.hits = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.tmpdir = tempfile.mkdtemp(prefix="excel-worker-test-")
        self.dest = os.path.join(self.tmpdir, "src.csv")

    def url(self, query: str = "") -> str:
        return f"http://127.0.0.1:{self.port}/bucket/bao_cao.csv" + (f"?{query}" if query else "")

    def hosts(self):
        return override(FILE_DOWNLOAD_ALLOWED_HOSTS=f"127.0.0.1:{self.port}",
                        EXCEL_DOWNLOAD_MIN_TTL_SECONDS=300)

    def test_downloads_to_the_path_the_sync_phase_reserved(self):
        with self.hosts():
            _fetch_source_file(self.url(), self.dest)
        self.assertTrue(os.path.exists(self.dest))
        with open(self.dest, "rb") as f:
            self.assertEqual(f.read(), _Handler.body)
        # File .part không được sót lại sau lượt tải thành công.
        self.assertFalse(os.path.exists(f"{self.dest}.part"))

    def test_existing_file_is_not_downloaded_again(self):
        """Job được vòng phục hồi nhặt lại: worker trước đã tải xong rồi mới chết.

        Tải lại vừa tốn thời gian vừa thường hỏng hẳn, vì tới lượt chạy thứ hai thì chữ ký của URL
        hay đã hết hạn.
        """
        with open(self.dest, "wb") as f:
            f.write(b"da co san")
        with self.hosts():
            _fetch_source_file(self.url(), self.dest)
        self.assertEqual(_Handler.hits, [])
        with open(self.dest, "rb") as f:
            self.assertEqual(f.read(), b"da co san")

    def test_url_is_revalidated_at_run_time_not_trusted_from_db(self):
        """Hạn chữ ký phải soi lại theo HIỆN TẠI: job có thể nằm hàng đợi hàng giờ trước khi chạy."""
        expired = self.url(sigv4_query(60, signed_at=None))
        with override(FILE_DOWNLOAD_ALLOWED_HOSTS=f"127.0.0.1:{self.port}",
                      EXCEL_DOWNLOAD_MIN_TTL_SECONDS=3600):
            with self.assertRaises(ExcelImportError) as ctx:
                _fetch_source_file(expired, self.dest)
        self.assertEqual(ctx.exception.error_code, remote_file.ERR_URL_EXPIRED)
        self.assertEqual(_Handler.hits, [])
        self.assertFalse(os.path.exists(self.dest))

    def test_host_removed_from_allowlist_blocks_a_queued_job(self):
        """Allowlist bị siết lại sau khi job đã vào hàng đợi thì job đó phải hỏng, không được tải."""
        with override(FILE_DOWNLOAD_ALLOWED_HOSTS="minio-hdnd.example.com"):
            with self.assertRaises(ExcelImportError) as ctx:
                _fetch_source_file(self.url(), self.dest)
        self.assertEqual(ctx.exception.error_code, remote_file.ERR_URL_HOST_NOT_ALLOWED)
        self.assertEqual(_Handler.hits, [])


if __name__ == "__main__":
    unittest.main()
