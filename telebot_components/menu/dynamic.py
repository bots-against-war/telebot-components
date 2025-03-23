import datetime
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Annotated, Awaitable, Callable

import pydantic
from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.types import service as tg_service_types

from telebot_components.language import LanguageStoreInterface
from telebot_components.menu.menu import (
    INACTIVE_BUTTON_CALLBACK_DATA,
    ROUTE_MENU_CALLBACK_DATA,
    TERMINATE_MENU_CALLBACK_DATA,
    Menu,
    MenuHandler,
    TerminalMenuOptionHandler,
)
from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.category import CategoryStore
from telebot_components.stores.generic import KeyListStore

TelegramUser = Annotated[
    tg.User,
    pydantic.BeforeValidator(tg.User.de_json),
    pydantic.PlainSerializer(tg.User.to_json, return_type=str),
]


@dataclass(frozen=True)
class DynamicMenuContext:
    user: TelegramUser
    chat_id: int | str


class DynamicMenu(pydantic.BaseModel):
    name: str
    context: DynamicMenuContext
    menu: Menu
    timestamp: datetime.datetime


@dataclass
class DynamicMenuHandler:
    menu_factory: Callable[[DynamicMenuContext], Awaitable[Menu]]
    terminal_option_handler: TerminalMenuOptionHandler

    bot: AsyncTeleBot
    bot_prefix: str
    redis: RedisInterface
    category_store: CategoryStore | None = None
    language_store: LanguageStoreInterface | None = None

    name: str | None = None  # set when using more than one DynamicMenuHandler per bot
    dynamic_menu_lifetime: datetime.timedelta = datetime.timedelta(days=180)

    def __post_init__(self) -> None:
        self._dynamic_menus_by_chat = KeyListStore[DynamicMenu](
            name=f"{self.name or 'anonymous'}-dynamic-menus",
            prefix=self.bot_prefix,
            redis=self.redis,
            # FIXME: manual expiration based on timestamp in a background job
            expiration_time=None,
            dumper=DynamicMenu.model_dump_json,
            loader=DynamicMenu.model_validate_json,
        )

    def create_handler(self, dmenu: DynamicMenu) -> MenuHandler:
        return MenuHandler(
            name=dmenu.name,
            bot_prefix=self.bot_prefix,
            menu_tree=dmenu.menu,
            redis=self.redis,
            category_store=self.category_store,
            language_store=self.language_store,
        )

    async def start(self, context: DynamicMenuContext) -> None:
        dmenu = DynamicMenu(
            name=f"dynamic-menu-{uuid.uuid4()}",
            menu=await self.menu_factory(context),
            context=context,
            timestamp=datetime.datetime.now(),
        )
        await self._dynamic_menus_by_chat.push(
            key=context.chat_id,
            item=dmenu,
        )

        handler = self.create_handler(dmenu)
        await handler.start_menu(bot=self.bot, user=context.user)

    async def _iter_dmenu_handlers(self, chat_id: str | int) -> AsyncGenerator[MenuHandler, None]:
        dmenus = await self._dynamic_menus_by_chat.all(chat_id)
        for dmenu in dmenus:
            yield self.create_handler(dmenu)

    def setup(self) -> None:
        @self.bot.callback_query_handler(callback_data=ROUTE_MENU_CALLBACK_DATA, auto_answer=True, priority=-10)
        async def handle_menu(call: tg.CallbackQuery) -> tg_service_types.HandlerResult | None:
            async for handler in self._iter_dmenu_handlers(call.message.chat.id):
                if await handler.process_route_menu_callback_query(bot=self.bot, call=call):
                    return None
            return CONTINUE_RESULT

        @self.bot.callback_query_handler(callback_data=INACTIVE_BUTTON_CALLBACK_DATA, auto_answer=True)
        async def handle_inactive_menu(call: tg.CallbackQuery):
            pass

        @self.bot.callback_query_handler(callback_data=TERMINATE_MENU_CALLBACK_DATA, auto_answer=True, priority=-10)
        async def handle_terminator(call: tg.CallbackQuery) -> tg_service_types.HandlerResult | None:
            async for handler in self._iter_dmenu_handlers(call.message.chat.id):
                if await handler.process_terminal_callback_query(
                    bot=self.bot,
                    call=call,
                    terminal_option_handler=self.terminal_option_handler,
                ):
                    return None
            return CONTINUE_RESULT

        @self.bot.message_handler(priority=900)
        async def try_handle_reply_to_menu(message: tg.Message) -> tg_service_types.HandlerResult | None:
            async for handler in self._iter_dmenu_handlers(message.chat.id):
                if await handler.process_message(
                    bot=self.bot,
                    message=message,
                    terminal_option_handler=self.terminal_option_handler,
                ):
                    return None
            return CONTINUE_RESULT


CONTINUE_RESULT = tg_service_types.HandlerResult(continue_to_other_handlers=True)
