import asyncio
import copy
import dataclasses
import enum
import logging
import math
import random
import warnings
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import timedelta
from typing import (
    Any,
    Callable,
    Coroutine,
    Optional,
    Protocol,
    TypeAlias,
    TypedDict,
    TypeVar,
    cast,
)

from telebot import AsyncTeleBot
from telebot import api as tgapi
from telebot import types as tg
from telebot.api import ApiHTTPException
from telebot.formatting import hbold
from telebot.runner import AuxBotEndpoint
from telebot.types import constants as tg_constants
from telebot.types.service import FilterFunc, HandlerResult
from telebot.util import extract_arguments

from telebot_components.constants import times
from telebot_components.feedback.anti_spam import (
    AntiSpam,
    AntiSpamConfig,
    AntiSpamInterface,
    AntiSpamStatus,
)
from telebot_components.feedback.integration.interface import (
    FeedbackHandlerIntegration,
    FeedbackIntegrationBackgroundContext,
    UserMessageRepliedFromIntegrationEvent,
)
from telebot_components.feedback.integration.trello import TrelloIntegration
from telebot_components.feedback.types import UserMessageRepliedEvent
from telebot_components.form.field import TelegramAttachment
from telebot_components.language import MaybeLanguage
from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.banned_users import BannedUsersStore
from telebot_components.stores.category import CategoryStore
from telebot_components.stores.forum_topics import CategoryForumTopicStore
from telebot_components.stores.generic import (
    KeyFlagStore,
    KeyListStore,
    KeySetStore,
    KeyValueStore,
)
from telebot_components.stores.language import (
    AnyLanguage,
    AnyText,
    LanguageStore,
    any_text_to_str,
    vaildate_singlelang_text,
)
from telebot_components.utils import (
    TelegramReactionEmoji,
    emoji_hash,
    html_link,
    send_attachment,
    telegram_html_escape,
    telegram_message_url,
)

T = TypeVar("T")


@dataclass
class ServiceMessages:
    # messages to user (may be localized, if used with LanguageStore), please keep in sync with user_facing property
    # e.g. "Спасибо за сообщение, переслано!"
    forwarded_to_admin_ok: Optional[AnyText] = None
    # e.g. "Пожалуйста, сначала выберите тему сообщения, и затем пришлите его заново"
    you_must_select_category: Optional[AnyText] = None
    # e.g. "⚠️ Пожалуйста, не присылайте больше {} сообщений в течение {}!"
    throttling_template: Optional[AnyText] = None
    # when failed to forward user's message
    something_went_wrong: Optional[AnyText] = None
    # bot puts the reaction on user messages it forwarded
    forwarded_to_admin_reaction: Optional[TelegramReactionEmoji] = None

    # messages in admin chat (not localised!)
    # e.g. "Скопировано в чат с пользователь_ницей!"
    copied_to_user_ok: Optional[str] = None
    # e.g. "Невозможно удалить сообщение."
    can_not_delete_message: Optional[str] = None
    # e.g. "Сообщение успешно удалено!"
    deleted_message_ok: Optional[str] = None

    @property
    def user_facing(self) -> list[Optional[AnyText]]:
        return [
            self.forwarded_to_admin_ok,
            self.you_must_select_category,
            self.throttling_template,
            self.something_went_wrong,
        ]

    def throttling(self, anti_spam: AntiSpamConfig, language: Optional[AnyLanguage]) -> str:
        if self.throttling_template is None:
            raise RuntimeError("throttling_template is not set, please use validate_config method on startup")
        return any_text_to_str(self.throttling_template, language).format(
            anti_spam.throttle_after_messages, anti_spam.throttle_duration
        )


class HashtagMessageData(TypedDict):
    """Can't just store message ids as ints because we need to update hashatag messages sometimes!"""

    message_id: int
    hashtags: list[str]  # NOTE: '#' prefixes are not stored here, only hashtag body


class CopiedMessageToUserData(TypedDict):
    origin_chat_id: int
    sent_message_id: int


class AdminChatActionCallback(Protocol):
    async def __call__(self, admin_message: tg.Message, forwarded_message: tg.Message, origin_chat_id: int) -> None: ...


@dataclass
class AdminChatAction:
    command: str
    callback: AdminChatActionCallback
    delete_everything_related_to_user_after: bool = False


class UserAnonymization(enum.Enum):
    # name, username and user id are shown to admins
    NONE = enum.auto()

    # legacy option
    # nothing is shown intentionally, but message forwarding mechanism is used,
    # which gives a link to user profile unless they opted in to anonymize it
    LEGACY = enum.auto()

    # admins only see anonymized identifier for the user
    FULL = enum.auto()


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

    # when users send a lot of messages, they can grow tired of constant confirmations
    # this parameter allows to limit confirmations to user to one per a specified time
    confirm_forwarded_to_admin_rarer_than: Optional[timedelta] = None

    # custom filters and hooks
    custom_user_message_filter: Optional[Callable[[tg.Message], Coroutine[None, None, bool]]] = None
    before_forwarding: Optional[Callable[[tg.User], Coroutine[None, None, Optional[tg.Message]]]] = None
    after_forwarding: Optional[Callable[[tg.User], Coroutine[None, None, Optional[tg.Message]]]] = None

    # appended to admin chat help under "Other" section; Supports HTML markup
    admin_chat_help_extra: Optional[str] = None

    # LEGACY OPTION
    # if True, user messages are not forwarded but copied to admin chat without any back
    # link to the user account; before the message, user id hash is sent for identification
    full_user_anonymization: bool = False

    user_anonymization: UserAnonymization = UserAnonymization.LEGACY

    # (user id, bot prefix) -> unique string identifying the user
    # used to generate user id hash for a particular bot;
    user_id_hash_func: Callable[[int, str], str] = emoji_hash

    # how many messages to forward in one go on /log command
    message_log_page_size: int = 30

    # create new forum topic per new user
    forum_topic_per_user: bool = False

    # if forum topic per user is set, admins can reply to users by just writing to their topic,
    # no need to use Telegram message reply
    any_message_in_user_topic_is_reply: bool = True

    user_forum_topic_lifetime: timedelta = timedelta(days=90)

    # default values for data retention are multiplied by this factor
    data_lifetime_multiplier: float = 1.0

    def __post_init__(self):
        if self.full_user_anonymization:
            warnings.warn(
                "full_user_anonymization argument is deprecated, use user_anonymization=UserAnonymization.FULL"
            )
            self.user_anonymization = UserAnonymization.FULL


