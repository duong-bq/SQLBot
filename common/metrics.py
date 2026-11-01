"""Instrument của service. Phần dựng log, trace và provider nằm ở bản sao `common/telemetry`: sửa ở skill
otel-integration (templates/python) rồi đồng bộ lại, không sửa tại đó.

Không đặt id (datasource, job, workspace) lên thuộc tính của metric: mỗi giá trị là một series. Mã lỗi là tập nhỏ cố định nên được phép.
"""

from opentelemetry import metrics

_meter = metrics.get_meter("sqlbot")

excel_import_jobs = _meter.create_counter(
    "sqlbot.excel.import.jobs", unit="{job}", description="Excel import jobs that ended, by outcome and error code"
)
excel_callbacks = _meter.create_counter(
    "sqlbot.excel.callbacks", unit="{callback}", description="Excel result callbacks, by outcome"
)
