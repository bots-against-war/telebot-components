import abc
import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.runner import AuxBotEndpoint

from telebot_components.feedback.types import UserMessageRepliedEvent
from telebot_components.stores.category import Category


@dataclass
class UserMessageRepliedFromIntegrationEvent(UserMessageRepliedEvent):
    integration: "FeedbackHandlerIntegration"


UserMessageRepliedFromIntegrationCallback = Callable[[UserMessageRepliedFromIntegrationEvent], Awaitable[Any]]


@dataclass
class FeedbackIntegrationBackgroundContext:
    """Context object passed to feedback integration background jobs"""

    # this options are set only when running within webhook app

    # app's base public url
    base_url: Optional[str]
    # future that is resolved when the webhook app's server is ready and listening
    server_listening: Optional[asyncio.Future[None]]


class FeedbackHandlerIntegration(abc.ABC):
    """
    Interface class for feedback handler integrations: components extending the default
    FeedbackHandler behavior by
      - additionally handling all user messages (e.g. exporting them to some new format / medium)
      - receiving an additional admin-side input, notifying the main admin chat and other
        integrations about it
    """

    def help_message_section(self) -> Optional[str]:
        """If this method returns non-empty string, it is added to admin chat's /help"""
        return None

    @abc.abstractmethod
    def name(self) -> str:
        """Human-readable integration name to display to admins"""
        ...

    @abc.abstractmethod
    async def handle_user_message(
        self,
        message: tg.Message,
        admin_chat_message_id: int,
        category: Optional[Category],
        bot: AsyncTeleBot,
    ) -> None:
        """
        The method is invoked on all user messages (including emulated) passing through the feedback handler.

        - `message` object is an original user's message
        - `admin_chat_message_id` allows backlinking to the main admin chat
        """
        ...

    @abc.abstractmethod
    async def handle_user_message_replied_elsewhere(self, event: UserMessageRepliedEvent) -> None:
        """
        The method is invoked when admins have replied to user message in the main admin chat or
        in other integrations (in which case even will be UserMessageRepliedFromIntegrationEvent).
        """
        ...

    def set_message_replied_callback(self, new: UserMessageRepliedFromIntegrationCallback) -> None:
        self._message_replied_callback = new

    @property
    def message_replied_callback(self) -> Optional[UserMessageRepliedFromIntegrationCallback]:
        try:
            return self._message_replied_callback
        except AttributeError:
            return None

    async def aux_endpoints(self) -> list[AuxBotEndpoint]:
        """
        Optional hook for the integration to specify aux endpoints it requires for functioning (e.g. webhook
        for some external service)
        """
        return []

    async def background_job(self, context: FeedbackIntegrationBackgroundContext) -> Any:
        """
        Optional coroutine that should be run as a background job along with the bot.
        Receives the 'server is listening' future that can be awaited if the initialization requires the
        listening HTTP server (including aux_endpoints).
        """
        pass

    async def setup(self, bot: AsyncTeleBot) -> None:
        """Optional hook for the integration to set up all the bot-related logic it needs (e.g. handlers)"""
        pass
