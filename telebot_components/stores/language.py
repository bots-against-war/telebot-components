import enum
import logging
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Union

from telebot import AsyncTeleBot, types
from telebot.callback_data import CallbackData

from telebot_components.constants import times

# unused imports are for backwards compatbility
from telebot_components.language import (  # noqa: F401
    AnyLanguage,
    AnyText,
    Language,
    LanguageChangeContext,
    LanguageChangeHandler,
    LanguageData,
    LanguageStoreInterface,
    MaybeLanguage,
    MultilangText,
    any_language_to_language_data,
    any_text_to_str,
    is_any_text,
    is_multilang_text,
    vaildate_singlelang_text,
    validate_multilang_text,
)
from telebot_components.menu import MenuHandler
from telebot_components.menu.menu import (
    Menu,
    MenuConfig,
    MenuItem,
    MenuMechanism,
    TerminatorContext,
)
from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.generic import KeyValueStore
from telebot_components.stores.utils import callback_query_processing_error
from telebot_components.utils.strings import telegram_html_escape


class LanguageLabelPart(enum.Enum):
    EMOJI = "emoji"
    CODE = "code"
    NAME_EN = "name_en"
    NAME_LOCAL = "name_local"


@dataclass
class LanguageSelectionMenuConfig:
    emojj_buttons: bool = False
    # if False (legacy), brackets are used: "[ EN ]"
    select_with_checkmark: bool = True
    prompt: Optional[MultilangText] = None
    is_prompt_html: bool = False

    language_label_template: Optional[list[Union[str, LanguageLabelPart]]] = None

    def __post_init__(self) -> None:
        if self.emojj_buttons and self.language_label_template is not None:
            raise RuntimeError("emoji buttons and button template options are mutually exclusive")

        if self.language_label_template:
            self._effective_language_label_template = self.language_label_template
        elif self.emojj_buttons:
            self._effective_language_label_template = [LanguageLabelPart.EMOJI]
        else:
            self._effective_language_label_template = [LanguageLabelPart.CODE]

    def language_label(self, lang: LanguageData) -> str:
        str_parts: list[str] = []
        for part in self._effective_language_label_template:
            if isinstance(part, str):
                str_parts.append(part)
            elif part is LanguageLabelPart.EMOJI:
                str_parts.append(lang.emoji or lang.code.upper())
            elif part is LanguageLabelPart.CODE:
                str_parts.append(lang.code.upper())
            elif part is LanguageLabelPart.NAME_EN:
                str_parts.append(lang.name)
            elif part is LanguageLabelPart.NAME_LOCAL:
                str_parts.append(lang.local_name or lang.name)
        return "".join(str_parts)

    def html_menu_prompt(self, language: AnyLanguage) -> str:
        if self.prompt is None:
            raise ValueError("To use this method, prompt must be specified in the menu config")
        localized = any_text_to_str(self.prompt, language=language)
        if self.is_prompt_html:
            return localized
        else:
            return telegram_html_escape(localized)


