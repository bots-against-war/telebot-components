import logging
from dataclasses import dataclass, fields
from datetime import timedelta
from itertools import chain
from typing import Callable, Coroutine, Optional, Protocol, TypedDict, cast

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.types import constants as tg_constants
from telebot.types.service import FilterFunc

from telebot_components.constants import times
from telebot_components.feedback.anti_spam import (
    AntiSpam,
    AntiSpamConfig,
    AntiSpamInterface,
    AntiSpamStatus,
)
from telebot_components.feedback.trello_integration import TrelloIntegration
from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.banned_users import BannedUsersStore
from telebot_components.stores.category import CategoryStore
from telebot_components.stores.generic import (
    KeyFlagStore,
    KeyListStore,
    KeySetStore,
    KeyValueStore,
)
from telebot_components.stores.language import (
    AnyText,
    Language,
    LanguageStore,
    any_text_to_str,
)


@dataclass
class ServiceMessages:
    # messages to user (may be localized, if used with LanguageStore), please keep in sync with user_facing property
    # e.g. "Спасибо за сообщение, переслано!"
    forwarded_to_admin_ok: Optional[AnyText] = None
    # e.g. "Пожалуйста, сначала выберите тему сообщения, и затем пришлите его заново"
    you_must_select_category: Optional[AnyText] = None
    # e.g. "⚠️ Пожалуйста, не присылайте больше {} сообщений в течение {}!"
    throttling_template: Optional[AnyText] = None

    # messages in admin chat (not localised!)
    # e.g. "Скопировано в чат с пользовател_ьницей!"
    copied_to_user_ok: Optional[str] = None

    @property
    def user_facing(self) -> list[Optional[AnyText]]:
        return [self.forwarded_to_admin_ok, self.you_must_select_category, self.throttling_template]

    def throttling(self, anti_spam: AntiSpamConfig, language: Optional[Language]) -> str:
        if self.throttling_template is None:
            raise RuntimeError("throttling_template is not set, please use validate_config method on startup")
        return any_text_to_str(self.throttling_template, language).format(
            anti_spam.throttle_after_messages, anti_spam.throttle_duration
        )


@dataclass
class HashtagMessageData(TypedDict):
    """Can't just store message ids as ints because we need to update hashatag messages sometimes!"""

    message_id: int
    hashtags: list[str]  # NOTE: '#' prefixes are not stored here, only hashtag body


class AdminChatActionCallback(Protocol):
    async def __call__(self, admin_message: tg.Message, forwarded_message: tg.Message, origin_chat_id: int) -> None:
        ...


@dataclass
class AdminChatAction:
    command: str
    callback: AdminChatActionCallback
    delete_everything_related_to_user_after: bool = False


@dataclass
class FeedbackConfig:
    # if False, message log is sent to PM with the admin that has invoked the '/log' cmd
    message_log_to_admin_chat: bool
    # if True, user's messages are not forwarded until they select a category
    force_category_selection: bool
    # if True, hashtag messages is sent before the actual forwarded message to the admin chat
    hashtags_in_admin_chat: bool
    # if hashtags_in_admin_chat is True, this specifies how often to send hashtag message
    hashtag_message_rarer_than: Optional[timedelta]
    # e.g. 'new' to mark all unanswered messages with '#new'; specify None to disable unanswered
    unanswered_hashtag: Optional[str]
    # when users sent a lot of messages, they can grow tired of constant confirmations
    # this parameter allows to limit confirmations to user to one per a specified time
    forwarded_to_admin_confirmations_throttle_duration: Optional[timedelta] = None
    # custom filters and hooks
    custom_user_message_filter: Optional[Callable[[tg.Message], Coroutine[None, None, bool]]] = None
    before_forwarding: Optional[Callable[[tg.User], Coroutine[None, None, Optional[tg.Message]]]] = None
    after_forwarding: Optional[Callable[[tg.User], Coroutine[None, None, Optional[tg.Message]]]] = None
    # appended to admin chat help under "Other" section; Supports HTML markup
    admin_chat_help_extra: Optional[str] = None


