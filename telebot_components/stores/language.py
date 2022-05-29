import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Protocol

from telebot import AsyncTeleBot, types
from telebot.callback_data import CallbackData

from telebot_components.constants import times
from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.generic import KeyValueStore


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
            Language.UK: "🇷🇺",
            Language.PL: "🇵🇱",
        }
        return known_emoji.get(self, str(self).upper())


MultilangText = dict[Language, str]


class OnLanguageChangeCallback(Protocol):
    async def __call__(
        self, bot: AsyncTeleBot, language_menu_message: types.Message, user: types.User, new_language: Language
    ) -> None:
        pass


@dataclass
class LanguageSelectionMenuConfig:
    emojj_buttons: bool  # if False (legacy), language codes are used: "RU"
    select_with_checkmark: bool  # if False (legacy), brackets are used: "[ EN ]"


class LanguageStore:
    def __init__(
        self,
        bot_prefix: str,
        redis: RedisInterface,
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
        self.logger = logging.getLogger(f"{__name__}.{bot_prefix}.language_store")
        self.languages = supported_languages
        self.default_language = default_language
        self.language_callback_data = CallbackData("code", prefix="lang")
        self.menu_config = menu_config

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
        return await self.user_language_store.save(user.id, lang)

    def setup(self, bot: AsyncTeleBot, on_language_change: Optional[OnLanguageChangeCallback] = None):
        @bot.callback_query_handler(callback_data=self.language_callback_data)
        async def language_selected(call: types.CallbackQuery):
            async def show_error_to_user(text: str):
                await bot.answer_callback_query(call.id, f"Server error: {text}", show_alert=True)

            try:
                data = self.language_callback_data.parse(call.data)
                user = call.from_user
                selected_language = Language(data["code"])
            except ValueError as e:
                self.logger.error(f"Error parsing callback data: {e}")
                await show_error_to_user(f"corrupted or outdated callback query '{call.data}' :(")
                return

            if selected_language not in self.languages:
                self.logger.error(f"User has sent callback query with unsupported language '{selected_language}'")
                await show_error_to_user(f"language '{selected_language}' is not supported :(")
                return

            language_saved = await self.set_user_language(user, selected_language)
            if not language_saved:
                self.logger.error(f"Error saving language to the DB")
                await show_error_to_user(f"unable to save selected language :(")
                return
            try:
                await bot.edit_message_reply_markup(
                    user.id, call.message.id, reply_markup=self._markup_from_selected_language(selected_language)
                )
                await bot.answer_callback_query(call.id)
            except Exception:
                # exception may be raised when user clicks on the same button and markup is not changed
                pass
            if on_language_change is not None:
                await on_language_change(bot, call.message, call.from_user, selected_language)

    def _markup_from_selected_language(self, selected_language: Language):
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

    async def markup(self, for_user: types.User) -> types.InlineKeyboardMarkup:
        user_lang = await self.get_user_language(for_user)
        return self._markup_from_selected_language(selected_language=user_lang)


class DummyLanguageStore(LanguageStore):
    def __init__(self, language: Language):
        self.constant_language = language

    async def get_user_language(self, user: types.User) -> Language:
        return self.constant_language

    async def set_user_language(self, user: types.User, lang: Language) -> bool:
        raise NotImplementedError("You can't save user language in a dummy language store")

    def setup(self, bot: AsyncTeleBot, on_language_change: Optional[OnLanguageChangeCallback] = None):
        pass

    async def markup(
        self, for_user: types.User, use_emoji: bool = False, selected_language_checkmark: bool = False
    ) -> types.InlineKeyboardMarkup:
        raise NotImplementedError("You can't use markup with a dummy language store")
