import json
import random
import re
from typing import Any, Optional, Type
from uuid import uuid4
import aiohttp
from aioresponses import aioresponses, CallbackResult
import pytest
from telebot import AsyncTeleBot
from telebot import types as tg
from yarl import URL

from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.language import (
    Language,
    LanguageSelectionMenuConfig,
    LanguageStore,
)
from tests.utils import mock_bot_user_json


@pytest.mark.parametrize(
    "user_language_code, expected_language",
    [
        pytest.param("ru", Language.RU),
        pytest.param("en", Language.EN),
        pytest.param("uk", Language.UK),
        pytest.param("fr", Language.RU, id="default language if the user's language is not supported"),
        pytest.param(None, Language.RU, id="default language if the user's language is not specified"),
    ],
)
async def test_get_user_language_basic(
    redis: RedisInterface, user_language_code: Optional[str], expected_language: Language
):
    language_store = LanguageStore(
        bot_prefix="testing",
        redis=redis,
        supported_languages=[Language.RU, Language.EN, Language.UK],
        default_language=Language.RU,
    )

    user_json = {"id": 131242069, "is_bot": False, "first_name": "test"}
    if user_language_code is not None:
        user_json["language_code"] = user_language_code
    user = tg.User.de_json(user_json)

    language = await language_store.get_user_language(user)
    assert language is expected_language
    for lang in language_store.languages:
        await language_store.set_user_language(user, lang)
        assert await language_store.get_user_language(user) == lang

    with pytest.raises(ValueError, match="Can't set user language to unsupported value <Language.PL: 'pl'>"):
        await language_store.set_user_language(user, Language.PL)

    with pytest.raises(ValueError, match="Can't set user language to unsupported value 'this is wrong'"):
        await language_store.set_user_language(user, "this is wrong")


@pytest.mark.parametrize(
    "supported_languages, default_language, emoji_buttons, checkmark_select, "
    + "expected_inline_keyboard_row, clicked_callback_data, expected_selected_language",
    [
        pytest.param(
            [Language.RU, Language.EN],
            Language.RU,
            True,
            True,
            [{"text": "✅ 🇷🇺", "callback_data": "lang:ru"}, {"text": "🇬🇧", "callback_data": "lang:en"}],
            "lang:en",
            Language.EN,
        ),
        pytest.param(
            [Language.RU, Language.EN],
            Language.EN,
            True,
            True,
            [{"text": "🇷🇺", "callback_data": "lang:ru"}, {"text": "✅ 🇬🇧", "callback_data": "lang:en"}],
            "lang:ru",
            Language.RU,
        ),
        pytest.param(
            [Language.RU, Language.EN],
            Language.EN,
            True,
            False,
            [{"text": "🇷🇺", "callback_data": "lang:ru"}, {"text": "[ 🇬🇧 ]", "callback_data": "lang:en"}],
            "lang:en",
            Language.EN,
        ),
        pytest.param(
            [Language.RU, Language.EN],
            Language.EN,
            False,
            False,
            [{"text": "RU", "callback_data": "lang:ru"}, {"text": "[ EN ]", "callback_data": "lang:en"}],
            "lang:en",
            Language.EN,
        ),
    ],
)
async def test_language_store_markup(
    supported_languages: list[Language],
    default_language: Language,
    emoji_buttons: bool,
    checkmark_select: bool,
    clicked_callback_data: str,
    expected_inline_keyboard_row: list[dict[str, str]],
    expected_selected_language: Language,
    mock_request: aioresponses,
    redis: RedisInterface,
):
    MOCK_TOKEN = uuid4().hex
    MOCK_CHAT_ID = random.randint(10**6, 10**7)
    MOCK_MESSAGE_TEXT = "please select language"

    bot = AsyncTeleBot(MOCK_TOKEN)

    language_store = LanguageStore(
        bot_prefix="testing",
        redis=redis,
        supported_languages=supported_languages,
        default_language=default_language,
        menu_config=LanguageSelectionMenuConfig(
            emojj_buttons=emoji_buttons,
            select_with_checkmark=checkmark_select,
        ),
    )
    language_store.setup(bot)

    user_json = {"id": 131242069, "is_bot": False, "first_name": "user"}
    user = tg.User.de_json(user_json)

    async def send_message_callback(url: URL, data: aiohttp.FormData, **kwargs):
        """Mocking telegram server response"""
        reply_markup = None
        for field in data._fields:
            mdict, _, dump = field
            if mdict["name"] == "reply_markup":
                reply_markup = json.loads(dump)

        assert reply_markup == {"inline_keyboard": [expected_inline_keyboard_row]}

        return CallbackResult(
            status=200,
            payload={
                "ok": True,
                "result": {
                    "message_id": 1312,
                    "from": mock_bot_user_json(),
                    "chat": {
                        "id": MOCK_CHAT_ID,
                        "first_name": "user",
                        "type": "private",
                    },
                    "date": 1654340488,
                    "text": MOCK_MESSAGE_TEXT,
                    "reply_markup": reply_markup,
                },
            },
        )

    mock_request.get(f"https://api.telegram.org/bot{MOCK_TOKEN}/sendMessage", callback=send_message_callback)

    language_select_message = await bot.send_message(
        MOCK_CHAT_ID, "please select language", reply_markup=(await language_store.markup_for_user(user))
    )

    button_clicked_update_json = {
        "update_id": 694134158,
        "callback_query": {
            "id": "1334532895174033954",
            "from": user_json,
            "message": language_select_message.json,
            "chat_instance": "-1351451435134665",
            "data": clicked_callback_data,
        },
    }

    await bot.process_new_updates([tg.Update.de_json(button_clicked_update_json)])
    assert await language_store.get_user_language(user) is expected_selected_language
