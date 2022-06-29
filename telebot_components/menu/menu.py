import copy
import logging
from dataclasses import dataclass
from typing import Optional
import enum

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.callback_data import CallbackData


class Terminators(enum.Enum):
    Agitation = 1
    Letter = 2
    Strike = 3
    Have_initiative = 4
    Search_initiative = 5
    Read_info = 6


ROUTE_MENU_CALLBACK_DATA = CallbackData("route_to", prefix="menu")
TERMINATE_MENU_CALLBACK_DATA = CallbackData("terminator", "housing_menu_name", prefix="terminator")


@dataclass
class MenuItem:
    label: str
    submenu: Optional["Menu"] = None
    terminator: Optional[Terminators] = None
    menu_that_houses_me: Optional["Menu"] = None

    def get_inline_button(self):
        if self.submenu is not None:
            return tg.InlineKeyboardButton(
                text=self.label,
                callback_data=ROUTE_MENU_CALLBACK_DATA.new(self.submenu.name),
            )
        else:
            return tg.InlineKeyboardButton(
                text=self.label,
                callback_data=TERMINATE_MENU_CALLBACK_DATA.new(self.terminator, self.menu_that_houses_me.name),
            )

    def get_blocked_inline_button(self):
        return tg.InlineKeyboardButton(
            text=self.label,
            callback_data=ROUTE_MENU_CALLBACK_DATA.new("the_end"),
        )


@dataclass
class Menu:
    name: Optional[str]

    def __init__(
        self,
        text: str,
        menu_items: list[MenuItem],
    ):
        self.name = None
        self.text = text
        self.menu_items = menu_items

    def descendants(self) -> list["Menu"]:
        children = [mi.submenu for mi in self.menu_items if mi.submenu is not None]
        grandchildren: list[Menu] = []
        for menu in children:
            grandchildren.extend(menu.descendants())
        return children + grandchildren

    def get_keyboard_markup(self):
        return tg.InlineKeyboardMarkup(keyboard=[[menu_item.get_inline_button()] for menu_item in self.menu_items])

    def get_blocked_keyboard_markup(self):
        return tg.InlineKeyboardMarkup(
            keyboard=[[menu_item.get_blocked_inline_button()] for menu_item in self.menu_items]
        )


class MenuHandler:
    def __init__(
        self,
        bot_prefix: str,
        menu_tree: Menu,
    ):
        self.menu_list = menu_tree.descendants()
        menu_tree.name = "main_menu"
        self.menu_list.append(menu_tree)

        self.init_back_buttons_and_housing_menu()
        self.init_menu_ids()
        self.logger = logging.getLogger(f"{__name__}.{bot_prefix}")

    def init_menu_ids(self):
        id = 0
        for menu in self.menu_list:
            if menu.name is None:
                menu.name = str(id)
                id = id + 1

    def init_back_buttons_and_housing_menu(self):
        for menu in self.menu_list:
            for menu_item in menu.menu_items:
                menu_item.menu_that_houses_me = menu
                if menu_item.submenu is not None:
                    menu_item.submenu.menu_items.append(MenuItem(label="Вернуться назад", submenu=menu))

    # TODO throw error on name duplication
    def get_menu_by_name(self, name: str) -> Menu:
        for menu in self.menu_list:
            if menu.name == name:
                return menu

    def setup(self, bot: AsyncTeleBot):
        @bot.callback_query_handler(callback_data=ROUTE_MENU_CALLBACK_DATA)
        async def route_menu(call: tg.CallbackQuery):
            user = call.from_user
            data = ROUTE_MENU_CALLBACK_DATA.parse(call.data)
            route_to = data["route_to"]

            await bot.answer_callback_query(call.id)
            if route_to == "the_end":
                return

            menu = self.get_menu_by_name(route_to)
            await bot.edit_message_text(
                text=menu.text,
                chat_id=user.id,
                message_id=call.message.id,
                reply_markup=menu.get_keyboard_markup(),
            )

        @bot.callback_query_handler(callback_data=TERMINATE_MENU_CALLBACK_DATA)
        async def route_terminator(call: tg.CallbackQuery):
            user = call.from_user
            data = TERMINATE_MENU_CALLBACK_DATA.parse(call.data)
            terminator = data["terminator"]
            housing_menu_name = data["housing_menu_name"]

            current_menu: Menu = copy.deepcopy(self.get_menu_by_name(housing_menu_name))
            for menu_item in current_menu.menu_items:
                if str(menu_item.terminator) == terminator:
                    menu_item.label = "✅ " + menu_item.label
                    break

            await bot.answer_callback_query(call.id)
            await bot.edit_message_text(
                text=current_menu.text,
                chat_id=user.id,
                message_id=call.message.id,
                reply_markup=current_menu.get_blocked_keyboard_markup(),
            )

            # TODO replace message sending with exact flow initiation
            if (
                terminator == str(Terminators.Agitation)
                or terminator == str(Terminators.Letter)
                or terminator == str(Terminators.Strike)
            ):
                await bot.send_message(
                    user.id,
                    "Сейчас мы инициируем составку и отправку Вашего отчета",
                )
            elif terminator == str(Terminators.Have_initiative):
                await bot.send_message(
                    user.id,
                    "Начинаем флоу добавки Вашей инициативы в нашу внутреннюю АИС",
                )
            elif terminator == str(Terminators.Search_initiative):
                await bot.send_message(
                    user.id,
                    "Мы попробуем найти Вам соратников, потому что заниматься антивоенным активизмом веселее с друзьями",
                )
            elif terminator == str(Terminators.Read_info):
                await bot.send_message(
                    user.id,
                    "ИНФО",
                )
