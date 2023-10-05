import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Awaitable, Callable, Optional

from async_lru import alru_cache
from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.callback_data import CallbackData

from telebot_components.language import (
    AnyText,
    LanguageStoreInterface,
    MaybeLanguage,
    any_text_to_str,
    vaildate_singlelang_text,
)
from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.generic import KeyValueStore
from telebot_components.stores.types import OnOptionSelected
from telebot_components.stores.utils import callback_query_processing_error


@dataclass(frozen=True)
class Category:
    name: str
    button_caption: Optional[AnyText] = None
    hashtag: Optional[str] = None
    # hides category from menu for new users while keeping them for those who already selected it
    hidden: bool = False

    def get_localized_button_caption(self, language: MaybeLanguage) -> str:
        return any_text_to_str(self.button_caption, language) if self.button_caption is not None else self.name


@dataclass
class CategorySelectedContext:
    category: Category
    user: tg.User
    bot: Optional[AsyncTeleBot]
    callback_query: Optional[tg.CallbackQuery]


class CategoryStore:
    def __init__(
        self,
        bot_prefix: str,
        redis: RedisInterface,
        categories: list[Category],
        category_expiration_time: Optional[timedelta],
        default_category: Optional[Category] = None,
        language_store: Optional[LanguageStoreInterface] = None,
        mark_selected: Callable[[str], str] = lambda caption: "✅ " + caption,
        on_category_selected: Optional[Callable[[CategorySelectedContext], Awaitable[None]]] = None,
    ):
        self.logger = logging.getLogger(f"{__name__}[{bot_prefix}]")
        self.categories = categories
        self.default_category = default_category
        if self.default_category is not None:
            self.categories.append(self.default_category)
        self.categories_by_name = {c.name: c for c in categories}
        if len(self.categories) != len(self.categories_by_name):
            category_names = [c.name for c in self.categories]
            raise ValueError(
                f"Categories contain duplicate names: {[cn for cn in category_names if category_names.count(cn) > 1]}"
            )

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
            if category.button_caption is not None:
                if self.language_store is not None:
                    self.language_store.validate_multilang(category.button_caption)
                else:
                    vaildate_singlelang_text(category.button_caption)

        self.mark_selected = mark_selected
        self.on_category_selected = on_category_selected

    def is_storable(self, category: Category) -> bool:
        return category.name in self.categories_by_name

    async def save_user_category(self, user: tg.User, category: Category) -> bool:
        if category.name not in self.categories_by_name:
            self.logger.warning("Saving category that has not been passed to the store on initialization")
        self.get_user_category.cache_invalidate(user)
        result = await self.user_category_store.save(user.id, category)
        if self.on_category_selected is not None:
            try:
                await self.on_category_selected(CategorySelectedContext(category, user, None, None))
            except Exception:
                self.logger.exception("Error in on_category_selected callback")
        return result

    @alru_cache(maxsize=1_000_000)
    async def get_user_category(self, user: tg.User) -> Optional[Category]:
        return await self.user_category_store.load(user.id) or self.default_category

    def setup(self, bot: AsyncTeleBot, on_category_selected: Optional[OnOptionSelected[Category]] = None):
        if on_category_selected is not None:
            if self.on_category_selected is not None:
                raise RuntimeError(
                    "on_category_selected specified at both CategoryStore initialization and in setup method"
                )

            self.logger.warning("on_category_selected passed to setup method is deprecated")

            async def legacy_on_category_selected_wrapper(context: CategorySelectedContext) -> None:
                if context.bot is not None and context.callback_query is not None and on_category_selected is not None:
                    await on_category_selected(
                        context.bot, context.callback_query.message, context.user, context.category
                    )

            self.on_category_selected = legacy_on_category_selected_wrapper

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
                await callback_query_processing_error(bot, call, "unable to save category", self.logger)
                return
            try:
                await bot.answer_callback_query(call.id)
                await bot.edit_message_reply_markup(
                    user.id, call.message.id, reply_markup=(await self.markup_for_user(call.from_user))
                )
            except Exception:
                # exceptions are raised when user clicks on the same button and markup is not changed
                pass
            if self.on_category_selected is not None:
                try:
                    await self.on_category_selected(CategorySelectedContext(category, call.from_user, bot, call))
                except Exception:
                    self.logger.exception("Error in on_category_selected callback")

    async def markup_for_user_localised(self, user: tg.User, language: MaybeLanguage) -> tg.InlineKeyboardMarkup:
        current_user_category = await self.get_user_category(user)

        def caption(category: Category) -> str:
            caption = category.get_localized_button_caption(language)
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
