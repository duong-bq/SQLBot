"""Test nhánh JSON đồng bộ (``stream=False``) chờ pipeline mà không chặn event loop.

Pipeline chạy ở thread của executor; route chỉ được ``await`` cho tới khi nó xong. Nếu route
chờ đồng bộ thì event loop đứng yên, probe ``/readyz`` hết hạn và Kubernetes gỡ pod khỏi
Service. Dựng ``LLMService`` bằng ``object.__new__`` như ``test_ask_datasource_list.py``,
giữ nguyên ``await_result``/``is_running``/``pop_chunk`` thật.
"""

# Import sqlbot_xpack TRƯỚC: apps.chat.* nằm trong một vòng import lẫn nhau.
import sqlbot_xpack  # noqa: F401  isort:skip

import asyncio
import gc
import time
from concurrent.futures import ThreadPoolExecutor

import orjson
import pytest

from apps.chat.api import chat as chat_api
from apps.chat.models.chat_model import ChatFinishStep, ChatQuestion
from apps.chat.task.llm import LLMService

PIPELINE_SECONDS = 0.6


@pytest.fixture()
def executor():
    """Executor riêng cho test, thay cho executor dùng chung của module ``llm``."""
    pool = ThreadPoolExecutor(max_workers=2)
    yield pool
    pool.shutdown(wait=True)


def make_service(executor, task):
    """``LLMService`` chỉ đủ sống cho ``stream_sql``; ``task`` chạy ở thread nền."""
    service = object.__new__(LLMService)
    service.chunk_list = []

    def run_task_async(**_kwargs):
        def body():
            for chunk in task():
                service.chunk_list.append(chunk)

        service.future = executor.submit(body)

    service.init_record = lambda session: None
    service.run_task_async = run_task_async
    return service


def slow_task(final):
    """Pipeline giả: ngủ đồng bộ như một lượt gọi LLM, rồi trả mẩu cuối."""

    def task():
        yield {"type": "progress"}
        time.sleep(PIPELINE_SECONDS)
        yield final

    return task


async def run_ask(monkeypatch, service):
    """Gọi ``stream_sql`` theo đúng tham số nhánh ``/chat/ask``."""

    async def fake_create(*_args, **_kwargs):
        return service

    monkeypatch.setattr(chat_api.LLMService, "create", fake_create)
    return await chat_api.stream_sql(
        session=None,
        current_user=None,
        request_question=ChatQuestion(chat_id=1, question="q"),
        current_assistant=None,
        in_chat=False,
        stream=False,
        finish_step=ChatFinishStep.QUERY_DATA,
        embedding=True,
    )


def test_event_loop_van_chay_trong_luc_cho_pipeline(monkeypatch, executor):
    """Một coroutine khác vẫn được chạy đều trong khi ``/chat/ask`` đang chờ."""
    final = {"success": True, "sql": "SELECT 1", "data": {"data": [[1]]}}
    service = make_service(executor, slow_task(final))

    async def scenario():
        ticks = 0
        ask = asyncio.ensure_future(run_ask(monkeypatch, service))
        while not ask.done():
            await asyncio.sleep(0.02)
            ticks += 1
        return ticks, ask.result()

    ticks, response = asyncio.run(scenario())

    # Chặn event loop thì vòng trên chỉ chạy được một hai lần sau khi pipeline xong.
    assert ticks >= 10
    assert response.status_code == 200
    assert orjson.loads(response.body) == final


def test_mau_cuoi_khong_success_tra_500(monkeypatch, executor):
    """Hành vi cũ giữ nguyên: mẩu cuối không ``success`` thì trả 500 kèm nội dung đó."""
    final = {"success": False, "message": "sinh SQL thất bại"}
    service = make_service(executor, slow_task(final))

    response = asyncio.run(run_ask(monkeypatch, service))

    assert response.status_code == 500
    assert orjson.loads(response.body) == final


def test_task_nen_nem_loi_tra_500_khong_canh_bao(monkeypatch, executor, caplog):
    """Task nền ném ngoại lệ: trả 500 như cũ, không log "exception was never retrieved"."""

    def broken():
        yield {"type": "progress"}
        raise RuntimeError("pipeline hỏng")

    service = make_service(executor, broken)

    response = asyncio.run(run_ask(monkeypatch, service))
    gc.collect()

    assert response.status_code == 500
    assert orjson.loads(response.body) == {"type": "progress"}
    assert "never retrieved" not in caplog.text
