import logging
from typing import Optional

from telebot import AsyncTeleBot
from telebot import types as tg

from telebot_components.feedback import FeedbackHandler
from telebot_components.feedback.integration.interface import (
    FeedbackHandlerIntegration,
    FeedbackIntegrationBackgroundContext,
    UserMessageRepliedFromIntegrationEvent,
)
from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.category import Category
from telebot_components.stores.generic import KeyValueStore


class _MainFeedbackHandlerIntegration(FeedbackHandlerIntegration):
    """
    Internal-only integration class representing main feedback handler as if it was and integration to the aux;
    """

    def __init__(self, aux: "AuxFeedbackHandlerIntegration") -> None:
        self.aux = aux
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}[{self.aux.feedback_handler.name}]")

    def name(self) -> str:
        return "main admin chat"

    async def handle_user_message(
        self,
        message: tg.Message,
        admin_chat_message_id: int,
        category: Optional[Category],
        bot: AsyncTeleBot,
    ) -> None:
        pass

    async def handle_admin_message_elsewhere(self, message: tg.Message, to_user_id: int, bot: AsyncTeleBot) -> None:
        """This message is invoked when admins answer forwarded message in aux admin chat; it uses saved aux -> main msg id
        mapping to notify the main feedback handler about admin action.
        """
        if self.aux.message_replied_callback is None:
            return
        if message.reply_to_message is None:
            self.logger.error(f"Message received by handle_admin_message_elsewhere method is not a reply: {message!r}")
            return
        main_chat_message_id = await self.aux.main_message_id_by_aux_message_id_store.load(message.reply_to_message.id)
        if main_chat_message_id is None:
            self.logger.error(f"Message in aux admin chat has no saved main admin chat message id")
            return
        await self.aux.message_replied_callback(
            UserMessageRepliedFromIntegrationEvent(
                bot=bot,
                integration=self.aux,
                origin_chat_id=to_user_id,
                main_admin_chat_message_id=main_chat_message_id,
                reply_author=message.from_user.first_name,
                reply_text=message.text_content or "<attachments>",
                reply_link=None,
            )
        )


class AuxFeedbackHandlerIntegration(FeedbackHandlerIntegration):
    """One feedback (aux) handler plugged as integration into another (main) one"""

    def __init__(self, feedback_handler: FeedbackHandler, bot_prefix: str, redis: RedisInterface) -> None:
        self.feedback_handler = feedback_handler
        if self.feedback_handler.integrations:
            raise ValueError("Aux feedback handler can't have integrations itself!")
        self.feedback_handler.integrations.append(_MainFeedbackHandlerIntegration(self))

        self.main_message_id_by_aux_message_id_store = KeyValueStore[int](
            name=f"main-by-{self.feedback_handler.name}-msg-id",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=self.feedback_handler.origin_chat_id_store.expiration_time,
            dumper=str,
            loader=int,
        )

    def name(self) -> str:
        return self.feedback_handler.name or "<unnamed feedback handler>"

    async def handle_user_message(
        self,
        message: tg.Message,
        admin_chat_message_id: int,
        category: Optional[Category],
        bot: AsyncTeleBot,
    ) -> None:
        async def message_forwarder() -> tuple[list[int], tg.Message]:
            copied = await bot.copy_message(
                chat_id=self.feedback_handler.admin_chat_id,
                from_chat_id=message.chat.id,
                message_id=admin_chat_message_id,
            )
            await self.main_message_id_by_aux_message_id_store.save(copied.message_id, admin_chat_message_id)
            return [copied.message_id], message

        async def noop(*args, **kwargs) -> None:
            pass

        await self.feedback_handler._handle_user_message(
            bot=bot,
            user=message.from_user,
            message_forwarder=message_forwarder,
            user_replier=noop,
            export_to_integrations=True,
        )

    async def handle_admin_message_elsewhere(self, message: tg.Message, to_user_id: int, bot: AsyncTeleBot) -> None:
        """To handle admin messages from elsewhere (main admin chat / other integrations), we just pretend that
        they come from aux feedback handler's integration and use its dedicated callback for that.
        """
        await self.feedback_handler.message_replied_from_integration_callback(
            context=UserMessageRepliedFromIntegrationEvent(
                bot=bot,
                integration=
            )
        )
        pass

    async def setup(self, bot: AsyncTeleBot) -> None:
        await self.feedback_handler.setup_admin_chat_handlers(bot)
