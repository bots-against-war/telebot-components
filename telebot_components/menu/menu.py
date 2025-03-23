import datetime
import enum
import hashlib
import itertools
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Union

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.api import ApiHTTPException
from telebot.callback_data import CallbackData
from telebot.types import service as tg_service_types

from telebot_components.language import (
    AnyText,
    LanguageStoreInterface,
    MaybeLanguage,
    any_text_to_str,
    vaildate_singlelang_text,
)
from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.category import Category, CategoryStore
from telebot_components.stores.generic import KeyValueStore
from telebot_components.utils import TextMarkup

ROUTE_MENU_CALLBACK_DATA = CallbackData("route_to", prefix="menu")
TERMINATE_MENU_CALLBACK_DATA = CallbackData("id", prefix="terminator")
INACTIVE_BUTTON_CALLBACK_DATA = CallbackData(prefix="inactive_button")


@dataclass
class MenuItem:
    label: AnyText
    submenu: Optional["Menu"] = None
    terminator: Optional[str] = None
    link_url: Optional[str] = None
    bound_category: Optional[Category] = None
    metadata: Any | None = None

    def __post_init__(self) -> None:
        specified_options_count = sum([int(opt is not None) for opt in [self.submenu, self.terminator, self.link_url]])
        if specified_options_count != 1:
            raise ValueError(
                "Exactly one of the arguments must be set to non-None value: submenu, terminator, or link_url, "
                + f"but {self.submenu = }, {self.terminator = }, {self.link_url = }"
            )

        if self.bound_category is not None and self.terminator is None:
            raise ValueError("A category can only be bound to terminal menu items")

        self._id: Optional[str] = None
        self._legacy_id: Optional[str] = None
        self._containing_menu: Optional["Menu"] = None

    @property
    def id(self) -> str:
        if self._id is None:
            raise RuntimeError("MenuItem object was not properly initialized.")
        return self._id

    @id.setter
    def id(self, id: str):
        self._id = id

    @property
    def legacy_id(self) -> str:
        if self._legacy_id is None:
            raise RuntimeError("MenuItem object was not properly initialized.")
        return self._legacy_id

    @legacy_id.setter
    def legacy_id(self, legacy_id: str):
        self._legacy_id = legacy_id

    @property
    def containing_menu(self) -> "Menu":
        if self._containing_menu is None:
            raise RuntimeError("MenuItem object was not properly initialized.")
        return self._containing_menu

    @containing_menu.setter
    def containing_menu(self, parent_menu: "Menu"):
        self._containing_menu = parent_menu

    def get_inline_button(self, language: MaybeLanguage):
        if self.submenu is not None:
            return tg.InlineKeyboardButton(
                text=any_text_to_str(self.label, language),
                callback_data=ROUTE_MENU_CALLBACK_DATA.new(self.submenu.id),
            )
        elif self.link_url is not None:
            return tg.InlineKeyboardButton(
                text=any_text_to_str(self.label, language),
                url=self.link_url,
            )
        else:
            return tg.InlineKeyboardButton(
                text=any_text_to_str(self.label, language),
                callback_data=TERMINATE_MENU_CALLBACK_DATA.new(self.id),
            )

    def get_keyboard_button(self, language: MaybeLanguage) -> tg.KeyboardButton:
        return tg.KeyboardButton(text=any_text_to_str(self.label, language))

    def get_inactive_inline_button(self, selected_item_id: str, language: MaybeLanguage):
        button_text = any_text_to_str(self.label, language)
        if selected_item_id in [self.id, self.legacy_id]:
            button_text = "✅ " + button_text
        return tg.InlineKeyboardButton(
            text=button_text,
            callback_data=INACTIVE_BUTTON_CALLBACK_DATA.new(),
        )


class MenuMechanism(enum.Enum):
    INLINE_BUTTONS = "inline_buttons"
    REPLY_KEYBOARD = "reply_keyboard"

    INLINE_BUTTONS_IMMUTABLE = "inline_buttons_immutable"

    def is_inline_kbd(self) -> bool:
        return self in {MenuMechanism.INLINE_BUTTONS, MenuMechanism.INLINE_BUTTONS_IMMUTABLE}

    def is_reply_kbd(self) -> bool:
        return not self.is_inline_kbd()

    def is_updateable(self) -> bool:
        return self is MenuMechanism.INLINE_BUTTONS


