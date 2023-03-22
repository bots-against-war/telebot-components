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
    main_admin_chat_message_id: int


UserMessageRepliedFromIntegrationCallback = Callable[[UserMessageRepliedFromIntegrationEvent], Awaitable[Any]]


@dataclass
class FeedbackIntegrationBackgroundContext:
    """Context object passed to"""

    # this options are set only when running within webhook app

    # app's base public url
    base_url: Optional[str]
    # future that is resolved when the webhook app's server is ready and listening
    server_listening: Optional[asyncio.Future[None]]


class FeedbackHandlerIntegration(abc.ABC):
    """Interface class, extending the default FeedbackHandler behavior by

    - additionally handling all user messages (usually exporting them to some kind of new medium)
    - providing an additional admin input, augumenting or even replacing the default admin chat
    """

    def help_message_section(self) -> Optional[str]:
        """If this method returns non-empty string, it is added to admin chat's /help"""
        return None

    @abc.abstractmethod
    def name(self) -> str:
        """Human-readable integration name"""
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
        - `admin_chat_message_id` allows backlinking to the original admin chat
        """
        ...

    @abc.abstractmethod
    async def handle_admin_message_elsewhere(
        self,
        message: tg.Message,
        to_user_id: int,
        integration: Optional["FeedbackHandlerIntegration"],
        bot: AsyncTeleBot,
    ) -> None:
        """
        The method is invoked when admins respond to users in the main admin chat or in other integrations.

        - `message` message in the main admin chat representing the admin message (whenever it originated from);
          should be generally used only as a text/media container
        """
        ...

    def register_message_replied_callback(self, new: UserMessageRepliedFromIntegrationCallback) -> None:
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
        """Optional coroutine that should be run as a background job along with the bot.
        Receives the 'server is listening' future that can be awaited if the initialization requires the
        listening HTTP server (including aux_endpoints).
        """
        pass

    async def setup(self, bot: AsyncTeleBot) -> None:
        """Optional hook for the integration to set up all the bot-related logic it needs (e.g. handlers)"""
        pass
