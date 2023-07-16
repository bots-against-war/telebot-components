import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.api import ApiHTTPException
from telebot.callback_data import CallbackData

from telebot_components.stores.category import Category, CategoryStore
from telebot_components.stores.language import (
    AnyText,
    LanguageStore,
    MaybeLanguage,
    any_text_to_str,
    vaildate_singlelang_text,
)
from telebot_components.utils import telegram_html_escape

ROUTE_MENU_CALLBACK_DATA = CallbackData("route_to", prefix="menu")
TERMINATE_MENU_CALLBACK_DATA = CallbackData("id", prefix="terminator")
INACTIVE_BUTTON_CALLBACK_DATA = CallbackData(prefix="inactive_button")


class MenuItem:
    def __init__(
        self,
        label: AnyText,
        submenu: Optional["Menu"] = None,
        terminator: Optional[str] = None,
        link_url: Optional[str] = None,
        bound_category: Optional[Category] = None,
    ):
        self.label = label
        self.bound_category = bound_category

        self.submenu = submenu
        self.terminator = terminator
        self.link_url = link_url

        specified_options_count = sum([int(opt is not None) for opt in [self.submenu, self.terminator, self.link_url]])
        if specified_options_count != 1:
            raise ValueError(
                "Exactly one of the arguments must be set to non-None value: submenu, terminator, or link_url, "
                + f"but {submenu = }, {terminator = }, {link_url = }"
            )

        if self.bound_category is not None and self.terminator is None:
            raise ValueError("A category can only be bound to terminal menu items")

        self._id: Optional[str] = None
        self._parent_menu: Optional["Menu"] = None

    def __str__(self) -> str:
        res = f"MenuItem({self.label!r}"
        if self.terminator:
            res += f", terminator={self.terminator!r}"
            if self.bound_category:
                res += f", bound_category={self.bound_category}"
        elif self.submenu:
            res += f", submeny={self.submenu}"
        elif self.link_url:
            res += f", link_url={self.link_url!r}"
        res += ")"
        return res

    __repr__ = __str__

    @property
    def id(self) -> str:
        if self._id is None:
            raise RuntimeError("MenuItem object was not properly initialized.")
        return self._id

    @id.setter
    def id(self, id: str):
        self._id = id

    @property
    def parent_menu(self) -> "Menu":
        if self._parent_menu is None:
            raise RuntimeError("MenuItem object was not properly initialized.")
        return self._parent_menu

    @parent_menu.setter
    def parent_menu(self, parent_menu: "Menu"):
        self._parent_menu = parent_menu

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

    def get_inactive_inline_button(self, selected_item_id: str, language: MaybeLanguage):
        button_text = any_text_to_str(self.label, language)
        if self.id == selected_item_id:
            button_text = "✅ " + button_text
        return tg.InlineKeyboardButton(
            text=button_text,
            callback_data=INACTIVE_BUTTON_CALLBACK_DATA.new(),
        )


@dataclass(frozen=True)
class MenuConfig:
    back_label: AnyText
    lock_after_termination: bool
    is_text_html: bool = False