@dataclass
class MenuConfig:
    back_label: Optional[AnyText]  # None = no back button = submenu cannot be exited
    lock_after_termination: bool = False
    is_text_html: bool = False
    text_markup: TextMarkup = TextMarkup.NONE
    mechanism: MenuMechanism = MenuMechanism.INLINE_BUTTONS

    def __post_init__(self) -> None:
        if self.is_text_html and self.text_markup is TextMarkup.NONE:
            self.text_markup = TextMarkup.HTML


@dataclass
class Menu:
    text: AnyText
    menu_items: list[MenuItem]
    config: Optional[MenuConfig] = None

    def __post_init__(self) -> None:
        self.parent_menu_item: Optional["MenuItem"] = None
        self._id: Optional[str] = None
        self._legacy_id: Optional[str] = None
        for item in self.menu_items:
            item.containing_menu = self
            if item.submenu is not None:
                item.submenu.parent_menu_item = item

    @property
    def displayed_items(self) -> list[MenuItem]:
        items = self.menu_items.copy()
        if self.effective_config.back_label is not None and self.parent_menu_item is not None:
            items.append(
                MenuItem(label=self.effective_config.back_label, submenu=self.parent_menu_item.containing_menu)
            )
        return items

    @property
    def effective_config(self) -> MenuConfig:
        if self.config is not None:
            return self.config
        elif self.parent_menu_item is not None:
            return self.parent_menu_item.containing_menu.effective_config
        else:
            return MenuConfig(
                back_label="back",
                lock_after_termination=True,
            )  # backwards compatibility for pre-config code

    @property
    def id(self) -> str:
        if self._id is None:
            raise RuntimeError("Menu object was not properly initialized.")
        return self._id

    @id.setter
    def id(self, id: str):
        self._id = id

    @property
    def legacy_id(self) -> str:
        if self._legacy_id is None:
            raise RuntimeError("Menu object was not properly initialized.")
        return self._legacy_id

    @legacy_id.setter
    def legacy_id(self, legacy_id: str):
        self._legacy_id = legacy_id

    def descendants(self) -> list["Menu"]:
        children = [mi.submenu for mi in self.menu_items if mi.submenu is not None]
        grandchildren: list[Menu] = []
        for menu in children:
            grandchildren.extend(menu.descendants())
        return children + grandchildren

    def get_keyboard_markup(self, language: MaybeLanguage) -> Union[tg.InlineKeyboardMarkup, tg.ReplyKeyboardMarkup]:
        if self.effective_config.mechanism.is_inline_kbd():
            return tg.InlineKeyboardMarkup(
                keyboard=[[menu_item.get_inline_button(language)] for menu_item in self.displayed_items]
            )
        else:
            reply_markup = tg.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
            # HACK: telebot annotates keyboard as list[list[KeyboardButton]], but actually expectes JSONified versions
            # of the button objects
            reply_markup.keyboard = [[item.get_keyboard_button(language).to_dict()] for item in self.displayed_items]
            return reply_markup

    def get_inactive_keyboard_markup(
        self, selected_item_id: str, language: MaybeLanguage
    ) -> tg.InlineKeyboardMarkup | None:
        if self.effective_config.mechanism.is_inline_kbd():
            return tg.InlineKeyboardMarkup(
                keyboard=[
                    [menu_item.get_inactive_inline_button(selected_item_id, language)] for menu_item in self.menu_items
                ]
            )
        else:
            return None


@dataclass
class TerminatorContext:
    bot: AsyncTeleBot
    user: tg.User
    menu_message: Optional[tg.Message]
    menu_message_id: Optional[int]
    terminator: str

    # chronological sequence of menu items clicked by the user to arrive at the terminator
    # top level menu item, submenu item, ..., terminator item
    path: list[MenuItem]


@dataclass
class TerminatorResult:
    menu_message_text_update: AnyText | None
    parse_mode: str | None = None
    menu_message_reply_markup_update: tg.ReplyMarkup | None = None
    lock_menu: bool | None = None  # if set, overrides the default lock_after_termination value in Menu config


TerminalMenuOptionHandler = Callable[[TerminatorContext], Awaitable[Optional[TerminatorResult]]]


