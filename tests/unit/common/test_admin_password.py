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


def test_sync_skips_when_admin_password_is_not_set(monkeypatch):
    from common.core import admin_password

    def no_session(*_args, **_kwargs):
        raise AssertionError("must not open a session without ADMIN_PASSWORD")

    monkeypatch.setattr(admin_password.settings, "ADMIN_PASSWORD", "")
    monkeypatch.setattr(admin_password, "Session", no_session)
    assert admin_password.sync_admin_password() is False


def test_sync_writes_the_configured_password(monkeypatch):
    from common.core import admin_password

    calls = []

    class FakeSessionContext:
        def __init__(self, _engine):
            pass

        def __enter__(self):
            return "session"

        def __exit__(self, *_exc):
            return False

    def fake_set(*, session, password):
        calls.append((session, password))
        return True

    monkeypatch.setattr(admin_password.settings, "ADMIN_PASSWORD", "Adminelc@123")
    monkeypatch.setattr(admin_password, "Session", FakeSessionContext)
    monkeypatch.setattr(admin_password, "set_admin_password", fake_set)
    assert admin_password.sync_admin_password() is True
    assert calls == [("session", "Adminelc@123")]
