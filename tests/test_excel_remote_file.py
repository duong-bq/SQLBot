"""Test cho module tải file nguồn từ presigned URL (``apps.datasource.utils.remote_file``).

Phần kiểm URL test bằng hàm thuần. Phần thăm dò và tải thì dựng một HTTP server thật trên cổng
ngẫu nhiên thay vì mock ``httpx``: những thứ cần kiểm ở đây — server phớt lờ header ``Range``,
``Content-Length`` khai láo, redirect, phân biệt 4xx với 5xx — đều là hành vi của tầng HTTP, mock
đi thì test chỉ còn kiểm chính cái mock.

Cổng luôn là 0 (hệ điều hành tự cấp) để không bao giờ đụng backend đang chạy ở 8001/8002.
"""

import contextlib
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from apps.datasource.utils import remote_file  # noqa: E402
from apps.datasource.utils.excel_import import ExcelImportError  # noqa: E402
from common.core.config import settings  # noqa: E402


@contextlib.contextmanager
def override(**kwargs):
    """Đổi tạm giá trị trong ``settings`` rồi trả lại nguyên trạng."""
    old = {k: getattr(settings, k) for k in kwargs}
    for k, v in kwargs.items():
        setattr(settings, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(settings, k, v)


def sigv4_query(ttl_seconds: int, signed_at: datetime | None = None) -> str:
    """Dựng query string kiểu SigV4 với hạn ký cho trước."""
    signed_at = signed_at or datetime.now(timezone.utc)
    return (
        "X-Amz-Algorithm=AWS4-HMAC-SHA256"
        f"&X-Amz-Date={signed_at.strftime('%Y%m%dT%H%M%SZ')}"
        f"&X-Amz-Expires={ttl_seconds}"
        "&X-Amz-Signature=deadbeef"
    )


class ValidateSourceUrlTest(unittest.TestCase):
    """Kiểm URL bằng chính chuỗi URL, không chạm mạng."""

    HOSTS = "minio-hdnd.example.com,192.168.254.145:9000"

    def _validate(self, url: str, hosts: str | None = None):
        with override(FILE_DOWNLOAD_ALLOWED_HOSTS=hosts if hosts is not None else self.HOSTS,
                      EXCEL_DOWNLOAD_MIN_TTL_SECONDS=300):
            return remote_file.validate_source_url(url)

    def _expect(self, url: str, code: str, hosts: str | None = None):
        with self.assertRaises(ExcelImportError) as ctx:
            self._validate(url, hosts)
        self.assertEqual(ctx.exception.error_code, code)

    def test_empty_url_rejected(self):
        self._expect("", remote_file.ERR_URL_INVALID)
        self._expect("   ", remote_file.ERR_URL_INVALID)

    def test_non_http_scheme_rejected(self):
        self._expect("file:///etc/passwd", remote_file.ERR_URL_INVALID)
        self._expect("ftp://minio-hdnd.example.com/a.xlsx", remote_file.ERR_URL_INVALID)

    def test_empty_allowlist_blocks_everything(self):
        """Allowlist rỗng phải hỏng theo hướng ĐÓNG, không phải mở cho mọi host."""
        self._expect("https://minio-hdnd.example.com/b/a.xlsx",
                     remote_file.ERR_URL_HOST_NOT_ALLOWED, hosts="")

    def test_host_outside_allowlist_rejected(self):
        self._expect("https://evil.example.com/b/a.xlsx", remote_file.ERR_URL_HOST_NOT_ALLOWED)
        # Chặn luôn đường vòng kinh điển của SSRF.
        self._expect("http://169.254.169.254/latest/meta-data/a.csv",
                     remote_file.ERR_URL_HOST_NOT_ALLOWED)

    def test_allowlist_accepts_entry_written_as_full_url(self):
        src = self._validate("https://minio-hdnd.example.com/b/a.xlsx",
                             hosts="https://minio-hdnd.example.com/")
        self.assertEqual(src.host, "minio-hdnd.example.com")

    def test_allowlist_matches_host_with_port(self):
        src = self._validate("http://192.168.254.145:9000/bucket/a.csv")
        self.assertEqual(src.extension, "csv")

    def test_extension_read_from_path_not_from_raw_url(self):
        """Bẫy chính: presigned URL luôn kết thúc bằng chuỗi ký, không phải bằng đuôi file."""
        url = f"https://minio-hdnd.example.com/bucket/bao_cao.xlsx?{sigv4_query(3600)}"
        src = self._validate(url)
        self.assertEqual(src.extension, "xlsx")
        self.assertEqual(src.object_key, "/bucket/bao_cao.xlsx")

    def test_percent_encoded_path_is_decoded(self):
        url = "https://minio-hdnd.example.com/bucket/b%C3%A1o%20c%C3%A1o.xlsx"
        src = self._validate(url)
        self.assertEqual(src.extension, "xlsx")
        self.assertIn("báo cáo.xlsx", src.object_key)

    def test_bad_extension_rejected(self):
        self._expect("https://minio-hdnd.example.com/b/a.exe", remote_file.ERR_URL_BAD_EXTENSION)
        self._expect("https://minio-hdnd.example.com/b/noext", remote_file.ERR_URL_BAD_EXTENSION)
        # Đuôi hợp lệ nằm trong query chứ không nằm ở path thì vẫn phải trượt.
        self._expect("https://minio-hdnd.example.com/b/a.exe?name=a.xlsx",
                     remote_file.ERR_URL_BAD_EXTENSION)

    def test_expired_url_rejected(self):
        signed_at = datetime.now(timezone.utc) - timedelta(hours=2)
        url = f"https://minio-hdnd.example.com/b/a.xlsx?{sigv4_query(3600, signed_at)}"
        self._expect(url, remote_file.ERR_URL_EXPIRED)

    def test_url_expiring_too_soon_rejected(self):
        url = f"https://minio-hdnd.example.com/b/a.xlsx?{sigv4_query(60)}"
        self._expect(url, remote_file.ERR_URL_EXPIRED)

    def test_url_with_enough_ttl_accepted(self):
        url = f"https://minio-hdnd.example.com/b/a.xlsx?{sigv4_query(3600)}"
        src = self._validate(url)
        self.assertIsNotNone(src.expires_at)
        self.assertGreater(src.expires_at, datetime.now(timezone.utc))

    def test_url_without_sigv4_params_has_unknown_expiry(self):
        """Không nhận ra hạn ký thì coi là 'không biết', KHÔNG coi là lỗi."""
        src = self._validate("https://minio-hdnd.example.com/b/a.csv")
        self.assertIsNone(src.expires_at)

    def test_safe_label_never_leaks_signature(self):
        url = f"https://minio-hdnd.example.com/b/a.xlsx?{sigv4_query(3600)}"
        label = self._validate(url).safe_label
        self.assertNotIn("Signature", label)
        self.assertNotIn("deadbeef", label)


class SafeStemTest(unittest.TestCase):
    """Phần gốc tên file tạm được rút từ chuỗi do bên ngoài kiểm soát."""

    def test_path_traversal_neutralised(self):
        self.assertEqual(remote_file.safe_stem("/bucket/../../etc/passwd"), "passwd")
        self.assertEqual(remote_file.safe_stem("/b/..%2F..%2Fetc/shadow.xlsx"), "shadow")

    def test_separators_and_specials_removed(self):
        stem = remote_file.safe_stem("/b/a b;rm -rf/'quái lạ'.xlsx")
        self.assertNotIn("/", stem)
        self.assertNotIn(";", stem)
        self.assertNotIn(" ", stem)

    def test_falls_back_when_nothing_usable_left(self):
        self.assertEqual(remote_file.safe_stem("/b/....xlsx"), "remote")
        self.assertEqual(remote_file.safe_stem(""), "remote")

    def test_truncated(self):
        self.assertLessEqual(len(remote_file.safe_stem("/b/" + "a" * 200 + ".csv")), 40)


class _Handler(BaseHTTPRequestHandler):
    """Server đóng vai MinIO. Hành vi lấy từ thuộc tính lớp để mỗi test tự dựng kịch bản."""

    status = 200
    body = b"hello"
    # Bỏ hẳn Content-Length: client buộc phải đọc tới lúc đóng kết nối, tức là không biết trước độ
    # dài — đúng tình huống mà việc đếm byte thật sinh ra để chống.
    omit_length = False
    honor_range = True
    redirect_to: str | None = None
    requests: list = []

    def log_message(self, *args):  # im lặng, khỏi rác output test
        pass

    def do_GET(self):
        type(self).requests.append(self.headers.get("Range"))

        if self.redirect_to:
            self.send_response(302)
            self.send_header("Location", self.redirect_to)
            self.end_headers()
            return

        if self.status >= 400:
            self.send_response(self.status)
            self.end_headers()
            self.wfile.write(b"error")
            return

        rng = self.headers.get("Range")
        if rng and self.honor_range:
            self.send_response(206)
            self.send_header("Content-Range", f"bytes 0-0/{len(self.body)}")
            self.send_header("Content-Length", "1")
            self.end_headers()
            self.wfile.write(self.body[:1])
            return

        self.send_response(200)
        if not self.omit_length:
            self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)


