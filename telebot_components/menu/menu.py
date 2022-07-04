import logging
from typing import Optional

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.callback_data import CallbackData


ROUTE_MENU_CALLBACK_DATA = CallbackData("route_to", prefix="menu")
TERMINATE_MENU_CALLBACK_DATA = CallbackData("id", prefix="terminator")
INACTIVE_BUTTON_CALLBACK_DATA = CallbackData(prefix="inactive_button")


class MenuItem:
    id: Optional[str]
    parent_menu: Optional["Menu"]

    def __init__(self, label: str, submenu: Optional["Menu"] = None, terminator: Optional[str] = None):
        self.label = label
        self.submenu = submenu
        self.terminator = terminator

    def get_inline_button(self):
        if self.submenu is not None:
            return tg.InlineKeyboardButton(
                text=self.label,
                callback_data=ROUTE_MENU_CALLBACK_DATA.new(self.submenu.id),
            )
        else:
            return tg.InlineKeyboardButton(
                text=self.label,
                callback_data=TERMINATE_MENU_CALLBACK_DATA.new(self.id),
            )

    def get_blocked_inline_button(self, selected_item_id: str):
        button_text = self.label
        if self.id == selected_item_id:
            button_text = "✅ " + button_text
        return tg.InlineKeyboardButton(
            text=button_text,
            callback_data=INACTIVE_BUTTON_CALLBACK_DATA.new(),
        )


class Menu:
    id: Optional[str]
    parent_menu: Optional["Menu"]

    def __init__(
        self,
        text: str,
        menu_items: list[MenuItem],
    ):
        self.id = None
        self.parent_menu = None
        self.text = text
        self.menu_items = menu_items

    def descendants(self) -> list["Menu"]:
        children = [mi.submenu for mi in self.menu_items if mi.submenu is not None]
        grandchildren: list[Menu] = []
        for menu in children:
            grandchildren.extend(menu.descendants())
        return children + grandchildren

    def get_keyboard_markup(self):
        keyboard = [[menu_item.get_inline_button()] for menu_item in self.menu_items]
        if self.parent_menu is not None:
            keyboard.append([MenuItem(label="back", submenu=self.parent_menu).get_inline_button()])
        return tg.InlineKeyboardMarkup(keyboard=keyboard)

    def get_inactive_keyboard_markup(self, selected_item_id: str):
        return tg.InlineKeyboardMarkup(
            keyboard=[[menu_item.get_blocked_inline_button(selected_item_id)] for menu_item in self.menu_items]
        )


class MenuHandler:
    def __init__(
        self,
        bot_prefix: str,
        menu_tree: Menu,
    ):
        self.menu_list: list[Menu] = [menu_tree]
        self.menu_list.extend(menu_tree.descendants())

        self.init_menu_ids()
        self.init_parent_menu()

        self.menu_items_list = self.init_item_ids_and_get_item_list()

        self.logger = logging.getLogger(f"{__name__}.{bot_prefix}")

    def init_item_ids_and_get_item_list(self) -> list[MenuItem]:
        item_list: list[MenuItem] = []
        for menu in self.menu_list:
            for menu_item in menu.menu_items:
                item_list.append(menu_item)

        for i, menu_item in enumerate(item_list):
            menu_item.id = str(i)

        return item_list

    def init_menu_ids(self):
        for i, menu in enumerate(self.menu_list):
            menu.id = str(i)

    def init_parent_menu(self):
        for menu in self.menu_list:
            for menu_item in menu.menu_items:
                menu_item.parent_menu = menu
                if menu_item.submenu is not None:
                    menu_item.submenu.parent_menu = menu

    # TODO throw error on name duplication
    def get_menu_by_id(self, id: str) -> Menu:
        for menu in self.menu_list:
            if menu.id == id:
                return menu

    def get_menu_item_by_id(self, id: str) -> MenuItem:
        for menu_item in self.menu_items_list:
            if menu_item.id == id:
                return menu_item

    def get_main_menu(self):
        return self.get_menu_by_id("0")

    def setup(self, bot: AsyncTeleBot, on_terminal_menu_option_selected):
        @bot.callback_query_handler(callback_data=ROUTE_MENU_CALLBACK_DATA)
        async def route_menu(call: tg.CallbackQuery):
            user = call.from_user
            data = ROUTE_MENU_CALLBACK_DATA.parse(call.data)
            route_to = data["route_to"]

            await bot.answer_callback_query(call.id)

            menu = self.get_menu_by_id(route_to)
            await bot.edit_message_text(
                text=menu.text,
                chat_id=user.id,
                message_id=call.message.id,
                reply_markup=menu.get_keyboard_markup(),
            )

        @bot.callback_query_handler(callback_data=INACTIVE_BUTTON_CALLBACK_DATA)
        async def route_menu(call: tg.CallbackQuery):
            await bot.answer_callback_query(call.id)

        @bot.callback_query_handler(callback_data=TERMINATE_MENU_CALLBACK_DATA)
        async def route_terminator(call: tg.CallbackQuery):
            user = call.from_user
            data = TERMINATE_MENU_CALLBACK_DATA.parse(call.data)
            selected_menu_item_id = data["id"]

            selected_menu_item = self.get_menu_item_by_id(selected_menu_item_id)
            terminator = selected_menu_item.terminator
            current_menu = self.get_menu_by_id(selected_menu_item.parent_menu.id)

            await bot.answer_callback_query(call.id)
            await bot.edit_message_text(
                text=current_menu.text,
                chat_id=user.id,
                message_id=call.message.id,
                reply_markup=current_menu.get_inactive_keyboard_markup(selected_menu_item_id),
            )

            await on_terminal_menu_option_selected(bot, user, call.message, terminator)
