"""Bản sao `common/telemetry` đồng bộ từ skill otel-integration (templates/python): sửa ở đó rồi sync lại, không sửa tay."""

import hashlib
from pathlib import Path

import common.telemetry as shared


def test_the_shared_module_was_not_edited_in_this_service():
    folder = Path(shared.__file__).parent
    digest = hashlib.sha256()
    for path in sorted(folder.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())

    assert digest.hexdigest() == (folder / "CHECKSUM").read_text().strip()
