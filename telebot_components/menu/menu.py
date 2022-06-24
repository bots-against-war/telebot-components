import logging
from dataclasses import dataclass
from typing import Optional

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.callback_data import CallbackData


@dataclass
class MenuItem:
    label: str
    submenu: Optional["Menu"] = None

    def get_inline_button(self):
        if self.submenu is not None:
            return tg.InlineKeyboardButton(
                text=self.label, callback_data=MenuHandler.route_menu_callback_data.new(self.submenu.name)
            )
        else:
            return tg.InlineKeyboardButton(
                text=self.label, callback_data=MenuHandler.route_menu_callback_data.new("main_menu")
            )


@dataclass
class Menu:
    def __init__(
        self,
        name: str,
        text: str,
        menu_items: list[MenuItem],
    ):
        self.name = name
        self.text = text
        self.menu_items = menu_items
        if name != "main_menu":
            self.menu_items.append(MenuItem("🔙"))

    def descendants(self) -> list["Menu"]:
        children = [mi.submenu for mi in self.menu_items if mi.submenu is not None]
        grandchildren: list[Menu] = []
        for menu in children:
            grandchildren.extend(menu.descendants())
        return children + grandchildren

    def get_keyboard_markup(self):
        return tg.InlineKeyboardMarkup(keyboard=[[menu_item.get_inline_button()] for menu_item in self.menu_items])


class MenuHandler:
    route_menu_callback_data = CallbackData("route_to", prefix="menu")

    def __init__(
        self,
        bot_prefix: str,
        menu_tree: Menu,
    ):
        self.menu_list = menu_tree.descendants()
        self.menu_list.append(menu_tree)
        self.logger = logging.getLogger(f"{__name__}.{bot_prefix}")

    def get_menu_by_name(self, name: str) -> Menu:
        for m in self.menu_list:
            if m.name == name:
                return m

    def setup(self, bot: AsyncTeleBot):
        @bot.callback_query_handler(callback_data=self.route_menu_callback_data)
        async def route_menu(call: tg.CallbackQuery):
            user = call.from_user
            data = self.route_menu_callback_data.parse(call.data)
            route_to = data["route_to"]

            menu = self.get_menu_by_name(route_to)

            await bot.answer_callback_query(call.id)
            await bot.edit_message_text(
                text=menu.text,
                chat_id=user.id,
                message_id=call.message.id,
                reply_markup=menu.get_keyboard_markup(),
                parse_mode="Markdown",
            )
