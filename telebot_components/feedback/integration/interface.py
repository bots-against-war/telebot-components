import abc
import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, NoReturn, Optional

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.runner import AuxBotEndpoint

from telebot_components.stores.category import Category


@dataclass
class MessageRepliedFromIntegrationContext:
    """
    Context object used to notify the feedback handler and other integrations
    about the user reply being sent throught the integration.
    """

    integration: "FeedbackHandlerIntegration"
    origin_chat_id: int
    reply_to_forwarded_message_id: int

    # these values are integration-specific and used by feedback handler opaquely
    reply_author: Optional[str]
    reply_text: Optional[str]
    reply_link: Optional[str]


MessageRepliedFromIntegrationCallback = Callable[[MessageRepliedFromIntegrationContext], Awaitable[Any]]


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
    async def handle_admin_message(
        self,
        message: tg.Message,
        to_user_id: int,
        bot: AsyncTeleBot,
    ) -> None:
        """
        The method is invoked when admins respond to users in the main admin chat or in other integrations.

        - `message` object should be generally used only as a media container,
          i.e. `from_user` and `chat` attributes may not be meaningful
        """
        ...

    def register_message_replied_callback(self, new: MessageRepliedFromIntegrationCallback) -> None:
        self._message_replied_callback = new

    @property
    def message_replied_callback(self) -> Optional[MessageRepliedFromIntegrationCallback]:
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