class FeedbackHandler:
    """
    A class incapsulating the following workflow:
     - people write messages to a bot
     - bot forwards them to admin chat
     - admins reply to messages
     - bot copy messages back to the user
    """

    def __init__(
        self,
        admin_chat_id: int,
        redis: RedisInterface,
        bot_prefix: str,
        config: FeedbackConfig,
        anti_spam: AntiSpamInterface,
        service_messages: ServiceMessages,
        banned_users_store: Optional[BannedUsersStore] = None,
        language_store: Optional[LanguageStore] = None,
        category_store: Optional[CategoryStore] = None,
        trello_integration: Optional[TrelloIntegration] = None,
        admin_chat_response_actions: Optional[list[AdminChatAction]] = None,
    ):
        self.bot_prefix = bot_prefix
        self.admin_chat_id = admin_chat_id
        self.config = config

        self.anti_spam = anti_spam
        self.banned_users_store = banned_users_store
        self.language_store = language_store
        self.category_store = category_store
        self.trello_integration = trello_integration

        self.service_messages = service_messages
        self.validate_service_messages()

        if admin_chat_response_actions is None:
            self.admin_chat_response_actions = []
        else:
            self.admin_chat_response_actions = admin_chat_response_actions

        if self.banned_users_store is not None:
            self.admin_chat_response_actions.append(
                AdminChatAction(
                    command="/ban",
                    callback=self._ban_admin_chat_action,
                    delete_everything_related_to_user_after=True,
                )
            )
        self.admin_chat_response_action_by_command = {aca.command: aca for aca in self.admin_chat_response_actions}
        self.logger = logging.getLogger(f"{__name__}.{self.bot_prefix}")

        # === stores used by the handler ===

        # forwarded message in admin chat -> origin chat id (user id)
        self.origin_chat_id_store = KeyValueStore[int](
            name="origin-chat-for-msg",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=times.YEAR,
        )
        # origin chat id -> set of message ids in admin chat related to user
        # NOTE: stores not only forwarded message ids but also service messages
        # associated with this user (hashtags, custom pre- and post-forwarding messages)
        self.user_related_messages_store = KeySetStore[int](
            name="msgs-from-user",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=times.MONTH,
        )
        # origin chat id -> list of messages from or to the user
        self.message_log_store = KeyListStore[int](
            name="message-log-with",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=times.MONTH,
        )
        # [optional] user id -> flag if the "forwarded to admin" confirmation has recently been
        # sent to them
        self.recently_sent_confirmation_flag_store = (
            KeyFlagStore(
                name="recently-sent-confirmation-to",
                prefix=bot_prefix,
                redis=redis,
                expiration_time=self.config.forwarded_to_admin_confirmations_throttle_duration,
            )
            if self.config.forwarded_to_admin_confirmations_throttle_duration is not None
            else None
        )
        # [optional] hashtag-related stores
        # user id -> hashtag message data; used to avoid sending hashtags too frequently
        self.recent_hashtag_message_for_user_store = (
            KeyValueStore[HashtagMessageData](
                name="recent-hashtag-message-for",
                prefix=bot_prefix,
                redis=redis,
                expiration_time=self.config.hashtag_message_rarer_than,
            )
            if self.config.hashtags_in_admin_chat
            else None
        )
        # forwarded msg id -> hashtag message id; used to update hashtag message on forwarded msg action
        self.hashtag_message_for_forwarded_message_store = (
            KeyValueStore[HashtagMessageData](
                name="hashtag-msg-for-fwd",
                prefix=bot_prefix,
                redis=redis,
                expiration_time=times.MONTH,
            )
            if self.config.hashtags_in_admin_chat
            else None
        )

    def validate_service_messages(self):
        if self.config.force_category_selection and self.service_messages.you_must_select_category is None:
            raise ValueError("force_category_selection is True, you_must_select_category message must be set")

        if self.language_store is not None:
            languages = self.language_store.languages
        else:
            languages = [None]
        for message in self.service_messages.user_facing:
            if message is None:
                continue
            for language in languages:
                any_text_to_str(message, language)

    async def _ban_admin_chat_action(
        self, admin_message: tg.Message, forwarded_message: tg.Message, origin_chat_id: int
    ):
        if self.banned_users_store is not None:
            await self.banned_users_store.ban_user(origin_chat_id)

    async def save_message_from_user(self, origin_message: tg.Message, forwarded_message: tg.Message):
        origin_chat_id = origin_message.chat.id
        await self.origin_chat_id_store.save(forwarded_message.id, origin_chat_id)
        await self.user_related_messages_store.add(origin_chat_id, forwarded_message.id, reset_ttl=True)
        await self.message_log_store.push(origin_chat_id, forwarded_message.id, reset_ttl=True)

    def _admin_help_message(self) -> str:
        help_msg = (
            "<b>Справка-памятка для админского чата</b>\n\n"
            + "💬 <i>Основное</i>\n"
            + "· Сюда бот пересылает все сообщения (кроме специальных случаев вроде /команд), которые ему "
            + "пишут в личку.\n"
            + "· Если в этом чате ответить на сообщение, бот скопирует ответ в чат с пользовател_ьницей.\n"
        )
        if self.category_store is not None:
            help_msg += (
                "📊 <i>Категории сообщений</i>\n"
                + "· Каждо_й пользовател_ьнице предлагается выбрать одну из категорий: "
                + ", ".join(
                    [f"<b>{c.name}</b> (# {c.hashtag})" for c in self.category_store.categories if not c.hidden]
                )
                + "\n"
            )
            if self.config.force_category_selection:
                help_msg += "· Выбор категории обязателен для пользовател_ьниц.\n"
            else:
                help_msg += "· Выбор категории необязателен, боту можно написать и без него.\n"

        help_msg += (
            "🛡️ <i>Защита и безопасность</i>\n"
            + "· Бот никак не выдаёт, кто отвечает пользовател_ьнице из этого чата. Насколько возможно судить, "
            + "никакого способа взломать бота нет. Однако всё, что вы отвечаете через бота, сразу пересылается человеку "
            + "на другом конце, и на данный момент удалить отправленное сообщение нельзя, поэтому будьте внимательны!\n"
        )
        if isinstance(self.anti_spam, AntiSpam):
            help_msg += (
                "· Бот автоматически ограничивает число сообщений, прислыаемых ему в единицу времени. "
                + f"Конфигруация на данный момент: не больше {self.anti_spam.config.throttle_after_messages} сообщений за "
                + f"{self.anti_spam.config.throttle_duration}; при необходимости её можно изменять.\n"
            )
        if self.banned_users_store is not None:
            help_msg += (
                "· Если ответить на пересланное сообщение командой /ban, "
                + "пользовател_ьница будет заблокирован_а, а все сообщения от них в чате — удалены\n\n"
            )
        help_msg += (
            "📋 <i>История сообщений</i>\n"
            + "· Через бота может быть неудобно вести несколько длительных переписок — все они мешаются в одном чате."
            + "· Если ответить на пересланное сообщение командой /log, бот перешлёт всю историю переписки с "
            + "пользовател_ьницей "
            + (
                "в этот чат. Можно настроить бота так, чтобы бот пересылал историю не сюда, а "
                + "в диалог с администратор_кой, которая её запросила."
                if self.config.message_log_to_admin_chat
                else "вам в личку (для этого вы должны хотя бы раз что-то ему написать). Можно настроить бота так, "
                + "чтобы чтобы бот пересылал историю сообщений не в личку, а прямо в этот чат."
            )
        )
        if self.trello_integration is not None:
            help_msg += "\n\n🗂️ <i>Интеграция с Trello</i>\n"
            help_msg += (
                f"· Помимо чата сообщения выгружаются на <a href={self.trello_integration.board.url}>доску</a> Trello"
            )
            if self.trello_integration.categories is not None:
                help_msg += "в несколько списков по категориям (хештегам): "
                help_msg += ", ".join(f"<b>{l.name}</b>" for l in self.trello_integration.lists_by_category_id.values())
            else:
                help_msg += f"в список <b>{self.trello_integration.bot_prefix}</b>"
            help_msg += (
                ". "
                + "В карточки переносятся присланные в бот фотографии и документы; для каждого сообщения доступна "
                + "обратная ссылка на этот чат.\n"
                # + "· В тестовом режиме работает ответ через Trello: если прокоментировать карточку сообщением в "
                # + "формате «/reply текст ответа», бот отправит «текст ответа» в чат с пользовател_ьницей, а также "
                # + "напишет уведомление сюда."
            )

        if self.config.admin_chat_help_extra:
            help_msg += "\n\n🪄 <i>Другое</i>\n"
            help_msg += self.config.admin_chat_help_extra

        # TODO: add bot support hotline link :)

        return help_msg

    async def _user_message_filter(self, message: tg.Message) -> bool:
        if self.config.custom_user_message_filter is not None:
            return await self.config.custom_user_message_filter(message)
        else:
            return True

    def _admin_chat_message_filter(self, message: tg.Message) -> bool:
        return message.chat.id == self.admin_chat_id and message.reply_to_message is not None

    def setup(self, bot: AsyncTeleBot):
        @bot.message_handler(
            func=cast(FilterFunc, self._user_message_filter),
            chat_types=[tg_constants.ChatType.private],
            content_types=list(tg_constants.MediaContentType),
            priority=-100,  # lower priority to process the rest of the handlers first
        )
        async def user_to_bot(message: tg.Message):
            user = message.from_user
            if self.banned_users_store is not None and await self.banned_users_store.is_banned(user.id):
                return
            anti_spam_status = await self.anti_spam.status(user)
            if anti_spam_status is AntiSpamStatus.SOFT_BAN:
                return

            if self.language_store is not None:
                language = await self.language_store.get_user_language(user)
            else:
                language = None
            if anti_spam_status is AntiSpamStatus.THROTTLING:
                anti_spam = cast(AntiSpam, self.anti_spam)  # only real AntiSpam can return this status
                await bot.reply_to(message, self.service_messages.throttling(anti_spam.config, language))
                return

            if self.config.hashtags_in_admin_chat:
                category_hashtag = None  # sentinel
                if self.category_store is not None:
                    category = await self.category_store.get_user_category(user)
                    if category is None:
                        if self.config.force_category_selection:
                            # see validate_service_messages
                            you_must_select_category = cast(AnyText, self.service_messages.you_must_select_category)
                            await bot.reply_to(
                                message,
                                any_text_to_str(you_must_select_category, language),
                                reply_markup=(await self.category_store.markup_for_user(user)),
                            )
                            return
                    else:
                        category_hashtag = category.hashtag

                # see runtime check for the hashtags_in_admin_chat flag and creation of the store
                hashtag_msg_data = await self.recent_hashtag_message_for_user_store.load(user.id)  # type: ignore
                if hashtag_msg_data is None or (
                    category_hashtag is not None and category_hashtag not in hashtag_msg_data["hashtags"]
                ):
                    # sending a new hashtag message
                    if self.config.unanswered_hashtag is not None:
                        hashtags = [self.config.unanswered_hashtag]
                    else:
                        hashtags = []
                    if category_hashtag is not None:
                        hashtags.append(category_hashtag)
                    # TODO: multiple categories per user support
                    hashtag_msg = await bot.send_message(self.admin_chat_id, _join_hashtags(hashtags))
                    await self.user_related_messages_store.add(user.id, hashtag_msg.id, reset_ttl=False)
                    hashtag_msg_data = HashtagMessageData(message_id=hashtag_msg.id, hashtags=hashtags)
                    await self.recent_hashtag_message_for_user_store.save(user.id, hashtag_msg_data)  # type: ignore

            preforwarded_msg = None
            if self.config.before_forwarding is not None:
                preforwarded_msg = await self.config.before_forwarding(user)
                if isinstance(preforwarded_msg, tg.Message):
                    await self.save_message_from_user(message, preforwarded_msg)
            forwarded_msg = await bot.forward_message(
                chat_id=self.admin_chat_id, from_chat_id=message.chat.id, message_id=message.id
            )
            await self.save_message_from_user(message, forwarded_msg)
            postforwarded_msg = None
            if self.config.after_forwarding is not None:
                postforwarded_msg = await self.config.after_forwarding(user)
                if isinstance(postforwarded_msg, tg.Message):
                    await self.save_message_from_user(message, postforwarded_msg)

            if self.config.hashtags_in_admin_chat:
                await self.hashtag_message_for_forwarded_message_store.save(forwarded_msg.id, hashtag_msg_data)  # type: ignore

            if self.service_messages.forwarded_to_admin_ok is not None:
                if self.recently_sent_confirmation_flag_store is not None:
                    confirmation_recently_sent = await self.recently_sent_confirmation_flag_store.is_flag_set(user.id)
                else:
                    confirmation_recently_sent = False
                if not confirmation_recently_sent:
                    await bot.reply_to(message, any_text_to_str(self.service_messages.forwarded_to_admin_ok, language))

            if self.trello_integration is not None:
                category = await self.category_store.get_user_category(user) if self.category_store else None
                # HACK: we're pretending pre- and postforwarded messages are actually part of user's message, that's not good
                card_text = message.text_content
                if preforwarded_msg is not None:
                    card_text = "[pre]: " + preforwarded_msg.text_content + "\n\n" + card_text
                if postforwarded_msg is not None:
                    card_text = card_text + "\n\n" + "[post]:" + postforwarded_msg.text_content
                await self.trello_integration.export_message(message, forwarded_msg, category)

        @bot.message_handler(chat_id=[self.admin_chat_id], commands=["help"])
        async def admin_chat_help(message: tg.Message):
            await bot.reply_to(
                message,
                self._admin_help_message(),
                disable_web_page_preview=True,
                disable_notification=True,
                parse_mode="HTML",
            )

        async def _remove_unanswered_hashtag(message_id: int):
            if self.hashtag_message_for_forwarded_message_store is None:
                return
            hashtag_message_data = await self.hashtag_message_for_forwarded_message_store.load(message_id)
            if hashtag_message_data is None:
                return
            if self.config.unanswered_hashtag is None:
                return
            if self.config.unanswered_hashtag not in hashtag_message_data["hashtags"]:
                return
            hashtag_message_data["hashtags"].remove(self.config.unanswered_hashtag)
            try:
                if hashtag_message_data["hashtags"]:
                    await bot.edit_message_text(
                        message_id=hashtag_message_data["message_id"],
                        chat_id=self.admin_chat_id,
                        text=_join_hashtags(hashtag_message_data["hashtags"]),
                    )
                else:
                    await bot.delete_message(chat_id=self.admin_chat_id, message_id=hashtag_message_data["message_id"])
            except Exception as e:
                # when replying on a message in a group that has already been responded to,
                # telegram API returns and error if there's nothing to change
                self.logger.info(f"Error updating hashtag message: {e}")
                pass
            await self.hashtag_message_for_forwarded_message_store.save(message_id, hashtag_message_data)

        @bot.message_handler(
            chat_id=[self.admin_chat_id],
            is_reply=True,
            content_types=list(tg_constants.MediaContentType),
        )
        async def admin_to_bot(message: tg.Message):
            try:
                forwarded_msg = message.reply_to_message
                if forwarded_msg is None:
                    return
                origin_chat_id = await self.origin_chat_id_store.load(forwarded_msg.id)
                if origin_chat_id is None:
                    return

                if message.text is not None and message.text.startswith("/"):
                    # admin chat commands
                    if message.text in self.admin_chat_response_action_by_command:
                        admin_chat_action = self.admin_chat_response_action_by_command[message.text]
                        await admin_chat_action.callback(message, forwarded_msg, origin_chat_id)
                        if admin_chat_action.delete_everything_related_to_user_after:
                            user_related_message_ids = await self.user_related_messages_store.all(origin_chat_id)
                            user_related_message_ids.add(message.id)
                            for message_id in user_related_message_ids:
                                try:
                                    await bot.delete_message(self.admin_chat_id, message_id)
                                    await self.origin_chat_id_store.drop(message_id)
                                except Exception:
                                    pass
                            await self.user_related_messages_store.drop(origin_chat_id)
                            await self.message_log_store.drop(origin_chat_id)
                    elif message.text == "/log":
                        log_message_ids = await self.message_log_store.all(origin_chat_id)
                        if not log_message_ids:
                            await bot.reply_to(message, "Message log with this user is not available :(")
                            return
                        log_destination_chat_id = (
                            self.admin_chat_id if self.config.message_log_to_admin_chat else message.from_user.id
                        )
                        for message_id in log_message_ids:
                            try:
                                log_mesage = await bot.forward_message(
                                    chat_id=log_destination_chat_id,
                                    from_chat_id=self.admin_chat_id,
                                    message_id=message_id,
                                )
                                if self.config.message_log_to_admin_chat:
                                    # to be able to reply to them as to normal forwarded messages...
                                    await self.origin_chat_id_store.save(log_mesage.id, origin_chat_id)
                                    # ... and to delete them in case of user ban
                                    await self.user_related_messages_store.add(origin_chat_id, log_mesage.id)
                            except Exception:
                                pass
                    else:
                        available_commands = list(self.admin_chat_response_action_by_command.keys()) + ["/log"]
                        await bot.reply_to(
                            message,
                            f"Invalid admin chat command: {message.text}; available commands are: "
                            + ", ".join(available_commands),
                        )
                else:
                    # actual response to user
                    await bot.copy_message(
                        chat_id=origin_chat_id, from_chat_id=self.admin_chat_id, message_id=message.id
                    )
                    # TODO: save copied message id to allow 'undo send' command
                    await self.message_log_store.push(origin_chat_id, message.id)
                    if self.service_messages.copied_to_user_ok is not None:
                        await bot.reply_to(message, self.service_messages.copied_to_user_ok)
                    if self.config.hashtags_in_admin_chat:
                        await _remove_unanswered_hashtag(forwarded_msg.id)
            except Exception as e:
                await bot.reply_to(message, f"Something went wrong: {e}")
                self.logger.exception(f"Unexpected error while replying to forwarded msg")


def _join_hashtags(hashtags: list[str]) -> str:
    return " ".join(["#" + h for h in hashtags])
