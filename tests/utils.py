import os
from typing import Any, Callable
import aiohttp
from aioresponses import CallbackResult

import pytest
import pytest_mock
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
        self.current_time += delay

    async def mock_asyncio_sleep(self, delay: float):
        self.current_time += delay

    def emulate_wait(self, delay: float):
        self.current_time += delay


def using_real_redis() -> bool:
    return "REDIS_URL" in os.environ


pytest_skip_on_real_redis = pytest.mark.skipif(using_real_redis(), reason="Can't emulate sleeping with real redis")


def mock_bot_user_json() -> dict[str, str]:
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
            status=200, payload={
                "ok": True,
                "result": form_data_handler(form_data),
            },
        )

    return callback
