from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, is_dataclass
from typing import Type

from telebot import AsyncTeleBot
from telebot import types as tg

from telebot_components.broadcast.subscriber import Subscriber


@dataclass
class MessageSenderContext:
    bot: AsyncTeleBot
    subscriber: Subscriber


class AbstractMessageSender(ABC):
    _registry: dict[str, Type["AbstractMessageSender"]] = dict()

    def __init_subclass__(cls) -> None:
        cls._registry[cls.concrete_name()] = cls

    @classmethod
    @abstractmethod
    def concrete_name(self) -> str: ...

    @abstractmethod
    def dump_concrete(self) -> dict: ...

    def dump(self) -> dict:
        return {
            "concrete_dump": self.dump_concrete(),
            "concrete_name": self.concrete_name(),
        }

    @classmethod
    @abstractmethod
    def load_concrete(self, dump: dict) -> "AbstractMessageSender": ...

    @classmethod
    def load(cls, dump: dict) -> "AbstractMessageSender":
        concrete_name = dump["concrete_name"]
        type_ = cls._registry[concrete_name]
        return type_.load_concrete(dump["concrete_dump"])

    @abstractmethod
    async def send(self, context: MessageSenderContext) -> None: ...


class DataclassMessageSender(AbstractMessageSender):
    def __new__(cls, *args, **kwargs):
        if not is_dataclass(cls):
            raise RuntimeError("DataclassMessageSender subclasses must be dataclasses")
        return super().__new__(cls)

    @classmethod
    def load_concrete(cls, dump: dict) -> "DataclassMessageSender":
        return cls(**dump)

    def dump_concrete(self) -> dict:
        return asdict(self)  # type: ignore


@dataclass(frozen=True)
class MessageCopySender(DataclassMessageSender):
    source_chat_id: int
    source_message_id: int

    @classmethod
    def concrete_name(self) -> str:
        return "MessageCopySender"

    @classmethod
    def from_message(cls, message: tg.Message) -> "MessageCopySender":
        return MessageCopySender(
            source_chat_id=message.chat.id,
            source_message_id=message.id,
        )

    async def send(self, context: MessageSenderContext) -> None:
        await context.bot.copy_message(
            chat_id=context.subscriber["user_id"],
            from_chat_id=self.source_chat_id,
            message_id=self.source_message_id,
        )


@dataclass(frozen=True)
class TextSender(DataclassMessageSender):
    text: str
    parse_mode: str = "HTML"

    @classmethod
    def concrete_name(self) -> str:
        return "TextSender"

    async def send(self, context: MessageSenderContext) -> None:
        await context.bot.send_message(
            chat_id=context.subscriber["user_id"],
            text=self.text,
            parse_mode=self.parse_mode,
        )
