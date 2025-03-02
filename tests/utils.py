import asyncio
import datetime
import os
import random
from typing import Any, Callable, Optional
from uuid import uuid4

import aiohttp
import pytest
import pytest_mock
from aioresponses import CallbackResult
from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.test_util import MethodCall
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

        _ = asyncio.create_task(set_future_result())
        try:
            await future  # using dummy await here to delegate control to other coroutines
        except Exception:
            pass
        self.current_time += delay

    def emulate_wait(self, delay: float):
        self.current_time += delay


def using_real_redis() -> bool:
    return "REDIS_URL" in os.environ


pytest_skip_on_real_redis = pytest.mark.skipif(using_real_redis(), reason="Not running on real Redis")


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
        assert actual[required_key] == required_value, (
            f"{actual} contains {required_key!r}: {actual[required_key]} != {required_value}"
        )


def assert_list_of_required_subdicts(actual_dicts: list[dict], required_subdicts: list[dict]):
    assert len(actual_dicts) == len(required_subdicts), (
        f"actual dicts list has mismatching size: {len(actual_dicts)} != {len(required_subdicts)}: "
        + f"{actual_dicts = }, {required_subdicts = }"
    )
    for actual, required in zip(actual_dicts, required_subdicts):
        assert_required_subdict(actual, required)


def extract_full_kwargs(method_calls: list[MethodCall]) -> list[dict[str, Any]]:
    return [mc.full_kwargs for mc in method_calls]


def reply_markups_to_dict(method_call_kwargs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            k: (v.to_dict() if isinstance(v, (tg.InlineKeyboardMarkup, tg.ReplyKeyboardMarkup)) else v)
            for k, v in kw.items()
        }
        for kw in method_call_kwargs
    ]


class TelegramServerMock:
    def __init__(self, admin_chats: set[int] | None = None) -> None:
        self._message_id_counter = 0
        self._admin_chats = admin_chats or set()

    async def send_message_to_bot(
        self,
        bot: AsyncTeleBot,
        user_id: int,
        text: str,
        chat_id: int | None = None,
        reply_to_message_id: Optional[int] = None,
    ):
        self._message_id_counter += 1
        chat_id_ = chat_id or user_id
        is_admin_chat = chat_id_ in self._admin_chats

        update_json = {
            "update_id": random.randint(int(1e4), int(1e6)),
            "message": {
                "message_id": self._message_id_counter,
                "from": {
                    "id": user_id,
                    "is_bot": False,
                    "first_name": "Admin" if is_admin_chat else "User",
                },
                "chat": {
                    "id": chat_id_,
                    "type": "supergroup" if is_admin_chat else "private",
                },
                "date": int(datetime.datetime.now().timestamp()),
                "text": text,
            },
        }

        if reply_to_message_id is not None:
            update_json["message"]["reply_to_message"] = {  # type: ignore
                "message_id": reply_to_message_id,
                "from": {
                    "id": 1,
                    "is_bot": True,
                    "first_name": "Bot",
                },
                "chat": {
                    "id": chat_id_,
                    "type": "supergroup",
                },
                "date": 1662891416,
                "text": "unused-replied-to-message-text",
            }

        await bot.process_new_updates([tg.Update.de_json(update_json)])  # type: ignore

    async def press_button(self, bot: AsyncTeleBot, user_id: int, callback_data: str) -> None:
        user_json = {
            "id": user_id,
            "is_bot": False,
            "first_name": "User",
        }
        update_json = {
            "update_id": 19283649187364,
            "callback_query": {
                "id": 40198734019872364,
                "chat_instance": "wtf is this",
                "from": user_json,
                "data": callback_data,
                "message": {
                    "message_id": 11111,
                    "from": user_json,
                    "chat": {
                        "id": user_id,
                        "type": "private",
                    },
                    "date": int(datetime.datetime.now().timestamp()),
                    "text": "whatever",
                },
            },
        }
        await bot.process_new_updates([tg.Update.de_json(update_json)])  # type: ignore
