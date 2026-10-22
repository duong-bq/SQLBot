"""set_admin_password đồng bộ mật khẩu admin theo ADMIN_PASSWORD: chỉ ghi khi khác giá trị đang lưu, chỉ nhắm đúng tài khoản admin id 1."""

from types import SimpleNamespace

from apps.system.crud.user import set_admin_password
from common.core.security import md5pwd


class FakeSession:
    def __init__(self, admin):
        self.admin = admin
        self.commits = 0

    def exec(self, _statement):
        return SimpleNamespace(first=lambda: self.admin)

    def add(self, _row):
        pass

    def commit(self):
        self.commits += 1


def test_updates_when_password_differs():
    admin = SimpleNamespace(password=md5pwd("SQLBot@123456"))
    session = FakeSession(admin)
    assert set_admin_password(session=session, password="Adminelc@123") is True
    assert admin.password == md5pwd("Adminelc@123")
    assert session.commits == 1


def test_noop_when_password_matches():
    admin = SimpleNamespace(password=md5pwd("Adminelc@123"))
    session = FakeSession(admin)
    assert set_admin_password(session=session, password="Adminelc@123") is False
    assert session.commits == 0


def test_noop_without_admin_row():
    session = FakeSession(None)
    assert set_admin_password(session=session, password="Adminelc@123") is False
    assert session.commits == 0
