import asyncio
import os
from typing import Any, Callable
from uuid import uuid4

import aiohttp
import pytest
import pytest_mock
from aioresponses import CallbackResult
from yarl import URL


class TimeSupplier:
    def __init__(self, mocker: pytest_mock.MockerFixture):
        self.current_time = 0.0
        mocker.patch("time.time", new=self.mock_time_time)
        mocker.patch("time.sleep", new=self.mock_time_sleep)
        mocker.patch("asyncio.sleep", new=self.mock_asyncio_sleep)

    def mock_time_time(self) -> float:
        return self.current_time

    def mock_time_sleep(self, delay: float):
        for _ in range(10000):
            sum(range(100))  # spending CPU time on dummy calculations
        self.current_time += delay

    async def mock_asyncio_sleep(self, delay: float):
        future = asyncio.Future[None]()

        async def set_future_result():
            if not future.done():
                future.set_result(None)

        task = asyncio.create_task(set_future_result())
        try:
            await future  # using dummy await here to delegate control to other coroutines
        except Exception:
            pass
        self.current_time += delay

    def emulate_wait(self, delay: float):
        self.current_time += delay


def using_real_redis() -> bool:
    return "REDIS_URL" in os.environ


pytest_skip_on_real_redis = pytest.mark.skipif(using_real_redis(), reason="Can't emulate sleeping with real redis")


def mock_bot_user_json() -> dict[str, Any]:
    return {"id": 124521435, "is_bot": True, "first_name": "this bot", "username": "something"}


def telegram_api_mock(form_data_handler: Callable[[dict[str, str]], dict[str, Any]]):
    """Used to create callback for aioresponses"""

    def callback(url: URL, data: aiohttp.FormData, **kwargs):
        print(f"Telegram API request: {url}")
        # parsing aiohttp form format to dict
        form_data = dict()
        for mdict, _, dump in data._fields:
            form_data[mdict["name"]] = dump
        return CallbackResult(
            status=200,
            payload={
                "ok": True,
                "result": form_data_handler(form_data),
            },
        )

    return callback


def generate_str() -> str:
    return uuid4().hex


def assert_required_subdict(actual: dict, required: dict):
    """Actual dict is allowed to have extra keys beyond those required"""
    for required_key, required_value in required.items():
        assert required_key in actual, f"{actual} misses required key {required_key!r}"
        assert (
            actual[required_key] == required_value
        ), f"{actual} contains {required_key!r}: {actual[required_key]} != {required_value}"


def assert_list_of_required_subdicts(actual_dicts: list[dict], required_subdicts: list[dict]):
    assert len(actual_dicts) == len(
        required_subdicts
    ), f"actual dicts list has mismatching size: {len(actual_dicts)} != {len(required_subdicts)}: {actual_dicts = }, {required_subdicts = }"
    for actual, required in zip(actual_dicts, required_subdicts):
        assert_required_subdict(actual, required)