class LanguageStore(LanguageStoreInterface):
    def __init__(
        self,
        redis: RedisInterface,
        bot_prefix: str,
        supported_languages: Iterable[AnyLanguage],
        default_language: AnyLanguage,
        menu_config: LanguageSelectionMenuConfig = LanguageSelectionMenuConfig(
            emojj_buttons=True,
            select_with_checkmark=True,
            prompt=None,
            is_prompt_html=False,
        ),
    ):
        self.user_language_store = KeyValueStore[LanguageData](
            name="user-language",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=times.FOREVER,
            dumper=lambda ld: ld.code,
            loader=LanguageData.lookup,
        )
        self.logger = logging.getLogger(f"{__name__}[{bot_prefix}]")
        self.languages = [any_language_to_language_data(lang) for lang in supported_languages]
        self.default_language = any_language_to_language_data(default_language)
        self.language_callback_data = CallbackData("code", prefix="lang")
        self.menu_config = menu_config

        self.reply_keyboard_lang_selector_menu_handler: Optional[MenuHandler] = None
        if self.menu_config.prompt is not None:
            self.validate_multilang(self.menu_config.prompt)
            self.reply_keyboard_lang_selector_menu_handler = MenuHandler(
                name="language-store-reply-kbd-selector",
                bot_prefix=bot_prefix,
                menu_tree=Menu(
                    text=self.menu_config.prompt,
                    config=MenuConfig(
                        back_label=None,
                        lock_after_termination=False,
                        is_text_html=self.menu_config.is_prompt_html,
                        mechanism=MenuMechanism.REPLY_KEYBOARD,
                    ),
                    menu_items=[
                        MenuItem(
                            label={lang: self.menu_config.language_label(language) for lang in self.languages},
                            terminator=language.code,
                        )
                        for language in self.languages
                    ],
                ),
                redis=redis,
                language_store=self,
            )

    async def send_inline_selector(
        self,
        bot: AsyncTeleBot,
        user: types.User,
    ) -> None:
        language = await self.get_user_language(user)
        await bot.send_message(
            chat_id=user.id,
            text=self.menu_config.html_menu_prompt(language),
            reply_markup=self.markup_for_selected_language(selected_language=language),
        )

    async def send_reply_keyboard_selector(self, bot: AsyncTeleBot, user: types.User) -> None:
        if self.reply_keyboard_lang_selector_menu_handler is None:
            raise ValueError("To use send_reply_keyboard_selector method, prompt must be specified in the menu config")
        await self.reply_keyboard_lang_selector_menu_handler.start_menu(bot, user)

    def validate_multilang(self, ml_text: Any):
        validate_multilang_text(ml_text, list(self.languages))

    async def get_selected_user_language(self, user: types.User) -> Optional[LanguageData]:
        return await self.user_language_store.load(user.id)

    async def get_user_language(self, user: types.User) -> LanguageData:
        stored_lang = await self.get_selected_user_language(user)
        if stored_lang is not None:
            return stored_lang
        if user.language_code is None:
            return self.default_language
        try:
            user_interface_language = LanguageData.lookup(user.language_code)
            if user_interface_language in self.languages:
                return user_interface_language
        except Exception:
            # unexpected user interface's language code
            pass
        return self.default_language

    async def set_user_language(self, user: types.User, language_data: AnyLanguage) -> bool:
        language_data = any_language_to_language_data(language_data)
        if language_data not in self.languages:
            raise ValueError(f"Can't set user language to unsupported value {language_data!r}")
        return await self.user_language_store.save(user.id, language_data)

    async def setup(self, bot: AsyncTeleBot, on_language_change: Optional[LanguageChangeHandler] = None):
        async def safe_on_language_change(
            message: Optional[types.Message], message_id: Optional[int], user: types.User, language: LanguageData
        ) -> None:
            if on_language_change is None:
                return
            try:
                await on_language_change(
                    LanguageChangeContext(
                        bot=bot,
                        message=message,
                        message_id=message_id,
                        user=user,
                        language=language,
                    )
                )
            except Exception:
                self.logger.exception("Error in on_language_change callback, ignoring")

        @bot.callback_query_handler(callback_data=self.language_callback_data, auto_answer=True)
        async def language_selected(call: types.CallbackQuery):
            user = call.from_user
            try:
                data = self.language_callback_data.parse(call.data)
                language = LanguageData.lookup(data["code"])
            except Exception:
                await callback_query_processing_error(bot, call, f"corrupted callback query '{call.data}'", self.logger)
                return

            if language not in self.languages:
                await callback_query_processing_error(bot, call, f"language '{language}' is not supported", self.logger)
                return

            previous_language = await self.get_user_language(user)

            if not await self.set_user_language(user, language):
                await callback_query_processing_error(bot, call, "unable to save selected language", self.logger)
                return

            if language == previous_language:
                return  # language not changed, nothing to do

            try:
                if self.menu_config.prompt is not None:
                    await bot.edit_message_text(
                        chat_id=user.id,
                        message_id=call.message.id,
                        text=self.menu_config.html_menu_prompt(language=language),
                        reply_markup=self.markup_for_selected_language(language),
                    )
                else:
                    await bot.edit_message_reply_markup(
                        chat_id=user.id,
                        message_id=call.message.id,
                        reply_markup=self.markup_for_selected_language(language),
                    )
            except Exception:
                self.logger.exception("Error editing message reply markup")

            await safe_on_language_change(
                message=call.message,
                message_id=call.message.id,
                user=call.from_user,
                language=language,
            )

        if self.reply_keyboard_lang_selector_menu_handler is not None:

            async def on_language_selected(context: TerminatorContext) -> None:
                selected_language = LanguageData.lookup(context.terminator)
                previous_language = await self.get_user_language(context.user)
                await self.set_user_language(context.user, selected_language)
                if selected_language == previous_language:
                    return None
                await safe_on_language_change(
                    message=context.menu_message,
                    message_id=context.menu_message_id,
                    user=context.user,
                    language=selected_language,
                )

            self.reply_keyboard_lang_selector_menu_handler.setup(
                bot,
                on_terminal_menu_option_selected=on_language_selected,
            )

    def markup_for_selected_language(self, selected_language: LanguageData):
        def get_lang_text(lang: LanguageData) -> str:
            lang_str = self.menu_config.language_label(lang)
            if lang == selected_language:
                if self.menu_config.select_with_checkmark:
                    lang_str = "✅ " + lang_str
                else:
                    lang_str = "[ " + lang_str + " ]"
            return lang_str

        return types.InlineKeyboardMarkup(
            [
                [
                    types.InlineKeyboardButton(
                        text=get_lang_text(lang),
                        callback_data=self.language_callback_data.new(code=lang.code),
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
    def __init__(self, language: LanguageData):
        self.constant_language = language

    async def get_user_language(self, user: types.User) -> LanguageData:
        return self.constant_language

    async def set_user_language(self, user: types.User, lang: AnyLanguage) -> bool:
        raise NotImplementedError("You can't save user language in a dummy language store")

    async def setup(self, bot: AsyncTeleBot, on_language_change: Optional[LanguageChangeHandler] = None):
        pass

    async def markup_for_user(
        self, for_user: types.User, use_emoji: bool = False, selected_language_checkmark: bool = False
    ) -> types.InlineKeyboardMarkup:
        raise NotImplementedError("You can't use markup with a dummy language store")