class _ServerCase(unittest.TestCase):
    """Nền chung: dựng server trên cổng ngẫu nhiên, dọn sạch sau mỗi test."""

    def setUp(self):
        _Handler.status = 200
        _Handler.body = b"hello"
        _Handler.omit_length = False
        _Handler.honor_range = True
        _Handler.redirect_to = None
        _Handler.requests = []

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmpdir = tempfile.mkdtemp(prefix="remote-file-test-")
        self.addCleanup(self.server.shutdown)

    def source(self, path: str = "/bucket/a.csv"):
        url = f"http://127.0.0.1:{self.port}{path}"
        with override(FILE_DOWNLOAD_ALLOWED_HOSTS=f"127.0.0.1:{self.port}"):
            return remote_file.validate_source_url(url)

    def dest(self, name: str = "out.csv") -> str:
        return os.path.join(self.tmpdir, name)


class ProbeSourceTest(_ServerCase):
    """Thăm dò 1 byte: chỉ câu trả lời DỨT KHOÁT mới được chặn."""

    def test_range_probe_returns_full_size_without_downloading(self):
        _Handler.body = b"x" * 5000
        size = remote_file.probe_source(self.source())
        self.assertEqual(size, 5000)
        self.assertEqual(_Handler.requests, ["bytes=0-0"])

    def test_uses_get_not_head(self):
        """Chữ ký presigned bao gồm cả HTTP method, nên HEAD sẽ ăn 403 giả."""
        remote_file.probe_source(self.source())
        self.assertEqual(len(_Handler.requests), 1)

    def test_forbidden_is_definitive(self):
        _Handler.status = 403
        with self.assertRaises(ExcelImportError) as ctx:
            remote_file.probe_source(self.source())
        self.assertEqual(ctx.exception.error_code, remote_file.ERR_DOWNLOAD_FORBIDDEN)

    def test_not_found_is_definitive(self):
        _Handler.status = 404
        with self.assertRaises(ExcelImportError) as ctx:
            remote_file.probe_source(self.source())
        self.assertEqual(ctx.exception.error_code, remote_file.ERR_DOWNLOAD_NOT_FOUND)

    def test_server_error_is_inconclusive(self):
        """5xx có thể tự khỏi nên không được chặn ở pha đồng bộ — cứ nhận job."""
        _Handler.status = 503
        self.assertIsNone(remote_file.probe_source(self.source()))

    def test_unreachable_host_is_inconclusive(self):
        self.server.shutdown()
        self.assertIsNone(remote_file.probe_source(self.source()))

    def test_oversize_is_definitive(self):
        _Handler.body = b"x" * (2 * 1024 * 1024)
        with override(EXCEL_DOWNLOAD_MAX_MB=1):
            with self.assertRaises(ExcelImportError) as ctx:
                remote_file.probe_source(self.source())
        self.assertEqual(ctx.exception.error_code, remote_file.ERR_FILE_TOO_LARGE)
        # Vượt trần mà chỉ tốn đúng một byte đường truyền — đó là toàn bộ giá trị của phép thăm dò.
        self.assertEqual(_Handler.requests, ["bytes=0-0"])

    def test_server_ignoring_range_still_gives_size(self):
        _Handler.honor_range = False
        _Handler.body = b"y" * 321
        self.assertEqual(remote_file.probe_source(self.source()), 321)


