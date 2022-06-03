import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.callback_data import CallbackData

from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.generic import KeyValueStore
from telebot_components.stores.language import (
    AnyText,
    Language,
    LanguageStore,
    MultilangText,
    any_text_to_str,
    validate_multilang_text,
)
from telebot_components.stores.types import OnOptionSelected
from telebot_components.stores.utils import callback_query_processing_error


@dataclass
class Category:
    id: int
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
        language_store: Optional[LanguageStore] = None,
    ):
        self.logger = logging.getLogger(f"{__name__}.{bot_prefix}")
        self.categories = categories
        self.categories_by_id = {c.id: c for c in categories}
        self.user_category_store = KeyValueStore[Category](
            name="user-category",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=category_expiration_time,
            dumper=lambda c: str(c.id),
            loader=lambda category_id: self.categories_by_id[int(category_id)],
        )

        self.select_category_callback_data = CallbackData("cat_id", prefix="category")

        self.language_store = language_store
        if self.language_store is not None:
            for category in categories:
                self.language_store.validate_multilang(category.button_caption)

    async def save_user_category(self, user: tg.User, category: Category) -> bool:
        return await self.user_category_store.save(user.id, category)

    async def get_user_category(self, user: tg.User) -> Optional[Category]:
        return await self.user_category_store.load(user.id)

    def setup(self, bot: AsyncTeleBot, on_category_selected: Optional[OnOptionSelected[Category]] = None):
        @bot.callback_query_handler(callback_data=self.select_category_callback_data)
        async def category_selected(call: tg.CallbackQuery):
            user = call.from_user
            try:
                data = self.select_category_callback_data.parse(call.data)
                category_id = int(data["cat_id"])
            except Exception:
                await callback_query_processing_error(bot, call, f"corrupted callback query '{call.data}'", self.logger)
                return
            category = self.categories_by_id.get(category_id)
            if category is None:
                await callback_query_processing_error(bot, call, f"corrupted category id: {category_id}", self.logger)
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
        current_category = await self.get_user_category(user)

        def caption(category: Category) -> str:
            caption = any_text_to_str(category.button_caption, language)
            if current_category is not None and category.id == current_category.id:
                caption = "✅ " + caption
            return caption

        return tg.InlineKeyboardMarkup(
            keyboard=[
                [
                    tg.InlineKeyboardButton(
                        text=caption(category),
                        callback_data=self.select_category_callback_data.new(cat_id=category.id),
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
