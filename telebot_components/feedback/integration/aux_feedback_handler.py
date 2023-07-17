import copy
import logging
from typing import Optional

from telebot import AsyncTeleBot
from telebot import types as tg

from telebot_components.feedback import (
    FeedbackHandler,
    MessageForwarderResult,
    UserAnonymization,
)
from telebot_components.feedback.integration.interface import (
    FeedbackHandlerIntegration,
    UserMessageRepliedFromIntegrationEvent,
)
from telebot_components.feedback.types import UserMessageRepliedEvent
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
        admin_chat_message: tg.Message,
        user: tg.User,
        user_message: Optional[tg.Message],
        category: Optional[Category],
        bot: AsyncTeleBot,
    ) -> None:
        pass

    async def handle_user_message_replied_elsewhere(self, event: UserMessageRepliedEvent) -> None:
        """This method just dispatches event from aux chat feedback handler to the main chat and other integrations"""
        if self.aux.message_replied_callback is None:
            return
        # the terminology is reversed here, because for integration's POV, aux chat is main and vice versa
        aux_admin_chat_message_id = event.main_admin_chat_message_id
        main_admin_chat_message_id = await self.aux.main_by_aux_admin_chat_message_id_store.load(
            aux_admin_chat_message_id
        )
        if main_admin_chat_message_id is None:
            self.logger.info("Message in aux admin chat has no saved main admin chat message id, ignoring")
            return
        await self.aux.message_replied_callback(
            UserMessageRepliedFromIntegrationEvent(
                bot=event.bot,
                origin_chat_id=event.origin_chat_id,
                reply_text=event.reply_text,
                reply_has_attachments=event.reply_has_attachments,
                reply_author=event.reply_author,
                reply_link=event.reply_link,
                main_admin_chat_message_id=main_admin_chat_message_id,
                integration=self.aux,
            )
        )


class AuxFeedbackHandlerIntegration(FeedbackHandlerIntegration):
    """One feedback (aux) handler plugged as integration into another (main) one"""

    def __init__(self, feedback_handler: FeedbackHandler, bot_prefix: str, redis: RedisInterface) -> None:
        self.feedback_handler = feedback_handler
        if self.feedback_handler.integrations:
            raise ValueError("Aux feedback handler can't have integrations itself!")
        self.feedback_handler.integrations.append(_MainFeedbackHandlerIntegration(self))

        # saving bi-directional mapping between main and aux admin chat message ids
        self.main_by_aux_admin_chat_message_id_store = KeyValueStore[int](
            name=f"main-by-aux-msg-id-{self.feedback_handler.name}",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=self.feedback_handler.origin_chat_id_store.expiration_time,
            dumper=str,
            loader=int,
        )
        self.aux_by_main_admin_chat_message_id_store = KeyValueStore[int](
            name=f"aux-by-main-msg-id-{self.feedback_handler.name}",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=self.feedback_handler.origin_chat_id_store.expiration_time,
            dumper=str,
            loader=int,
        )
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}[{self.feedback_handler.name}]")

    def name(self) -> str:
        return self.feedback_handler.name or "<unnamed feedback handler>"

    async def handle_user_message(
        self,
        admin_chat_message: tg.Message,
        user: tg.User,
        user_message: Optional[tg.Message],
        # NOTE: if two feedback handlers share CategoryStore, the categoory will be loaded by
        #       the aux feedback handler by itself, so we can ignore the `category` argument
        category: Optional[Category],
        bot: AsyncTeleBot,
    ) -> None:
        if user_message is not None:
            aux_admin_message_id = await self.feedback_handler.handle_user_message(
                user_message,
                bot,
                reply_to_user=False,
            )
            if aux_admin_message_id:  # if the message was actually sent to the aux admin chat
                await self.main_by_aux_admin_chat_message_id_store.save(aux_admin_message_id, admin_chat_message.id)
                await self.aux_by_main_admin_chat_message_id_store.save(admin_chat_message.id, aux_admin_message_id)
        else:
            # if we got no user message (e.g. the message in admin chat is emulated), we just clone the message
            # from the main admin chat to the aux one
            async def message_forwarder(message_thread_id: Optional[int]) -> MessageForwarderResult:
                copied_message = await bot.copy_message(
                    chat_id=self.feedback_handler.admin_chat_id,
                    from_chat_id=admin_chat_message.chat.id,
                    message_id=admin_chat_message.id,
                    message_thread_id=message_thread_id,
                )
                await self.main_by_aux_admin_chat_message_id_store.save(
                    copied_message.message_id, admin_chat_message.id
                )
                await self.aux_by_main_admin_chat_message_id_store.save(
                    admin_chat_message.id, copied_message.message_id
                )
                fake_admin_chat_message = copy.deepcopy(admin_chat_message)
                fake_admin_chat_message.chat = await self.feedback_handler.admin_chat()
                fake_admin_chat_message.id = copied_message.message_id
                return MessageForwarderResult(admin_chat_msg=fake_admin_chat_message, user_msg=None)

            async def noop(*args, **kwargs) -> None:
                pass

            await self.feedback_handler._handle_user_message(
                bot=bot,
                user=user,
                message_forwarder=message_forwarder,
                send_user_identifier=self.feedback_handler.config.user_anonymization is not UserAnonymization.LEGACY,
                user_replier=noop,
                export_to_integrations=True,
            )

    async def handle_user_message_replied_elsewhere(self, event: UserMessageRepliedEvent) -> None:
        aux_admin_chat_message_id = await self.aux_by_main_admin_chat_message_id_store.load(
            event.main_admin_chat_message_id
        )
        if aux_admin_chat_message_id is None:
            self.logger.info("Message in the main admin chat has no saved aux admin chat message id, ignoring")
            return
        # from aux feedback handler's POV, aux admin chat msg id is main
        event.main_admin_chat_message_id = aux_admin_chat_message_id

        if not isinstance(event, UserMessageRepliedFromIntegrationEvent):
            event = UserMessageRepliedFromIntegrationEvent(
                bot=event.bot,
                origin_chat_id=event.origin_chat_id,
                reply_text=event.reply_text,
                reply_has_attachments=event.reply_has_attachments,
                reply_author=event.reply_author,
                reply_link=event.reply_link,
                main_admin_chat_message_id=event.main_admin_chat_message_id,
                # this is fake _MainFeedbackHandlerIntegration inserted in __init__
                integration=self.feedback_handler.integrations[0],
            )
        await self.feedback_handler.message_replied_from_integration_callback(event, notify_integrations=False)

    async def setup(self, bot: AsyncTeleBot) -> None:
        await self.feedback_handler.setup_without_user_message_handler(bot)