class DownloadTest(_ServerCase):
    """Tải theo khối: trần dung lượng, thử lại đúng chỗ, đổi tên nguyên tử."""

    def test_happy_path_writes_file_and_removes_part(self):
        _Handler.body = b"col\n1\n2\n"
        dest = self.dest()
        size = remote_file.download_to(self.source(), dest)

        self.assertEqual(size, len(_Handler.body))
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), _Handler.body)
        self.assertFalse(os.path.exists(dest + ".part"))

    def test_creates_missing_directory(self):
        dest = os.path.join(self.tmpdir, "sub", "dir", "out.csv")
        remote_file.download_to(self.source(), dest)
        self.assertTrue(os.path.exists(dest))

    def test_oversize_declared_rejected_before_reading_body(self):
        _Handler.body = b"z" * (2 * 1024 * 1024)
        dest = self.dest()
        with override(EXCEL_DOWNLOAD_MAX_MB=1):
            with self.assertRaises(ExcelImportError) as ctx:
                remote_file.download_to(self.source(), dest)
        self.assertEqual(ctx.exception.error_code, remote_file.ERR_FILE_TOO_LARGE)
        self.assertFalse(os.path.exists(dest))
        self.assertFalse(os.path.exists(dest + ".part"))

    def test_oversize_caught_when_source_declares_nothing(self):
        """Không có Content-Length thì chỉ còn cách đếm byte thật trong lúc ghi."""
        _Handler.body = b"z" * (2 * 1024 * 1024)
        _Handler.omit_length = True
        dest = self.dest()
        with override(EXCEL_DOWNLOAD_MAX_MB=1):
            with self.assertRaises(ExcelImportError) as ctx:
                remote_file.download_to(self.source(), dest)
        self.assertEqual(ctx.exception.error_code, remote_file.ERR_FILE_TOO_LARGE)
        self.assertFalse(os.path.exists(dest))
        self.assertFalse(os.path.exists(dest + ".part"))

    def test_client_error_is_not_retried(self):
        _Handler.status = 403
        with override(EXCEL_DOWNLOAD_RETRIES=3):
            with self.assertRaises(ExcelImportError) as ctx:
                remote_file.download_to(self.source(), self.dest())
        self.assertEqual(ctx.exception.error_code, remote_file.ERR_DOWNLOAD_FORBIDDEN)
        self.assertEqual(len(_Handler.requests), 1, "4xx mà thử lại là đốt thời gian vô ích")

    def test_server_error_is_retried_then_gives_up(self):
        _Handler.status = 503
        with override(EXCEL_DOWNLOAD_RETRIES=1):
            with self.assertRaises(ExcelImportError) as ctx:
                remote_file.download_to(self.source(), self.dest())
        self.assertEqual(ctx.exception.error_code, remote_file.ERR_DOWNLOAD_FAILED)
        self.assertEqual(len(_Handler.requests), 2)

    def test_redirect_is_not_followed(self):
        """Redirect là đường vòng qua allowlist, nên phải chết chứ không được đi theo."""
        _Handler.redirect_to = "http://169.254.169.254/latest/meta-data/"
        with override(EXCEL_DOWNLOAD_RETRIES=0):
            with self.assertRaises(ExcelImportError) as ctx:
                remote_file.download_to(self.source(), self.dest())
        self.assertEqual(ctx.exception.error_code, remote_file.ERR_DOWNLOAD_FAILED)


if __name__ == "__main__":
    unittest.main()