class MenuHandler:
    def __init__(
        self,
        name: str,
        bot_prefix: str,
        menu_tree: Menu,
        redis: RedisInterface,
        category_store: Optional[CategoryStore] = None,
        language_store: Optional[LanguageStoreInterface] = None,
    ):
        self.category_store = category_store
        self.language_store = language_store
        self.name = name
        self.redis = redis
        self.logger = logging.getLogger(f"{__name__}[{bot_prefix}]")

        self.menus_list: list[Menu] = [menu_tree]
        self.menus_list.extend(menu_tree.descendants())

        # initializing menu ids with sequential numbers
        for i, menu in enumerate(self.menus_list):
            menu.id = self.generate_id(i, is_legacy=False)
            menu.legacy_id = self.generate_id(i, is_legacy=True)
        # initializing links from child menus to their parents
        for menu in self.menus_list:
            for menu_item in menu.menu_items:
                menu_item.containing_menu = menu
                if menu_item.submenu is not None:
                    menu_item.submenu.parent_menu_item = menu_item
        # validating keyboard button types against menu types
        self.has_reply_keyboard_menus = False
        for menu in self.menus_list:
            if menu.effective_config.mechanism.is_reply_kbd():
                self.has_reply_keyboard_menus = True
                for item in menu.menu_items:
                    if item.link_url is not None:
                        raise ValueError(
                            f"Menu {menu} is configured to work on reply keyboards, "
                            "but contains items with link URL, which only work on inline keyboards"
                        )

        self.menu_by_id = dict(
            itertools.chain.from_iterable(
                [
                    [
                        (menu.id, menu),
                        (menu.legacy_id, menu),
                    ]
                    for menu in self.menus_list
                ],
            )
        )

        # initializing menu item ids with sequential numbers
        all_menu_items: list[MenuItem] = []
        for menu in self.menus_list:
            all_menu_items.extend(menu.menu_items)
        for i, menu_item in enumerate(all_menu_items):
            menu_item.id = self.generate_id(i, is_legacy=False)
            menu_item.legacy_id = self.generate_id(i, is_legacy=True)

        menu_items_with_bound_categories = [mi for mi in all_menu_items if mi.bound_category is not None]
        if menu_items_with_bound_categories:
            if self.category_store is None:
                raise ValueError(
                    "Menu items have bound categories, but category store "
                    + f"is not passed to MenuHandler: {menu_items_with_bound_categories}"
                )
            menu_items_with_non_storable_categories = [
                mi
                for mi in menu_items_with_bound_categories
                if mi.bound_category and not self.category_store.is_storable(mi.bound_category)
            ]
            if menu_items_with_non_storable_categories:
                raise ValueError(
                    "Some categories bound to menu items are not storable "
                    + f"with the passed category store: {menu_items_with_non_storable_categories}"
                )

        self.menu_item_by_id = dict(
            itertools.chain.from_iterable(
                [
                    [
                        (menu_item.id, menu_item),
                        (menu_item.legacy_id, menu_item),
                    ]
                    for menu_item in all_menu_items
                ],
            )
        )

        for any_text in itertools.chain(
            [menu.text for menu in self.menus_list],
            [menu_item.label for menu_item in all_menu_items],
            [
                menu.effective_config.back_label
                for menu in self.menus_list
                if menu.effective_config.back_label is not None
            ],
        ):
            if self.language_store is not None:
                self.language_store.validate_multilang(any_text)
            else:
                vaildate_singlelang_text(any_text)

        # chat id -> id for the last menu sent to user
        self.current_menu_store = KeyValueStore[str](
            name=f"{self.name}-current-menu-id",
            prefix=bot_prefix,
            redis=self.redis,
            expiration_time=datetime.timedelta(hours=12),
            dumper=str,
            loader=str,
        )
        # chat id -> last sent menu message id
        self.last_menu_message_id_store = KeyValueStore[int](
            name=f"{self.name}-menu-message",
            prefix=bot_prefix,
            redis=self.redis,
            expiration_time=datetime.timedelta(hours=12),
            dumper=str,
            loader=int,
        )

    def generate_id(self, sequential_idx: int, is_legacy: bool) -> str:
        if is_legacy:
            return str(sequential_idx)
        else:
            name_hash = hashlib.md5(self.name.encode("utf-8")).hexdigest()[:16]
            return f"{name_hash}-{sequential_idx}"

    async def get_current_menu(self, chat_id: Union[str, int]) -> Optional[Menu]:
        current_menu_id = await self.current_menu_store.load(chat_id)
        if current_menu_id is None:
            return None
        else:
            return self.menu_by_id.get(current_menu_id)

    async def get_maybe_language(self, user: tg.User) -> MaybeLanguage:
        if self.language_store is None:
            return None
        else:
            return await self.language_store.get_user_language(user)

    def get_main_menu(self):
        return self.menu_by_id[self.generate_id(0, is_legacy=False)]

    async def start_menu(self, bot: AsyncTeleBot, user: tg.User) -> None:
        """Send menu message to the user, starting at the main menu"""
        await self._route_to_menu(
            bot=bot,
            user=user,
            new_menu=self.get_main_menu(),
            current_menu_message_id=None,
        )

    async def _route_to_menu(
        self,
        bot: AsyncTeleBot,
        user: tg.User,
        new_menu: Menu,
        current_menu_message_id: Optional[int],
        force_update: bool = False,
    ) -> None:
        language = await self.get_maybe_language(user)
        current_menu = await self.get_current_menu(user.id)
        await self.current_menu_store.save(user.id, new_menu.id)
        if force_update or (
            current_menu_message_id is not None
            and current_menu is not None
            and current_menu.effective_config.mechanism.is_updateable()
            and new_menu.effective_config.mechanism.is_updateable()
        ):
            try:
                await bot.edit_message_text(
                    chat_id=user.id,
                    text=any_text_to_str(new_menu.text, language),
                    parse_mode=new_menu.effective_config.text_markup.parse_mode(),
                    message_id=current_menu_message_id,
                    reply_markup=new_menu.get_keyboard_markup(language),
                )
                return
            except ApiHTTPException as e:
                self.logger.info(f"Error editing message text and reply markup, will send a new message: {e!r}")

        new_menu_message = await bot.send_message(
            chat_id=user.id,
            text=any_text_to_str(new_menu.text, language),
            parse_mode=new_menu.effective_config.text_markup.parse_mode(),
            reply_markup=new_menu.get_keyboard_markup(language),
        )
        await self.last_menu_message_id_store.save(user.id, new_menu_message.id)

    async def _terminate_menu(
        self,
        bot: AsyncTeleBot,
        user: tg.User,
        terminal_menu_item_id: str,
        handler: TerminalMenuOptionHandler,
        # these duplicate each other but are used in different contexts
        menu_message: Optional[tg.Message],
        menu_message_id: Optional[int],
    ) -> Optional[tg_service_types.HandlerResult]:
        if terminal_menu_item_id not in self.menu_item_by_id:
            # probably an item from another menu, let them catch it
            return tg_service_types.HandlerResult(continue_to_other_handlers=True)

        menu_message_id = menu_message_id or (menu_message.id if menu_message is not None else None)

        language = await self.get_maybe_language(user)
        selected_item = self.menu_item_by_id[terminal_menu_item_id]
        terminator = selected_item.terminator
        if terminator is None:
            self.logger.error(f"handle_terminator got non-terminating menu item: {selected_item}")
            return None

        await self.current_menu_store.drop(user.id)
        if selected_item.bound_category is not None and self.category_store is not None:
            await self.category_store.save_user_category(user, selected_item.bound_category)

        curr_item = selected_item
        path: list[MenuItem] = [curr_item]
        while curr_item.containing_menu.parent_menu_item is not None:
            curr_item = curr_item.containing_menu.parent_menu_item
            path.append(curr_item)
        path.reverse()

        try:
            terminator_handler_result = await handler(
                TerminatorContext(
                    bot=bot,
                    user=user,
                    terminator=terminator,
                    menu_message=menu_message,
                    menu_message_id=menu_message_id,
                    path=path,
                )
            )
        except Exception:
            self.logger.exception("Unexpected error handling terminal menu option, ignoring")
            terminator_handler_result = None

        terminal_menu = self.menu_by_id[selected_item.containing_menu.id]
        lock_menu = terminal_menu.effective_config.lock_after_termination
        if terminator_handler_result is not None:
            if terminator_handler_result.lock_menu is not None:
                lock_menu = terminator_handler_result.lock_menu

            if terminator_handler_result.menu_message_text_update is not None:
                reason: Optional[str] = None
                if menu_message_id is None:
                    reason = "message id is not passed to _teminate_menu"
                elif not terminal_menu.effective_config.mechanism.is_updateable():
                    reason = f"last menu has non-updateable mechanism {terminal_menu.effective_config.mechanism}"

                if reason is not None:
                    self.logger.error(
                        f"Terminator handler returned menu message text update, but we can't update it because {reason}"
                    )
                else:
                    try:
                        await bot.edit_message_text(
                            text=any_text_to_str(terminator_handler_result.menu_message_text_update, language),
                            chat_id=user.id,
                            message_id=menu_message_id,
                            parse_mode=terminator_handler_result.parse_mode,
                            reply_markup=terminator_handler_result.menu_message_reply_markup_update,
                        )
                    except Exception:
                        self.logger.info(
                            f"Error editing menu message with text from {terminator_handler_result!r}",
                            exc_info=True,
                        )

        if lock_menu and menu_message_id is not None:
            inactive_inline_markup = terminal_menu.get_inactive_keyboard_markup(terminal_menu_item_id, language)
            if inactive_inline_markup is not None:
                try:
                    await bot.edit_message_reply_markup(
                        chat_id=user.id,
                        message_id=menu_message_id,
                        reply_markup=inactive_inline_markup,
                    )
                except ApiHTTPException:
                    self.logger.info("Error locking menu", exc_info=True)

        return None

    def setup(
        self,
        bot: AsyncTeleBot,
        on_terminal_menu_option_selected: TerminalMenuOptionHandler,
    ):
        # handlers for inline menu stuff

        @bot.callback_query_handler(callback_data=ROUTE_MENU_CALLBACK_DATA, auto_answer=True)
        async def handle_menu(call: tg.CallbackQuery) -> Optional[tg_service_types.HandlerResult]:
            data = ROUTE_MENU_CALLBACK_DATA.parse(call.data)
            new_menu_id = data["route_to"]
            if new_menu_id not in self.menu_by_id:
                return tg_service_types.HandlerResult(continue_to_other_handlers=True)
            await self._route_to_menu(
                bot=bot,
                user=call.from_user,
                new_menu=self.menu_by_id[new_menu_id],
                current_menu_message_id=call.message.id,
            )
            return None

        @bot.callback_query_handler(callback_data=INACTIVE_BUTTON_CALLBACK_DATA, auto_answer=True)
        async def handle_inactive_menu(call: tg.CallbackQuery):
            pass

        @bot.callback_query_handler(callback_data=TERMINATE_MENU_CALLBACK_DATA, auto_answer=True)
        async def handle_terminator(call: tg.CallbackQuery) -> Optional[tg_service_types.HandlerResult]:
            data = TERMINATE_MENU_CALLBACK_DATA.parse(call.data)
            return await self._terminate_menu(
                bot=bot,
                user=call.from_user,
                terminal_menu_item_id=data["id"],
                handler=on_terminal_menu_option_selected,
                menu_message=call.message,
                menu_message_id=call.message.id,
            )

        # handler for reply keyboard stuff

        @bot.message_handler(priority=1000)  # high priority to process these first
        async def try_handle_reply_to_menu(message: tg.Message) -> Optional[tg_service_types.HandlerResult]:
            continue_result = tg_service_types.HandlerResult(continue_to_other_handlers=True)
            if not self.has_reply_keyboard_menus:
                return continue_result
            current_menu = await self.get_current_menu(message.chat.id)
            if current_menu is None:
                return continue_result

            for item in current_menu.displayed_items:
                item_texts = [item.label] if isinstance(item.label, str) else list(item.label.values())
                for text in item_texts:
                    if message.text == text:
                        if item.submenu is not None:
                            await self._route_to_menu(
                                bot=bot,
                                user=message.from_user,
                                new_menu=item.submenu,
                                current_menu_message_id=await self.last_menu_message_id_store.load(message.chat.id),
                            )
                            return None
                        elif item.terminator is not None:
                            return await self._terminate_menu(
                                bot=bot,
                                user=message.from_user,
                                terminal_menu_item_id=item.id,
                                handler=on_terminal_menu_option_selected,
                                menu_message=None,
                                menu_message_id=await self.last_menu_message_id_store.load(message.chat.id),
                            )
            else:
                return continue_result