class Menu:
    def __init__(
        self,
        text: AnyText,
        menu_items: list[MenuItem],
        config: Optional[MenuConfig] = None,
    ):
        self._id: Optional[str] = None
        self.parent_menu: Optional["Menu"] = None
        self.text = text
        self.menu_items = menu_items
        self._explicit_config = config

    @property
    def config(self) -> MenuConfig:
        if self._explicit_config is not None:
            return self._explicit_config
        elif self.parent_menu is not None:
            return self.parent_menu.config
        else:
            return MenuConfig(
                back_label="back",
                lock_after_termination=True,
            )  # backwards compatibility for pre-config code

    def html_text(self, language: MaybeLanguage) -> str:
        text = any_text_to_str(self.text, language)
        if not self.config.is_text_html:
            text = telegram_html_escape(
                text
            )  # menu handler always uses parse_mode="HTML", so we need to escape plain text
        return text

    @property
    def id(self) -> str:
        if self._id is None:
            raise RuntimeError("Menu object was not properly initialized.")
        return self._id

    @id.setter
    def id(self, id: str):
        self._id = id

    def descendants(self) -> list["Menu"]:
        children = [mi.submenu for mi in self.menu_items if mi.submenu is not None]
        grandchildren: list[Menu] = []
        for menu in children:
            grandchildren.extend(menu.descendants())
        return children + grandchildren

    def get_keyboard_markup(self, language: MaybeLanguage):
        keyboard = [[menu_item.get_inline_button(language)] for menu_item in self.menu_items]
        if self.parent_menu is not None:
            if isinstance(self.config.back_label, str):
                keyboard.append(
                    [MenuItem(label=self.config.back_label, submenu=self.parent_menu).get_inline_button(None)]
                )
            else:
                keyboard.append(
                    [MenuItem(label=self.config.back_label, submenu=self.parent_menu).get_inline_button(language)]
                )
        return tg.InlineKeyboardMarkup(keyboard=keyboard)

    def get_inactive_keyboard_markup(self, selected_item_id: str, language: MaybeLanguage):
        return tg.InlineKeyboardMarkup(
            keyboard=[
                [menu_item.get_inactive_inline_button(selected_item_id, language)] for menu_item in self.menu_items
            ]
        )


@dataclass
class TerminatorContext:
    bot: AsyncTeleBot
    user: tg.User
    menu_message: tg.Message
    terminator: str


@dataclass
class TerminatorResult:
    menu_message_text_update: AnyText
    parse_mode: Optional[str] = None
    lock_menu: Optional[bool] = None  # if set, overrides the default lock_after_termination value in Menu config


