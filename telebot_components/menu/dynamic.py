from dataclasses import dataclass
from typing import Awaitable, Callable

from telebot_components.menu.menu import Menu


@dataclass
class DynamicMenuContext:
    user_id: int
    chat: int | str


class DynamicMenu:
    def __init__(self, menu_factory: Callable[[DynamicMenuContext], Awaitable[Menu]]) -> None:
        self.factory = menu_factory

    # async def start