@dataclasses.dataclass
class MessageForwarderResult:
    admin_chat_msg: tg.Message
    user_msg: Optional[tg.Message]


DUMMY_EXPIRATION_TIME = timedelta(seconds=1312)  # for stores unused based on runtime settings


# service type for a callback function passed around for various scenarios of message copying
UserReplier: TypeAlias = Callable[[str | None, tg.ReplyMarkup | None, tg.ReactionType | None], Awaitable[Any]]


class FeedbackHandler:
    """
    A class incapsulating the following workflow:
     - people write messages to the bot
     - the bot forwards/copies messages to admin chat
     - admins reply
     - the bot copies messages back to the user
    """

    CONST_KEY = "const"

    def __init__(
        self,
        *,
        admin_chat_id: int,
        redis: RedisInterface,
        bot_prefix: str,
        config: FeedbackConfig,
        anti_spam: AntiSpamInterface,
        service_messages: ServiceMessages,
        banned_users_store: Optional[BannedUsersStore] = None,
        language_store: Optional[LanguageStore] = None,
        category_store: Optional[CategoryStore] = None,
        forum_topic_store: Optional[CategoryForumTopicStore] = None,
        trello_integration: Optional[TrelloIntegration] = None,
        integrations: Optional[list[FeedbackHandlerIntegration]] = None,
        admin_chat_response_actions: Optional[list[AdminChatAction]] = None,
        # specific feedback handler name in case there are several of them;
        # the default (empty string) makes it backwards compatible
        name: str = "",
    ) -> None:
        self.name = name
        bot_prefix = bot_prefix + name  # hacky solution to allow several feedback handlers per bot
        self.bot_prefix = bot_prefix
        self.logger = logging.getLogger(f"{__name__}[{self.bot_prefix}]")

        self.admin_chat_id = admin_chat_id
        self.config = config

        self._admin_chat: Optional[tg.Chat] = None
        self._bot: Optional[AsyncTeleBot] = None

        self.anti_spam = anti_spam
        self.banned_users_store = banned_users_store
        self.language_store = language_store
        self.category_store = category_store
        self.forum_topic_store = forum_topic_store
        if self.config.forum_topic_per_user and self.forum_topic_store is not None:
            raise ValueError(
                "Forum topics can be used either for categories OR created per-user, not both at the same time"
            )

        integrations_ = integrations or []
        if trello_integration is not None:
            self.logger.warning("'trello_integration' argument is deprecated, please use 'integrations' instead")
            integrations_.append(trello_integration)
        self.integrations = integrations_

        self.service_messages = service_messages
        self.validate_service_messages()

        if admin_chat_response_actions is None:
            self.admin_chat_response_actions = []
        else:
            self.admin_chat_response_actions = admin_chat_response_actions

        self.admin_chat_response_action_by_command = {aca.command: aca for aca in self.admin_chat_response_actions}

        # === stores used by the handler ===

        # forwarded message in admin chat -> origin chat id (user id)
        self.origin_chat_id_store = KeyValueStore[int](
            name="origin-chat-for-msg",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=times.YEAR * config.data_lifetime_multiplier,
        )
        # origin chat id -> set of message ids in admin chat related to user
        # NOTE: stores not only forwarded message ids but also service messages
        # associated with this user (hashtags, custom pre- and post-forwarding messages)
        self.user_related_messages_store = KeySetStore[int](
            name="msgs-from-user",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=times.MONTH * config.data_lifetime_multiplier,
        )
        # origin chat id -> list of messages from or to the user
        self.message_log_store = KeyListStore[int](
            name="message-log-with",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=times.MONTH * config.data_lifetime_multiplier,
        )
        # [optional] user id -> flag if the "forwarded to admin" confirmation has recently been
        # sent to them
        self.recently_sent_confirmation_flag_store = KeyFlagStore(
            name="recently-sent-confirmation-to",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=self.config.confirm_forwarded_to_admin_rarer_than or DUMMY_EXPIRATION_TIME,
        )
        # [optional] hashtag-related stores
        # user id -> hashtag message data; used to avoid sending hashtags too frequently
        self.recent_hashtag_message_for_user_store = KeyValueStore[HashtagMessageData](
            name="recent-hashtag-message-for",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=self.config.hashtag_message_rarer_than or DUMMY_EXPIRATION_TIME,
        )
        # forwarded msg id -> hashtag message id; used to update hashtag message on forwarded msg action
        self.hashtag_message_for_forwarded_message_store = KeyValueStore[HashtagMessageData](
            name="hashtag-msg-for-fwd",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=times.MONTH,
        )

        # copied to user ok msg id/admin response msg -> origin chat id (user id) + sent message id;
        # used to undo sent message if needed
        self.copied_to_user_data_store = KeyValueStore[CopiedMessageToUserData](
            name="copied-to-user-ok",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=times.FIVE_MINUTES,
        )

        # const key -> last sent user id hash to avoid repeating it on multiple consequtive messages
        self.last_sent_user_identifier_store = KeyValueStore[str](
            name="last-sent-user-id-hash",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=timedelta(hours=12),
            loader=str,
            dumper=str,
        )

        # if forum_topic_per_user option is used
        self.message_thread_id_by_user_id_store = KeyValueStore[int](
            name="message-thread-id-by-user",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=self.config.user_forum_topic_lifetime,
            loader=int,
            dumper=str,
        )
        self.last_forwarded_message_id_by_message_thread_id = KeyValueStore[int](
            name="last-fwd-msg-id-in-thread",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=self.config.user_forum_topic_lifetime,
            loader=int,
            dumper=str,
        )

    @property
    def bot(self) -> AsyncTeleBot:
        if self._bot is None:
            raise RuntimeError("Bot was not initialized")
        return self._bot

    async def admin_chat(self) -> tg.Chat:
        if self._admin_chat is None:
            self._admin_chat = await self.bot.get_chat(self.admin_chat_id)
        return self._admin_chat

    def validate_service_messages(self):
        if self.config.force_category_selection and self.service_messages.you_must_select_category is None:
            raise ValueError("force_category_selection is True, you_must_select_category message must be set")

        for message in self.service_messages.user_facing:
            if message is None:
                continue
            if self.language_store is None:
                vaildate_singlelang_text(message)
            else:
                self.language_store.validate_multilang(message)

    async def _delete_user_related_messages(
        self, bot: AsyncTeleBot, origin_chat_id: int, initiator_message_id: int
    ) -> None:
        if self.config.forum_topic_per_user:
            user_topic_message_thread_id = await self.message_thread_id_by_user_id_store.load(origin_chat_id)
            if user_topic_message_thread_id is not None:
                self.logger.info("Found forum topic for user, deleting it")
                await bot.delete_forum_topic(self.admin_chat_id, message_thread_id=user_topic_message_thread_id)
                await self.message_thread_id_by_user_id_store.drop(origin_chat_id)
                self.logger.info("User forum topic deleted")
                return

        user_related_message_ids = await self.user_related_messages_store.all(origin_chat_id)
        user_related_message_ids.add(initiator_message_id)
        for message_id in user_related_message_ids:
            try:
                await bot.delete_message(self.admin_chat_id, message_id)
                await self.origin_chat_id_store.drop(message_id)
                await asyncio.sleep(0.5)
            except Exception:
                pass
        await self.user_related_messages_store.drop(origin_chat_id)
        await self.message_log_store.drop(origin_chat_id)

    async def _ban_admin_chat_action(
        self, admin_message: tg.Message, forwarded_message: tg.Message, origin_chat_id: int
    ):
        if self.banned_users_store is not None:
            await self.banned_users_store.ban_user(origin_chat_id)

    async def save_message_from_user(
        self, author: tg.User, forwarded_message_id: int, message_thread_id: Optional[int]
    ):
        origin_chat_id = author.id
        await self.origin_chat_id_store.save(forwarded_message_id, origin_chat_id)
        await self.user_related_messages_store.add(origin_chat_id, forwarded_message_id, reset_ttl=True)
        await self.message_log_store.push(origin_chat_id, forwarded_message_id, reset_ttl=True)
        if message_thread_id is not None:
            await self.last_forwarded_message_id_by_message_thread_id.save(message_thread_id, forwarded_message_id)

    def _admin_help_message(self) -> str:
        paragraphs = [
            "<b>Справка-памятка для админского чата</b>",
            "<i>Сообщение сгенерировано автоматически по команде /help</i>",
        ]
        copies_or_forwards = "пересылает" if self.config.user_anonymization is UserAnonymization.LEGACY else "копирует"
        paragraphs.append(
            "💬 <i>Основное</i>\n"
            + f"· В этот чат бот {copies_or_forwards} все сообщения (кроме специальных случаев вроде /команд), "
            + "которые ему пишут в личку.\n"
            + (
                (
                    "· Перед скопированным сообщением бот указывает анонимизированный идентификатор пользователь_ницы, "
                    + f"например такой: «{self.config.user_id_hash_func(random.randint(1, 1000), self.bot_prefix)}»\n"
                )
                if self.config.user_anonymization is UserAnonymization.FULL
                else (
                    "· Перед скопированным сообщением бот указывает имя и юзернейм пользователь_ницы"
                    if self.config.user_anonymization is UserAnonymization.NONE
                    else ""
                )
            )
            + "· Если в этом чате ответить на сообщение, бот скопирует ответ в чат с пользователь_ницей.\n"
            + "· Чтобы отменить отправку сообщения пользователь_нице - отправьте реплай с командой /undo на ваше "
            + "сообщение или на подтверждение отправки бота (доступно в течение 5 минут)"
        )
        if self.category_store is not None:
            categories_help = (
                "📊 <i>Категории сообщений</i>\n"
                + "· Каждо_й пользователь_нице предлагается выбрать одну из категорий: "
                + ", ".join(
                    [f"<b>{c.name}</b> (# {c.hashtag})" for c in self.category_store.categories if not c.hidden]
                )
                + "\n"
            )
            if self.config.force_category_selection:
                categories_help += "· Выбор категории обязателен для пользователь_ниц."
            else:
                categories_help += "· Выбор категории необязателен, боту можно написать и без него."

            paragraphs.append(categories_help)

        security_help = (
            "🛡️ <i>Защита и безопасность</i>\n"
            + "· Бот никак не выдаёт, кто отвечает пользователь_нице из этого чата. Насколько возможно судить, "
            + "никакого способа взломать бота нет. Однако всё, что вы отвечаете через бота, "
            + "сразу пересылается человеку на другом конце, и отменить отправку можно лишь в течении первых "
            + "5 минут, поэтому будьте внимательны!"
        )
        if isinstance(self.anti_spam, AntiSpam):
            security_help += (
                "\n"
                + "· Бот автоматически ограничивает число сообщений, присылаемых ему в единицу времени. "
                + f"Конфигурация на данный момент: не больше {self.anti_spam.config.throttle_after_messages} "
                + f"сообщений за {self.anti_spam.config.throttle_duration}. При необходимости её можно изменять."
            )
        if self.banned_users_store is not None:
            security_help += (
                "\n· Если ответить на пересланное сообщение командой /ban, "
                + "пользователь_ница будет заблокирован_а, а все сообщения от них в чате — удалены"
            )
        paragraphs.append(security_help)

        paragraphs.append(
            "📋 <i>История сообщений</i>\n"
            + "· Через бота может быть неудобно вести несколько длительных переписок — все они мешаются в одном чате.\n"
            + "· Если ответить на пересланное сообщение командой /log, бот перешлёт историю переписки с "
            + "пользователь_ницей "
            + (
                "в этот чат. Можно настроить бота так, чтобы бот пересылал историю не сюда, а "
                + "в диалог с администратор_кой, которая её запросила."
                if self.config.message_log_to_admin_chat
                else "вам в личку (для этого вы должны хотя бы раз что-то ему написать). Можно настроить бота так, "
                + "чтобы чтобы бот пересылал историю сообщений не в личку, а прямо в этот чат."
            )
            + f"\n· По умолчанию бот пересылает первые {self.config.message_log_page_size} сообщений, "
            + "дальше можно листать по страницам: «/log 2», «/log 3», и так далее"
        )

        integration_help_messages = [integration.help_message_section() for integration in self.integrations]
        paragraphs.extend([m for m in integration_help_messages if m])

        if self.config.admin_chat_help_extra:
            paragraphs.append("🪄 <i>Другое</i>\n" + self.config.admin_chat_help_extra)
        return "\n\n".join(paragraphs)

    async def _user_message_filter(self, message: tg.Message) -> bool:
        if self.config.custom_user_message_filter is not None:
            return await self.config.custom_user_message_filter(message)
        else:
            return True

    def user_identifier(self, user: tg.User, support_html: bool) -> str:
        """Human readable identifier for the user (not to be confused with user id)"""
        escape_text = telegram_html_escape if support_html else lambda x: x
        if self.config.user_anonymization is UserAnonymization.FULL:
            return escape_text(self.config.user_id_hash_func(user.id, self.bot_prefix))
        elif self.config.user_anonymization is UserAnonymization.NONE:
            user_identifier = user.full_name
            if user.username:
                user_identifier += " @" + user.username
            user_identifier += f" (#{user.id})"

            if not support_html:
                return user_identifier
            else:
                return html_link(href=f"tg://user?id={user.id}", text=user_identifier)
        elif self.config.user_anonymization is UserAnonymization.LEGACY:
            return escape_text(user.full_name)  # it's shown on message forward anyway

    async def get_maybe_language(self, user: tg.User) -> MaybeLanguage:
        if self.language_store is not None:
            return await self.language_store.get_user_language(user)
        else:
            return None

    async def _handle_user_message(
        self,
        bot: AsyncTeleBot,
        user: tg.User,
        message_forwarder: Callable[[Optional[int]], Awaitable[MessageForwarderResult]],
        user_replier: UserReplier,
        send_user_identifier: bool,
        export_to_integrations: bool = True,
    ) -> Optional[int]:
        try:
            return await self._handle_user_message_or_fail(
                bot=bot,
                user=user,
                message_forwarder=message_forwarder,
                user_replier=user_replier,
                send_user_identifier=send_user_identifier,
                export_to_integrations=export_to_integrations,
            )
        except Exception:
            if self.service_messages.something_went_wrong is not None:
                try:
                    await bot.send_message(
                        chat_id=user.id,
                        text=any_text_to_str(
                            self.service_messages.something_went_wrong,
                            await self.get_maybe_language(user),
                        ),
                    )
                except Exception:
                    pass
            raise

    async def _handle_user_message_or_fail(
        self,
        bot: AsyncTeleBot,
        user: tg.User,
        message_forwarder: Callable[[Optional[int]], Awaitable[MessageForwarderResult]],
        user_replier: UserReplier,
        send_user_identifier: bool,
        export_to_integrations: bool = True,
    ) -> Optional[int]:
        if self.banned_users_store is not None and await self.banned_users_store.is_banned(user.id):
            return None
        anti_spam_status = await self.anti_spam.status(user)
        if anti_spam_status is AntiSpamStatus.SOFT_BAN:
            return None

        language = await self.get_maybe_language(user)
        if anti_spam_status is AntiSpamStatus.THROTTLING:
            anti_spam = cast(AntiSpam, self.anti_spam)  # only real AntiSpam can return this status
            await user_replier(
                self.service_messages.throttling(anti_spam.config, language),
                None,
                None,
            )
            return None

        category = await self.category_store.get_user_category(user) if self.category_store is not None else None

        _message_thread_id: Optional[int] = None

        async def with_message_thread_id(fn: Callable[[int | None], Awaitable[T]]) -> T:
            """
            Message thread id is an identifier of "forum topic", we use it in various ways based on configuration.

            Determining message thread id can be tricky, for example we may find out the thread saved in bot's
            memory had ben deleted and we need to re-create it. To facilitate this, all logic dealing with message
            thread id must be packed into a function and passed into function. This function is intended for use
            with actions that actually depend on forum topic's validity (e.g. sending message), simple saving can
            be done with "getter" funciton (see below).
            """
            # using "cached" value to avoid double loading
            nonlocal _message_thread_id
            if _message_thread_id is not None:
                return await fn(_message_thread_id)

            # the case of topic = category, loading message thread id from the store
            if self.forum_topic_store is not None:
                _message_thread_id = await self.forum_topic_store.get_message_thread_id(category)
                # TODO: handle case when category's topic was deleted?
                return await fn(_message_thread_id)

            # the case of topic = user
            if self.config.forum_topic_per_user:
                _message_thread_id = await self.message_thread_id_by_user_id_store.load(user.id)
                await self.message_thread_id_by_user_id_store.touch(user.id)

                # first, trying to use saved message thread id
                if _message_thread_id is not None:
                    try:
                        return await fn(_message_thread_id)
                    except tgapi.ApiHTTPException as e:
                        # if it was deleted, proceed to topic creation
                        if "message thread not found" in str(e):
                            self.logger.info("Saved message thread seems to be deleted, will create new one")
                            _message_thread_id = None
                        else:
                            raise e

                # no saved valid message thread id, will create new one
                if _message_thread_id is None:
                    try:
                        new_topic = await bot.create_forum_topic(
                            chat_id=self.admin_chat_id,
                            name=self.user_identifier(user, support_html=False),
                        )
                        _message_thread_id = new_topic.message_thread_id
                        await self.message_thread_id_by_user_id_store.save(user.id, _message_thread_id)
                        return await fn(_message_thread_id)
                    except Exception:
                        self.logger.exception(
                            f"Error creating forum topic for user {user}, will send without message thread id"
                        )

            # no message thread id, default case or fallback for errors in the code above
            return await fn(None)

        async def get_message_thread_id() -> int | None:
            # hack to turn call_with_... function into a simple getter
            return await with_message_thread_id(async_noop)

        hashtag_msg_data: Optional[HashtagMessageData] = None
        if self.config.hashtags_in_admin_chat:
            category_hashtag = None  # sentinel
            if self.category_store is not None:
                if category is None:
                    if self.config.force_category_selection:
                        # see validate_service_messages
                        you_must_select_category = cast(AnyText, self.service_messages.you_must_select_category)
                        await user_replier(
                            any_text_to_str(you_must_select_category, language),
                            await self.category_store.markup_for_user(user),
                            None,
                        )
                        return None
                else:
                    category_hashtag = category.hashtag

            # see runtime check for the hashtags_in_admin_chat flag and creation of the store
            hashtag_msg_data = await self.recent_hashtag_message_for_user_store.load(user.id)
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

                if hashtags:
                    hashtag_msg = await with_message_thread_id(
                        lambda message_thread_id: bot.send_message(
                            self.admin_chat_id,
                            _join_hashtags(hashtags),
                            message_thread_id=message_thread_id,
                        )
                    )
                    await self.user_related_messages_store.add(user.id, hashtag_msg.id, reset_ttl=False)
                    hashtag_msg_data = HashtagMessageData(message_id=hashtag_msg.id, hashtags=hashtags)
                    await self.recent_hashtag_message_for_user_store.save(user.id, hashtag_msg_data)

        if send_user_identifier and not self.config.forum_topic_per_user:
            user_identifier = self.user_identifier(user, support_html=True)
            last_sent_user_identifier = await self.last_sent_user_identifier_store.load(self.CONST_KEY)
            if last_sent_user_identifier is None or last_sent_user_identifier != user_identifier:
                user_identifier_msg = await with_message_thread_id(
                    lambda message_thread_id: bot.send_message(
                        self.admin_chat_id,
                        user_identifier,
                        message_thread_id=message_thread_id,
                        parse_mode="HTML",
                    )
                )
                await self.last_sent_user_identifier_store.save(self.CONST_KEY, user_identifier)
                await self.save_message_from_user(
                    user, user_identifier_msg.id, message_thread_id=await get_message_thread_id()
                )

        preforwarded_msg = None
        if self.config.before_forwarding is not None:
            preforwarded_msg = await self.config.before_forwarding(user)
            if isinstance(preforwarded_msg, tg.Message):
                await self.save_message_from_user(
                    user, preforwarded_msg.id, message_thread_id=await get_message_thread_id()
                )

        message_forwarder_result = await with_message_thread_id(message_forwarder)
        await self.save_message_from_user(
            user, message_forwarder_result.admin_chat_msg.id, message_thread_id=await get_message_thread_id()
        )

        postforwarded_msg = None
        if self.config.after_forwarding is not None:
            postforwarded_msg = await self.config.after_forwarding(user)
            if isinstance(postforwarded_msg, tg.Message):
                await self.save_message_from_user(
                    user, postforwarded_msg.id, message_thread_id=await get_message_thread_id()
                )

        if self.config.hashtags_in_admin_chat and hashtag_msg_data is not None:
            await self.hashtag_message_for_forwarded_message_store.save(
                message_forwarder_result.admin_chat_msg.id, hashtag_msg_data
            )

        confirmation_msg: str | None = None
        if self.service_messages.forwarded_to_admin_ok is not None and (
            self.config.confirm_forwarded_to_admin_rarer_than is None
            or not await self.recently_sent_confirmation_flag_store.is_flag_set(user.id)
        ):
            confirmation_msg = any_text_to_str(self.service_messages.forwarded_to_admin_ok, language)
            if self.config.confirm_forwarded_to_admin_rarer_than is not None:
                await self.recently_sent_confirmation_flag_store.set_flag(user.id)

        reaction = (
            tg.ReactionTypeEmoji(self.service_messages.forwarded_to_admin_reaction)
            if self.service_messages.forwarded_to_admin_reaction is not None
            else None
        )
        await user_replier(confirmation_msg, None, reaction)

        if export_to_integrations:
            # integrations have no concept of pre- and post-forwarded messages, so we just patch their texts
            # to the admin chat msg; their attachments and other info is lost, which is fine because we don't
            # use them that often anymore
            if preforwarded_msg is not None:
                message_forwarder_result.admin_chat_msg.text = (
                    "[pre]: "
                    + preforwarded_msg.text_content
                    + "\n\n"
                    + message_forwarder_result.admin_chat_msg.text_content
                )
            if postforwarded_msg is not None:
                message_forwarder_result.admin_chat_msg.text = (
                    message_forwarder_result.admin_chat_msg.text_content
                    + "\n\n"
                    + "[post]: "
                    + postforwarded_msg.text_content
                )

            await asyncio.gather(
                *[
                    integration.handle_user_message(
                        admin_chat_message=message_forwarder_result.admin_chat_msg,
                        user=user,
                        user_message=message_forwarder_result.user_msg,
                        category=category,
                        bot=bot,
                    )
                    for integration in self.integrations
                ]
            )
        return message_forwarder_result.admin_chat_msg.id

    async def emulate_user_message(
        self,
        bot: AsyncTeleBot,
        user: tg.User,
        text: str,
        attachment: Optional[TelegramAttachment] = None,
        no_response: bool = False,
        export_to_trello: bool = True,
        remove_exif_data: bool = True,
        send_user_id_hash_message: bool = False,  # DEPRECATED, USE send_user_identifier_message
        send_user_identifier_message: bool = False,
        # if the bot uses reactions to signal sent message, users need to specify which message to react to (if any)
        message_id_to_react_to: int | None = None,
        **send_message_kwargs,
    ) -> Optional[int]:
        """
        Sometimes we want FeedbackHandler to act like the user has sent us a message, but without actually
        a message there (they might have pressed a button or interacted with the bot in some other way). This
        method can be used in such cases.

        If the message has been successfully sent to the admin chat, this method returns its id.
        """

        async def message_forwarder(message_thread_id: Optional[int]) -> MessageForwarderResult:
            if attachment is None:
                if "message_thread_id" not in send_message_kwargs:
                    send_message_kwargs["message_thread_id"] = message_thread_id
                sent_msg = await bot.send_message(
                    self.admin_chat_id,
                    text=text,
                    **send_message_kwargs,
                )
            else:
                sent_msg = await send_attachment(
                    bot,
                    self.admin_chat_id,
                    attachment,
                    text,
                    remove_exif_data,
                    message_thread_id=message_thread_id,
                    **send_message_kwargs,
                )
            return MessageForwarderResult(
                admin_chat_msg=sent_msg,
                user_msg=None,
            )

        async def user_replier(text: str | None, reply_markup: tg.ReplyMarkup | None, reaction: tg.ReactionType | None):
            if no_response:
                return
            if text:
                await bot.send_message(
                    user.id,
                    text=text,
                    reply_markup=reply_markup,
                    reply_to_message_id=message_id_to_react_to,
                )
            if reaction and message_id_to_react_to is not None:
                await bot.set_message_reaction(
                    user.id,
                    message_id=message_id_to_react_to,
                    reaction=[reaction],
                )

        return await self._handle_user_message(
            bot=bot,
            user=user,
            message_forwarder=message_forwarder,
            user_replier=user_replier,
            send_user_identifier=send_user_identifier_message or send_user_id_hash_message,
            export_to_integrations=export_to_trello,
        )

    async def handle_user_message(self, message: tg.Message, bot: AsyncTeleBot, reply_to_user: bool) -> Optional[int]:
        async def message_forwarder(message_thread_id: Optional[int]) -> MessageForwarderResult:
            if self.config.user_anonymization is UserAnonymization.LEGACY:
                forwarded_message = await bot.forward_message(
                    self.admin_chat_id,
                    from_chat_id=message.chat.id,
                    message_id=message.id,
                    message_thread_id=message_thread_id,
                )
                return MessageForwarderResult(admin_chat_msg=forwarded_message, user_msg=message)
            else:
                copied_message_id = await bot.copy_message(
                    chat_id=self.admin_chat_id,
                    from_chat_id=message.chat.id,
                    message_id=message.id,
                    message_thread_id=message_thread_id,
                )
                fake_admin_chat_message = copy.deepcopy(message)
                fake_admin_chat_message.chat = await self.admin_chat()
                fake_admin_chat_message.id = copied_message_id.message_id
                return MessageForwarderResult(admin_chat_msg=fake_admin_chat_message, user_msg=message)

        async def user_replier(text: str | None, reply_markup: tg.ReplyMarkup | None, reaction: tg.ReactionType | None):
            if not reply_to_user:
                return

            if text:
                await bot.reply_to(message, text, reply_markup=reply_markup)
            if reaction is not None:
                await bot.set_message_reaction(
                    chat_id=message.chat.id,
                    message_id=message.id,
                    reaction=[reaction],
                )

        return await self._handle_user_message(
            bot=bot,
            user=message.from_user,
            message_forwarder=message_forwarder,
            send_user_identifier=self.config.user_anonymization is not UserAnonymization.LEGACY,
            user_replier=user_replier,
        )

    async def setup(self, bot: AsyncTeleBot) -> None:
        # user messages handler
        @bot.message_handler(
            func=cast(FilterFunc, self._user_message_filter),
            chat_types=[tg_constants.ChatType.private],
            content_types=list(tg_constants.MediaContentType),
            priority=-200,  # lowest priority to process the rest of the handlers first
            name="feedback-user-to-admin",
        )
        async def handle_user_message(message: tg.Message) -> None:
            await self.handle_user_message(message, bot=bot, reply_to_user=True)

        await self.setup_without_user_message_handler(bot)

    async def setup_without_user_message_handler(self, bot: AsyncTeleBot) -> None:
        await self.setup_admin_chat_handlers(bot)
        self._bot = bot
        for integration in self.integrations:
            await integration.setup(bot)
        if self.forum_topic_store is not None:
            await self.forum_topic_store.setup(bot)

    async def aux_endpoints(self) -> list[AuxBotEndpoint]:
        endpoints: list[AuxBotEndpoint] = []
        for integration in self.integrations:
            endpoints.extend(await integration.aux_endpoints())
        return endpoints

    def background_jobs(
        self,
        base_url: Optional[str],
        server_listening_future: Optional[asyncio.Future[None]],
    ) -> list[Coroutine[None, None, None]]:
        integration_backgroung_jobs = [
            i.background_job(FeedbackIntegrationBackgroundContext(base_url, server_listening_future))
            for i in self.integrations
        ]
        self_background_jobs: list[Coroutine[None, None, None]] = []
        if self.forum_topic_store is not None:
            self_background_jobs.append(self.forum_topic_store.background_job())
        return self_background_jobs + integration_backgroung_jobs

    async def _remove_unanswered_hashtag(self, bot: AsyncTeleBot, message_id: int):
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
        finally:
            await self.hashtag_message_for_forwarded_message_store.save(message_id, hashtag_message_data)

    async def message_replied_from_integration_callback(
        self,
        event: UserMessageRepliedFromIntegrationEvent,
        *,
        notify_integrations: bool = True,
    ) -> None:
        self.logger.debug(f"Message replied from integration: {event!r}")
        if self.config.hashtags_in_admin_chat:
            await self._remove_unanswered_hashtag(event.bot, event.main_admin_chat_message_id)

        integration_name = telegram_html_escape(event.integration.name())
        cloned_reply_message = await event.bot.send_message(
            chat_id=self.admin_chat_id,
            reply_to_message_id=event.main_admin_chat_message_id,
            text=(
                "💬 "
                + hbold(telegram_html_escape(event.reply_author or "<unknown admin>"), escape=False)
                + " via "
                + (html_link(event.reply_link, integration_name) if event.reply_link else integration_name)
                + (("\n\n" + event.reply_text) if event.reply_text else "")
                + ("\n\n📎 attachment" if event.reply_has_attachments else "")
            ),
            parse_mode="HTML",
        )

        await self.message_log_store.push(event.origin_chat_id, cloned_reply_message.id)

        if notify_integrations:
            # do not notify integration about its own replies
            integrations_to_notify = [i for i in self.integrations if i is not event.integration]
            self.logger.debug(f"Notifying integrations: {[i.name() for i in integrations_to_notify]}")
            await asyncio.gather(
                *[integration.handle_user_message_replied_elsewhere(event) for integration in integrations_to_notify]
            )
        else:
            self.logger.debug("Will not notify integrations")

    async def setup_admin_chat_handlers(
        self,
        bot: AsyncTeleBot,
        on_admin_reply_to_bot: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        @bot.message_handler(chat_id=[self.admin_chat_id], commands=["help"])
        async def admin_chat_help(message: tg.Message):
            await bot.reply_to(
                message,
                self._admin_help_message(),
                disable_web_page_preview=True,
                disable_notification=True,
                parse_mode="HTML",
            )

        for integration in self.integrations:
            integration.set_message_replied_callback(self.message_replied_from_integration_callback)

        @bot.message_handler(
            chat_id=[self.admin_chat_id],
            is_reply=True,
            commands=["undo"],
        )
        async def admin_undo_forwarded_message(message: tg.Message):
            replied_to_message = message.reply_to_message
            if replied_to_message is None:
                return
            copied_message_data = await self.copied_to_user_data_store.load(replied_to_message.id)
            if copied_message_data is None:
                if self.service_messages.can_not_delete_message is not None:
                    await bot.reply_to(message, self.service_messages.can_not_delete_message)
                return
            origin_chat_id = copied_message_data["origin_chat_id"]
            sent_message_id = copied_message_data["sent_message_id"]
            try:
                await bot.delete_message(origin_chat_id, sent_message_id)
                if self.service_messages.deleted_message_ok is not None:
                    await bot.reply_to(message, self.service_messages.deleted_message_ok)
                await self.copied_to_user_data_store.drop(replied_to_message.id)
            except Exception:
                self.logger.exception("Error deleting message from the user chat")
                if self.service_messages.can_not_delete_message is not None:
                    await bot.reply_to(message, self.service_messages.can_not_delete_message)

        @bot.message_handler(
            chat_id=[self.admin_chat_id],
            content_types=list(tg_constants.MediaContentType),
            priority=-100,  # to process commands in admin chat first
            name="feedback-admin-to-user",
        )
        async def admin_to_bot(message: tg.Message) -> HandlerResult | None:
            ignore_message = HandlerResult(
                # in case the admin chat is a private one, it's very likely the bot-admin
                # private messages; in this case we want to let users sent messages to bot
                # as users, so it's necessary to let user message handler to do its job
                continue_to_other_handlers=((await self.admin_chat()).type == "private"),
            )
            try:
                replied_to_msg = message.reply_to_message
                if replied_to_msg is not None and replied_to_msg.forum_topic_created is None:
                    self.logger.debug("Message in admin chat is a non-trivial reply")
                    forwarded_msg_id = replied_to_msg.id
                    forwarded_msg: Optional[tg.Message] = replied_to_msg
                elif not self.config.forum_topic_per_user:
                    self.logger.debug(
                        "Ignoring message in admin chat: not a reply and forum topic per user not enabled"
                    )
                    return ignore_message
                elif not self.config.any_message_in_user_topic_is_reply:
                    self.logger.debug(
                        "Ignoring message in admin chat: not a reply and "
                        + f"{self.config.any_message_in_user_topic_is_reply = }"
                    )
                    return ignore_message
                else:
                    forwarded_msg = None
                    if message.message_thread_id is None:
                        self.logger.debug("Message in admin chat is not a reply and not in topic")
                        return ignore_message
                    maybe_forwarded_msg_id = await self.last_forwarded_message_id_by_message_thread_id.load(
                        message.message_thread_id
                    )
                    if maybe_forwarded_msg_id is None:
                        self.logger.debug("Message in admin chat is not a reply and not in user's topic")
                        return ignore_message
                    forwarded_msg_id = maybe_forwarded_msg_id

                origin_chat_id = await self.origin_chat_id_store.load(forwarded_msg_id)
                if origin_chat_id is None:
                    return ignore_message

                if message.text is not None and message.text.startswith("/"):
                    # admin chat commands
                    if message.text in self.admin_chat_response_action_by_command:
                        if forwarded_msg is None:
                            raise RuntimeError("To execute command, please reply to a user's message directly.")
                        admin_chat_action = self.admin_chat_response_action_by_command[message.text]
                        await admin_chat_action.callback(message, forwarded_msg, origin_chat_id)
                        if admin_chat_action.delete_everything_related_to_user_after:
                            await self._delete_user_related_messages(
                                bot=bot, origin_chat_id=origin_chat_id, initiator_message_id=message.id
                            )
                    elif message.text.strip() == "/ban" and self.banned_users_store is not None:
                        await self.banned_users_store.ban_user(user_id=origin_chat_id)
                        await self._delete_user_related_messages(
                            bot=bot, origin_chat_id=origin_chat_id, initiator_message_id=message.id
                        )
                    elif message.text_content.startswith("/log"):
                        try:
                            page_str = extract_arguments(message.text_content) or "1"
                            page = int(page_str)
                            if page > 0:
                                page -= 1  # one based to zero based
                        except Exception:
                            await bot.reply_to(
                                message, "Bad command, expected format is '/log' or '/log <page number>'"
                            )
                            return None
                        log_message_ids = await self.message_log_store.all(origin_chat_id)
                        total_pages = int(math.ceil(len(log_message_ids) / self.config.message_log_page_size))
                        if page < 0:
                            page = page % total_pages  # wrapping so that -1 = last, -2 = second to last, etc
                        start_idx = self.config.message_log_page_size * page
                        end_idx = self.config.message_log_page_size * (page + 1)
                        log_message_ids_page = log_message_ids[start_idx:end_idx]
                        self.logger.info(
                            f"Forwarding log page {page} / {total_pages} (from {message.text_content!r}) "
                            + f"received for origin chat id {origin_chat_id}, total messages: {len(log_message_ids)}, "
                            + f"on current page: {len(log_message_ids_page)}"
                        )
                        if not log_message_ids_page:
                            if page == 0:
                                await bot.reply_to(message, "Message log with this user is not available :(")
                            else:
                                await bot.reply_to(
                                    message,
                                    f"Only {len(log_message_ids)} messages are available in log, "
                                    + f"not enough messages for page {page}",
                                )
                            return None
                        log_destination_chat_id = (
                            self.admin_chat_id if self.config.message_log_to_admin_chat else message.from_user.id
                        )
                        await bot.send_message(
                            chat_id=log_destination_chat_id,
                            text=f"📜 Log page {page + 1} / {total_pages}",
                        )
                        for message_id in log_message_ids_page:
                            try:
                                log_message = await bot.forward_message(
                                    chat_id=log_destination_chat_id,
                                    from_chat_id=self.admin_chat_id,
                                    message_id=message_id,
                                )
                                if self.config.message_log_to_admin_chat:
                                    # to be able to reply to them as to normal forwarded messages...
                                    await self.origin_chat_id_store.save(log_message.id, origin_chat_id)
                                    # ... and to delete them in case of user ban
                                    await self.user_related_messages_store.add(origin_chat_id, log_message.id)
                            except Exception:
                                self.logger.info(
                                    f"Error forwarding message for /log command, {page = }; {total_pages = }",
                                    exc_info=True,
                                )
                                await bot.send_message(
                                    chat_id=log_destination_chat_id,
                                    text="Failed to send log message!",
                                )
                            await asyncio.sleep(0.5)  # soft rate limit prevention
                        await bot.send_message(
                            chat_id=log_destination_chat_id,
                            text=(
                                f"⬆️ Log page {page + 1} / {total_pages}"
                                + (f"\nNext: <code>/log {page + 2}</code>" if page + 1 < total_pages else "")
                            ),
                            parse_mode="HTML",
                        )
                    else:
                        available_commands = list(self.admin_chat_response_action_by_command.keys()) + ["/log"]
                        if self.banned_users_store is not None:
                            available_commands.append("/ban")
                        await bot.reply_to(
                            message,
                            f"Invalid admin chat command: {message.text!r}; available commands are: "
                            + ", ".join(repr(cmd) for cmd in available_commands),
                        )
                else:
                    # actual response to the user
                    try:
                        copied_message_id = await bot.copy_message(
                            chat_id=origin_chat_id, from_chat_id=self.admin_chat_id, message_id=message.id
                        )
                    except ApiHTTPException as e:
                        # this is normal and most likely means that user has blocked the bot
                        self.logger.info(f"Error copying message to user chat. {e!r}")
                        await bot.reply_to(message, str(e))
                        return None
                    await self.message_log_store.push(origin_chat_id, message.id)

                    await self.copied_to_user_data_store.save(
                        message.id,
                        CopiedMessageToUserData(
                            origin_chat_id=origin_chat_id, sent_message_id=int(copied_message_id.message_id)
                        ),
                    )
                    if self.service_messages.copied_to_user_ok is not None:
                        copied_to_user_ok_message = await bot.reply_to(message, self.service_messages.copied_to_user_ok)
                        await self.copied_to_user_data_store.save(
                            copied_to_user_ok_message.id,
                            CopiedMessageToUserData(
                                origin_chat_id=origin_chat_id, sent_message_id=int(copied_message_id.message_id)
                            ),
                        )

                    if self.config.hashtags_in_admin_chat:
                        await self._remove_unanswered_hashtag(bot, forwarded_msg_id)
                    has_attachments = message.content_type != "text"
                    maybe_exceptions = await asyncio.gather(
                        *[
                            integration.handle_user_message_replied_elsewhere(
                                UserMessageRepliedEvent(
                                    bot=bot,
                                    origin_chat_id=origin_chat_id,
                                    reply_text=(
                                        (message.html_text if not has_attachments else message.html_caption) or ""
                                    ),
                                    reply_has_attachments=has_attachments,
                                    reply_author=message.from_user.first_name,
                                    reply_link=telegram_message_url(self.admin_chat_id, message.id),
                                    main_admin_chat_message_id=forwarded_msg_id,
                                )
                            )
                            for integration in self.integrations
                        ],
                        return_exceptions=True,
                    )
                    for maybe_exception, integration in zip(maybe_exceptions, self.integrations):
                        if maybe_exception is None:
                            continue
                        elif isinstance(maybe_exception, Exception):
                            self.logger.error(
                                f"Error notifying integration {integration.name()!r}, ignoring: {maybe_exception!r}"
                            )
                        else:
                            self.logger.warning(
                                f"Unexpected value returned from notifying integration {integration.name()!r}, "
                                + f"ignoring: {maybe_exception!r}"
                            )
            except Exception as e:
                await bot.reply_to(message, f"Something went wrong! {e}")
                self.logger.exception("Unexpected error replying to user")
            return None


def _join_hashtags(hashtags: list[str]) -> str:
    return " ".join(["#" + h for h in hashtags])


async def async_noop(x: int | None) -> int | None:
    return x