class MenuHandler:
    def __init__(
        self,
        bot_prefix: str,
        menu_tree: Menu,
        category_store: Optional[CategoryStore] = None,
        language_store: Optional[LanguageStore] = None,
    ):
        self.category_store = category_store
        self.language_store = language_store

        self.menus_list: list[Menu] = [menu_tree]
        self.menus_list.extend(menu_tree.descendants())

        self.init_menu_ids()
        self.init_parent_menu()

        self.menu_by_id: dict[str, Menu] = {m.id: m for m in self.menus_list}

        self.menu_items_list = self.init_item_ids_and_get_item_list()
        menu_items_with_bound_categories = [mi for mi in self.menu_items_list if mi.bound_category is not None]
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

        self.menu_item_by_id: dict[str, MenuItem] = {mi.id: mi for mi in self.menu_items_list}

        menu_texts = [menu.text for menu in self.menus_list]
        menu_item_labels = [menu_item.label for menu_item in self.menu_items_list]
        menu_back_labels = [menu.config.back_label for menu in self.menus_list]

        for any_text in menu_texts + menu_item_labels + menu_back_labels:
            if self.language_store is not None:
                self.language_store.validate_multilang(any_text)
            else:
                vaildate_singlelang_text(any_text)

        self.logger = logging.getLogger(f"{__name__}[{bot_prefix}]")

    async def get_maybe_language(self, user: tg.User) -> MaybeLanguage:
        if self.language_store is None:
            return None
        else:
            return await self.language_store.get_user_language(user)

    def init_item_ids_and_get_item_list(self) -> list[MenuItem]:
        item_list: list[MenuItem] = []
        for menu in self.menus_list:
            for menu_item in menu.menu_items:
                item_list.append(menu_item)
        for i, menu_item in enumerate(item_list):
            menu_item.id = str(i)
        return item_list

    def init_menu_ids(self):
        for i, menu in enumerate(self.menus_list):
            menu.id = str(i)

    def init_parent_menu(self):
        for menu in self.menus_list:
            for menu_item in menu.menu_items:
                menu_item.parent_menu = menu
                if menu_item.submenu is not None:
                    menu_item.submenu.parent_menu = menu

    def get_main_menu(self):
        return self.menu_by_id["0"]

    async def start_menu(self, bot: AsyncTeleBot, user: tg.User) -> None:
        """Send menu message to the user, starting at the main menu"""
        main_menu = self.get_main_menu()
        language = await self.language_store.get_user_language(user) if self.language_store is not None else None
        await bot.send_message(
            chat_id=user.id,
            text=main_menu.html_text(language),
            reply_markup=main_menu.get_keyboard_markup(language),
            parse_mode="HTML",
        )

    def setup(
        self,
        bot: AsyncTeleBot,
        on_terminal_menu_option_selected: Callable[[TerminatorContext], Awaitable[Optional[TerminatorResult]]],
    ):
        @bot.callback_query_handler(callback_data=ROUTE_MENU_CALLBACK_DATA, auto_answer=True)
        async def handle_menu(call: tg.CallbackQuery):
            user = call.from_user
            language = await self.get_maybe_language(user)
            data = ROUTE_MENU_CALLBACK_DATA.parse(call.data)
            route_to = data["route_to"]
            menu = self.menu_by_id[route_to]
            try:
                await bot.edit_message_text(
                    text=menu.html_text(language),
                    chat_id=user.id,
                    message_id=call.message.id,
                    reply_markup=menu.get_keyboard_markup(language),
                    parse_mode="HTML",
                )
            except ApiHTTPException as e:
                self.logger.info(f"Error editing message text and reply markup: {e!r}")

        @bot.callback_query_handler(callback_data=INACTIVE_BUTTON_CALLBACK_DATA, auto_answer=True)
        async def handle_inactive_menu(call: tg.CallbackQuery):
            pass

        @bot.callback_query_handler(callback_data=TERMINATE_MENU_CALLBACK_DATA, auto_answer=True)
        async def handle_terminator(call: tg.CallbackQuery):
            user = call.from_user
            language = await self.get_maybe_language(user)
            data = TERMINATE_MENU_CALLBACK_DATA.parse(call.data)
            selected_menu_item_id = data["id"]

            selected_menu_item = self.menu_item_by_id[selected_menu_item_id]
            terminator = selected_menu_item.terminator
            if terminator is None:
                self.logger.error(f"handle_terminator got non-terminating menu item: {selected_menu_item}")
                return

            if selected_menu_item.bound_category is not None and self.category_store is not None:
                await self.category_store.save_user_category(user, selected_menu_item.bound_category)

            try:
                terminator_callback_result = await on_terminal_menu_option_selected(
                    TerminatorContext(bot, user, call.message, terminator)
                )
            except Exception:
                self.logger.exception("Unexpected error in on_terminal_menu_option_selected callback, ignoring")
                terminator_callback_result = None

            if terminator_callback_result is not None:
                try:
                    await bot.edit_message_text(
                        text=any_text_to_str(terminator_callback_result.menu_message_text_update, language),
                        chat_id=call.message.chat.id,
                        message_id=call.message.id,
                        parse_mode=terminator_callback_result.parse_mode,
                    )
                except Exception:
                    self.logger.info(
                        f"Eror editing menu message with callback returned value {terminator_callback_result!r}",
                        exc_info=True,
                    )

            current_menu = self.menu_by_id[selected_menu_item.parent_menu.id]
            lock_menu = (
                terminator_callback_result.lock_menu
                if terminator_callback_result is not None and terminator_callback_result.lock_menu is not None
                else current_menu.config.lock_after_termination
            )
            if lock_menu:
                try:
                    await bot.edit_message_reply_markup(
                        chat_id=user.id,
                        message_id=call.message.id,
                        reply_markup=current_menu.get_inactive_keyboard_markup(selected_menu_item_id, language),
                    )
                except ApiHTTPException:
                    self.logger.info("Error editing message reply markup", exc_info=True)
