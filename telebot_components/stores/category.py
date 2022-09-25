import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable, Optional

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.callback_data import CallbackData

from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.generic import KeyValueStore
from telebot_components.stores.language import (
    AnyText,
    Language,
    LanguageStore,
    any_text_to_str,
    vaildate_singlelang_text,
)
from telebot_components.stores.types import OnOptionSelected
from telebot_components.stores.utils import callback_query_processing_error


@dataclass
class Category:
    name: str
    button_caption: AnyText
    hashtag: Optional[str] = None
    hidden: bool = False  # hide category from menu for new users while keeping them for those who already selected it


class CategoryStore:
    def __init__(
        self,
        bot_prefix: str,
        redis: RedisInterface,
        categories: list[Category],
        category_expiration_time: timedelta,
        default_category: Optional[Category] = None,
        language_store: Optional[LanguageStore] = None,
        mark_selected: Callable[[str], str] = lambda caption: "✅ " + caption,
    ):
        self.logger = logging.getLogger(f"{__name__}.{bot_prefix}")
        self.categories = categories
        self.default_category = default_category
        if self.default_category is not None:
            self.categories.append(self.default_category)
        self.categories_by_name = {c.name: c for c in categories}
        self.user_category_store = KeyValueStore[Optional[Category]](
            name="user-category",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=category_expiration_time,
            dumper=lambda c: c.name if c else "None",
            loader=lambda category_name: self.categories_by_name.get(category_name),
        )

        self.select_category_callback_data = CallbackData("cat_name", prefix="category")

        self.language_store = language_store
        for category in categories:
            if self.language_store is not None:
                self.language_store.validate_multilang(category.button_caption)
            else:
                vaildate_singlelang_text(category.button_caption)

        self.mark_selected = mark_selected

    async def save_user_category(self, user: tg.User, category: Category) -> bool:
        if category not in self.categories:
            self.logger.warning("Saving category that has not been passed to the store on initialization")
        return await self.user_category_store.save(user.id, category)

    async def get_user_category(self, user: tg.User) -> Optional[Category]:
        return await self.user_category_store.load(user.id) or self.default_category

    def setup(self, bot: AsyncTeleBot, on_category_selected: Optional[OnOptionSelected[Category]] = None):
        @bot.callback_query_handler(callback_data=self.select_category_callback_data)
        async def category_selected(call: tg.CallbackQuery):
            user = call.from_user
            try:
                data = self.select_category_callback_data.parse(call.data)
                category_name = data["cat_name"]
            except Exception:
                await callback_query_processing_error(bot, call, f"corrupted callback query '{call.data}'", self.logger)
                return
            category = self.categories_by_name.get(category_name)
            if category is None:
                await callback_query_processing_error(
                    bot, call, f"unknown category name: {category_name}", self.logger, error_level=False
                )
                return
            category_saved = await self.save_user_category(user, category)
            if not category_saved:
                await callback_query_processing_error(bot, call, f"unable to save category", self.logger)
                return
            try:
                await bot.answer_callback_query(call.id)
                await bot.edit_message_reply_markup(
                    user.id, call.message.id, reply_markup=(await self.markup_for_user(call.from_user))
                )
            except Exception:
                # exceptions are raised when user clicks on the same button and markup is not changed
                pass
            if on_category_selected is not None:
                try:
                    await on_category_selected(bot, call.message, call.from_user, category)
                except Exception:
                    self.logger.exception("Error in on_category_selected callback")

    async def markup_for_user_localised(self, user: tg.User, language: Optional[Language]) -> tg.InlineKeyboardMarkup:
        current_user_category = await self.get_user_category(user)

        def caption(category: Category) -> str:
            caption = any_text_to_str(category.button_caption, language)
            if current_user_category is not None and category.name == current_user_category.name:
                return self.mark_selected(caption)
            else:
                return caption

        return tg.InlineKeyboardMarkup(
            keyboard=[
                [
                    tg.InlineKeyboardButton(
                        text=caption(category),
                        callback_data=self.select_category_callback_data.new(cat_name=category.name),
                    )
                ]
                for category in self.categories
                if not category.hidden
            ]
        )

    async def markup_for_user(self, user: tg.User) -> tg.InlineKeyboardMarkup:
        if self.language_store is None:
            language = None
        else:
            language = await self.language_store.get_user_language(user)

        return await self.markup_for_user_localised(user, language)
