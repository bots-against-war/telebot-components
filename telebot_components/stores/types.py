from typing import Protocol, TypeVar

from telebot import AsyncTeleBot, types

OptionT = TypeVar("OptionT", contravariant=True)


class OnOptionSelected(Protocol[OptionT]):
    async def __call__(
        self, bot: AsyncTeleBot, language_menu_message: types.Message, user: types.User, new_option: OptionT
    ) -> None:
        pass
