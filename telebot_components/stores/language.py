import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, Union

from telebot import AsyncTeleBot, types
from telebot.callback_data import CallbackData

from telebot_components.constants import times
from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.generic import KeyValueStore
from telebot_components.stores.types import OnOptionSelected
from telebot_components.stores.utils import callback_query_processing_error


class Language(Enum):
    """IETF language tags, same as used by Telegram
    https://en.wikipedia.org/wiki/IETF_language_tag

    Add your languages here on demand.
    """

    EN = "en"
    UK = "uk"
    RU = "ru"
    PL = "pl"

    def __str__(self) -> str:
        return self.value

    def emoji(self) -> str:
        known_emoji = {
            Language.EN: "🇬🇧",
            Language.UK: "🇺🇦",
            Language.RU: "🇷🇺",
            Language.PL: "🇵🇱",
        }
        return known_emoji.get(self, str(self).upper())


MaybeLanguage = Optional[Language]  # None = multilang mode is of, using normal strings


MultilangText = dict[Language, str]


def validate_multilang_text(t: Any, languages: list[Language]) -> MultilangText:
    if not isinstance(t, dict):
        raise TypeError(f"Language -> str dictionary expected, found {type(t).__name__}: {t}")
    for language in languages:
        if language not in t:
            raise ValueError(f"Multilang text misses localisation to '{language}': {t}")
        if not isinstance(t[language], str):
            raise ValueError(f"Non-string text for language {language}: {t[language]!r}")
    return t


def vaildate_singlelang_text(t: Any) -> str:
    if not isinstance(t, str):
        raise TypeError(f"Single language text must be a string, found {type(t).__name__}: {t}")
    return t


AnyText = Union[str, MultilangText]


def any_text_to_str(t: AnyText, language: MaybeLanguage) -> str:
    if language is None:
        if isinstance(t, str):
            return t
        else:
            raise ValueError(f"MultilangText requires a valid Language for localisation")
    else:
        if isinstance(t, str):
            raise ValueError(f"Plain string text requires language=None")
        else:
            localised_t = t.get(language)
            if not isinstance(localised_t, str):
                raise ValueError(f"No valid localisation found for language '{language}'")
            return localised_t


@dataclass
class LanguageSelectionMenuConfig:
    emojj_buttons: bool  # if False (legacy), language codes are used: "RU"
    select_with_checkmark: bool  # if False (legacy), brackets are used: "[ EN ]"


class LanguageStore:
    def __init__(
        self,
        redis: RedisInterface,
        bot_prefix: str,
        supported_languages: list[Language],
        default_language: Language,
        menu_config: LanguageSelectionMenuConfig = LanguageSelectionMenuConfig(True, True),
    ):
        self.user_language_store = KeyValueStore[Language](
            name="user-language",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=times.FOREVER,
            dumper=str,
            loader=Language,
        )
        self.logger = logging.getLogger(f"{__name__}.{bot_prefix}")
        self.languages = supported_languages
        self.default_language = default_language
        self.language_callback_data = CallbackData("code", prefix="lang")
        self.menu_config = menu_config

    def validate_multilang(self, ml_text: Any):
        validate_multilang_text(ml_text, self.languages)

    async def get_user_language(self, user: types.User) -> Language:
        stored_lang = await self.user_language_store.load(user.id)
        if stored_lang is not None:
            return stored_lang
        if user.language_code is None:
            return self.default_language
        try:
            user_interface_language = Language(user.language_code.lower())
            if user_interface_language in self.languages:
                return user_interface_language
        except ValueError:
            pass
        return self.default_language

    async def set_user_language(self, user: types.User, lang: Language) -> bool:
        if lang not in self.languages:
            raise ValueError(f"Can't set user language to unsupported value {lang!r}")
        return await self.user_language_store.save(user.id, lang)

    def setup(self, bot: AsyncTeleBot, on_language_change: Optional[OnOptionSelected[Language]] = None):
        @bot.callback_query_handler(callback_data=self.language_callback_data)
        async def language_selected(call: types.CallbackQuery):
            user = call.from_user
            try:
                data = self.language_callback_data.parse(call.data)
                language = Language(data["code"])
            except Exception:
                await callback_query_processing_error(bot, call, f"corrupted callback query '{call.data}'", self.logger)
                return

            if language not in self.languages:
                await callback_query_processing_error(bot, call, f"language '{language}' is not supported", self.logger)
                return

            language_saved = await self.set_user_language(user, language)
            if not language_saved:
                await callback_query_processing_error(bot, call, f"unable to save selected language", self.logger)
                return
            try:
                await bot.answer_callback_query(call.id)
                await bot.edit_message_reply_markup(
                    user.id, call.message.id, reply_markup=self.markup_for_selected_language(language)
                )
            except Exception:
                # exception may be raised when user clicks on the same button and markup is not changed
                pass
            if on_language_change is not None:
                try:
                    await on_language_change(bot, call.message, call.from_user, language)
                except Exception:
                    self.logger.exception("Error in on_language_change callback")

    def markup_for_selected_language(self, selected_language: Language):
        def get_lang_text(lang: Language) -> str:
            lang_str = lang.emoji() if self.menu_config.emojj_buttons else str(lang).upper()
            if lang is selected_language:
                if self.menu_config.select_with_checkmark:
                    lang_str = "✅ " + lang_str
                else:
                    lang_str = "[ " + lang_str + " ]"
            return lang_str

        return types.InlineKeyboardMarkup(
            [
                [
                    types.InlineKeyboardButton(
                        text=get_lang_text(lang), callback_data=self.language_callback_data.new(code=lang.value)
                    )
                    for lang in self.languages
                ]
            ],
            row_width=len(self.languages),
        )

    async def markup_for_user(self, user: types.User) -> types.InlineKeyboardMarkup:
        user_lang = await self.get_user_language(user)
        return self.markup_for_selected_language(selected_language=user_lang)


class DummyLanguageStore(LanguageStore):
    def __init__(self, language: Language):
        self.constant_language = language

    async def get_user_language(self, user: types.User) -> Language:
        return self.constant_language

    async def set_user_language(self, user: types.User, lang: Language) -> bool:
        raise NotImplementedError("You can't save user language in a dummy language store")

    def setup(self, bot: AsyncTeleBot, on_language_change: Optional[OnOptionSelected[Language]] = None):
        pass

    async def markup_for_user(
        self, for_user: types.User, use_emoji: bool = False, selected_language_checkmark: bool = False
    ) -> types.InlineKeyboardMarkup:
        raise NotImplementedError("You can't use markup with a dummy language store")
