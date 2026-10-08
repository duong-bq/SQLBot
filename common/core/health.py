"""Route cho probe của Kubernetes: /healthz (còn sống) và /readyz (sẵn sàng nhận request)."""

import asyncio
import logging
from dataclasses import dataclass

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy import text

from common.core.config import settings
from common.core.db import engine

router = APIRouter(include_in_schema=False)

logger = logging.getLogger("sqlbot.health")

# Dùng chung cho mọi check, và nhỏ hơn timeoutSeconds của readinessProbe.
READINESS_BUDGET_SECONDS = 2.0


class QuietProbeAccessLog(logging.Filter):
    """Bỏ dòng access log của probe thành công để log không bị ngập mỗi 10 giây."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[4], int):
            paths = (f"{settings.CONTEXT_PATH}/healthz", f"{settings.CONTEXT_PATH}/readyz")
            return not (args[4] < 400 and str(args[2]).split("?")[0] in paths)
        return True


@dataclass
class CheckResult:
    """Kết quả một check; detail chỉ là tên loại lỗi, không bao giờ chứa thông báo lỗi gốc.

    Thông báo lỗi của driver có thể kèm chuỗi kết nối, tức là cả mật khẩu.
    """

    ok: bool
    detail: str | None = None


def _check_database() -> CheckResult:
    """Chạy `SELECT 1` bằng một kết nối lấy từ pool."""
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return CheckResult(ok=True)


async def _check_redis() -> CheckResult:
    """PING Redis bằng chính client mà cache đang dùng (standalone, sentinel hay cluster)."""
    from fastapi_cache import FastAPICache

    await FastAPICache.get_backend().redis.ping()
    return CheckResult(ok=True)


async def _run(check) -> CheckResult:
    """Chạy một check (đồng bộ hay bất đồng bộ) và đổi mọi ngoại lệ thành kết quả thất bại."""
    try:
        if asyncio.iscoroutinefunction(check):
            return await check()
        return await run_in_threadpool(check)
    except Exception as exc:  # noqa: BLE001 - detail chỉ lấy tên loại lỗi
        return CheckResult(ok=False, detail=type(exc).__name__)


def readiness_checks() -> dict:
    """Danh sách check đang áp dụng; Redis chỉ có khi cache dùng Redis."""
    checks = {"database": _check_database}
    if settings.CACHE_TYPE.lower() == "redis":
        checks["redis"] = _check_redis
    return checks


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness không làm I/O: một dependency chậm không được làm pod bị khởi động lại."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> JSONResponse:
    """Readiness kiểm tra các dependency trong một ngân sách thời gian chung.

    Trả 503 thì pod bị gỡ khỏi Service nhưng không bị khởi động lại.
    """
    tasks = {name: asyncio.ensure_future(_run(check)) for name, check in readiness_checks().items()}
    await asyncio.wait(tasks.values(), timeout=READINESS_BUDGET_SECONDS)
    results = {}
    for name, task in tasks.items():
        if task.done():
            results[name] = task.result()
        else:
            task.cancel()
            results[name] = CheckResult(ok=False, detail="Timeout")
    healthy = all(result.ok for result in results.values())
    if not healthy:
        failed = ", ".join(f"{name}={result.detail}" for name, result in results.items() if not result.ok)
        logger.warning("readyz unavailable: %s", failed)
    body = {
        "status": "ok" if healthy else "unavailable",
        "checks": {name: {"ok": result.ok, "detail": result.detail} for name, result in results.items()},
    }
    return JSONResponse(status_code=200 if healthy else 503, content=body)
