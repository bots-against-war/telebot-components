from typing import Protocol, TypeVar

from telebot import AsyncTeleBot
from telebot import types as tg

OptionT = TypeVar("OptionT", contravariant=True)


class OnOptionSelected(Protocol[OptionT]):
    """Legacy type for callbacks, new code should use contexts"""

    async def __call__(self, bot: AsyncTeleBot, menu_message: tg.Message, user: tg.User, new_option: OptionT) -> None:
        pass
