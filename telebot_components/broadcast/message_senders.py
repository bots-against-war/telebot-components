from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Generic, Type, TypeVar

from telebot import AsyncTeleBot
from telebot import types as tg

from telebot_components.broadcast.subscriber import Subscriber

CustomMessageSenderContextT = TypeVar("CustomMessageSenderContextT")


@dataclass
class MessageSenderContext(Generic[CustomMessageSenderContextT]):
    bot: AsyncTeleBot
    subscriber: Subscriber
    previosly_sent_message_ids: list[int] | None
    custom: CustomMessageSenderContextT | None


@dataclass
class MessageSenderResult:
    success: bool
    sent_message_ids: list[int] | None


class AbstractMessageSender(ABC, Generic[CustomMessageSenderContextT]):
    _registry: dict[str, Type["AbstractMessageSender"]] = dict()

    def __init_subclass__(cls) -> None:
        cls._registry[cls.concrete_name()] = cls

    @classmethod
    @abstractmethod
    def concrete_name(cls) -> str: ...

    @abstractmethod
    def dump_concrete(self) -> dict: ...

    def dump(self) -> dict:
        return {
            "concrete_dump": self.dump_concrete(),
            "concrete_name": self.concrete_name(),
        }

    @classmethod
    @abstractmethod
    def load_concrete(cls, dump: dict) -> "AbstractMessageSender": ...

    @classmethod
    def load(cls, dump: dict) -> "AbstractMessageSender":
        concrete_name = dump["concrete_name"]
        type_ = cls._registry[concrete_name]
        return type_.load_concrete(dump["concrete_dump"])

    @abstractmethod
    async def send(self, context: MessageSenderContext[CustomMessageSenderContextT]) -> MessageSenderResult | None: ...


class DataclassMessageSender(AbstractMessageSender[CustomMessageSenderContextT]):
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
class MessageCopySender(DataclassMessageSender[Any]):
    source_chat_id: int
    source_message_id: int

    @classmethod
    def concrete_name(cls) -> str:
        return "MessageCopySender"

    @classmethod
    def from_message(cls, message: tg.Message) -> "MessageCopySender":
        return MessageCopySender(
            source_chat_id=message.chat.id,
            source_message_id=message.id,
        )

    async def send(self, context: MessageSenderContext[Any]) -> MessageSenderResult:
        res = await context.bot.copy_message(
            chat_id=context.subscriber["user_id"],
            from_chat_id=self.source_chat_id,
            message_id=self.source_message_id,
        )
        return MessageSenderResult(success=True, sent_message_ids=[res.message_id])


@dataclass(frozen=True)
class TextSender(DataclassMessageSender[Any]):
    text: str
    parse_mode: str = "HTML"

    @classmethod
    def concrete_name(cls) -> str:
        return "TextSender"

    async def send(self, context: MessageSenderContext[Any]) -> MessageSenderResult:
        message = await context.bot.send_message(
            chat_id=context.subscriber["user_id"],
            text=self.text,
            parse_mode=self.parse_mode,
        )
        return MessageSenderResult(success=True, sent_message_ids=[message.id])


class DeleteLastBroadcastSender(AbstractMessageSender):
    @classmethod
    def concrete_name(cls) -> str:
        return "PreviousMessageDeleter"

    def dump_concrete(self) -> dict:
        return {}

    @classmethod
    def load_concrete(cls, dump: dict) -> "AbstractMessageSender":
        return DeleteLastBroadcastSender()

    async def send(self, context: MessageSenderContext[Any]) -> MessageSenderResult:
        if context.previosly_sent_message_ids is None:
            return MessageSenderResult(success=False, sent_message_ids=None)
        else:
            success = await context.bot.delete_messages(
                chat_id=context.subscriber["user_id"], message_ids=context.previosly_sent_message_ids
            )
            return MessageSenderResult(success=success, sent_message_ids=None)
