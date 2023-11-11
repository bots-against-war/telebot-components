import json
import random
from typing import Optional
from uuid import uuid4

import pytest
from aioresponses import aioresponses
from telebot import AsyncTeleBot
from telebot import types as tg

from telebot_components.language import LanguageData
from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.language import (
    Language,
    LanguageLabelPart,
    LanguageSelectionMenuConfig,
    LanguageStore,
)
from tests.utils import generate_str, mock_bot_user_json, telegram_api_mock


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
        bot_prefix=generate_str(),
        redis=redis,
        supported_languages=[Language.RU, Language.EN, Language.UK],
        default_language=Language.RU,
    )

    user_json = {"id": 131242069, "is_bot": False, "first_name": "test"}
    if user_language_code is not None:
        user_json["language_code"] = user_language_code
    user = tg.User.de_json(user_json)

    language = await language_store.get_user_language(user)
    assert language == expected_language
    for lang in language_store.languages:
        await language_store.set_user_language(user, lang)
        assert await language_store.get_user_language(user) == lang

    with pytest.raises(ValueError, match="Can't set user language to unsupported value "):
        await language_store.set_user_language(user, Language.PL)

    with pytest.raises(ValueError, match="Can't set user language to unsupported value 'this is wrong'"):
        await language_store.set_user_language(user, "this is wrong")  # type: ignore


@pytest.mark.parametrize(
    "code, expected_lang_data",
    [
        pytest.param("ru", LanguageData.lookup("ru")),
        pytest.param("en", LanguageData.lookup("en")),
        pytest.param("En", LanguageData.lookup("en")),
        pytest.param("En-gb", LanguageData.lookup("en")),
        pytest.param("hy-am", LanguageData.lookup("hy")),
        pytest.param("ru-a-b-c-f-afg-b-sfb", LanguageData.lookup("ru")),
    ],
)
def test_language_data_lookup(code: str, expected_lang_data: LanguageData) -> None:
    assert LanguageData.lookup(code) == expected_lang_data


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
        pytest.param(
            [Language.RU, LanguageData.lookup("hy"), LanguageData.lookup("uk"), LanguageData.lookup("kk")],
            Language.RU,
            True,
            True,
            [
                {"text": "✅ 🇷🇺", "callback_data": "lang:ru"},
                {"text": "🇦🇲", "callback_data": "lang:hy"},
                {"text": "🇺🇦", "callback_data": "lang:uk"},
                {"text": "🇰🇿", "callback_data": "lang:kk"},
            ],
            "lang:hy",
            LanguageData.lookup("hy"),
        ),
        pytest.param(
            [LanguageData.lookup("blt"), LanguageData.lookup("en")],
            LanguageData.lookup("en"),
            True,
            True,
            [{"text": "BLT", "callback_data": "lang:blt"}, {"text": "✅ 🇬🇧", "callback_data": "lang:en"}],
            "lang:blt",
            LanguageData.lookup("blt"),
        ),
        pytest.param(
            [LanguageData.lookup("en"), LanguageData.lookup("de"), LanguageData.lookup("fr")],
            Language.EN,
            False,
            True,
            [
                {"text": "✅ EN", "callback_data": "lang:en"},
                {"text": "DE", "callback_data": "lang:de"},
                {"text": "FR", "callback_data": "lang:fr"},
            ],
            "lang:fr",
            LanguageData.lookup("fr"),
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
        bot_prefix=generate_str(),
        redis=redis,
        supported_languages=supported_languages,
        default_language=default_language,
        menu_config=LanguageSelectionMenuConfig(
            emojj_buttons=emoji_buttons,
            select_with_checkmark=checkmark_select,
        ),
    )
    await language_store.setup(bot)

    user_json = {"id": 131242069, "is_bot": False, "first_name": "user"}
    user = tg.User.de_json(user_json)

    @telegram_api_mock
    def check_inline_keyboard(form_data: dict[str, str]):
        reply_markup = json.loads(form_data["reply_markup"])
        assert reply_markup == {"inline_keyboard": [expected_inline_keyboard_row]}
        return {
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
        }

    mock_request.get(f"https://api.telegram.org/bot{MOCK_TOKEN}/sendMessage", callback=check_inline_keyboard)

    language_select_message = await bot.send_message(
        MOCK_CHAT_ID, MOCK_MESSAGE_TEXT, reply_markup=(await language_store.markup_for_user(user))
    )

    button_clicked_update = tg.Update.de_json(
        {
            "update_id": 694134158,
            "callback_query": {
                "id": "1334532895174033954",
                "from": user_json,
                "message": language_select_message.json,
                "chat_instance": "-1351451435134665",
                "data": clicked_callback_data,
            },
        }
    )
    assert button_clicked_update is not None
    await bot.process_new_updates([button_clicked_update])
    assert await language_store.get_user_language(user) == expected_selected_language


@pytest.mark.parametrize(
    "lang_label_templ, language, expected_label",
    [
        pytest.param([LanguageLabelPart.CODE], LanguageData.lookup("ru"), "RU"),
        pytest.param([LanguageLabelPart.EMOJI], LanguageData.lookup("ru"), "🇷🇺"),
        pytest.param([LanguageLabelPart.NAME_EN], LanguageData.lookup("ru"), "Russian"),
        pytest.param([LanguageLabelPart.NAME_LOCAL], LanguageData.lookup("ru"), "Русский"),
        pytest.param(
            [LanguageLabelPart.NAME_LOCAL],
            LanguageData.lookup("ade"),
            "Adele",
            id="no local name available, english used",
        ),
        pytest.param(
            [LanguageLabelPart.EMOJI, " ", LanguageLabelPart.NAME_LOCAL], LanguageData.lookup("uk"), "🇺🇦 Українська"
        ),
        pytest.param(
            [
                LanguageLabelPart.EMOJI,
                " ",
                LanguageLabelPart.NAME_LOCAL,
                " (",
                LanguageLabelPart.NAME_EN,
                ", ",
                LanguageLabelPart.CODE,
                ")",
            ],
            LanguageData.lookup("uk"),
            "🇺🇦 Українська (Ukrainian, UK)",
        ),
    ],
)
def test_complex_lang_label_schemes(lang_label_templ: list, language: LanguageData, expected_label: str) -> None:
    config = LanguageSelectionMenuConfig(language_label_template=lang_label_templ)
    assert config.language_label(language) == expected_label
