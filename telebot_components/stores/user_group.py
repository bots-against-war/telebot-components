import functools
import logging
from typing import Any, Awaitable, Callable, Coroutine, Optional

from telebot import AsyncTeleBot, invoke_handler
from telebot import types as tg
from telebot.types.service import HandlerFunction

from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.generic import SetStore

logger = logging.getLogger(__name__)


unity = lambda x: x


async def telegram_user_id_identity(user: tg.User) -> str:
    return str(user.id)


class UserGroupStore:
    """Generic user group store with an arbitrary user identity function.

    User identity is an async User -> str function, allowing user identification by id, username or any other attribute.
    """

    def __init__(
        self,
        redis: RedisInterface,
        prefix: str,
        group_name: str,
        user_identity: Callable[[tg.User], Awaitable[str]] = telegram_user_id_identity,
    ):
        self.name = group_name
        self.user_identity = user_identity

        self.store = SetStore[str](
            name=f"user-group-{group_name}",
            prefix=prefix,
            redis=redis,
            expiration_time=None,
            loader=unity,
            dumper=unity,
        )
        self.cache: set[str] = set()
        self.cache_initialized = False

    async def _ensure_cache_initialized(self):
        if not self.cache_initialized:
            self.cache = await self.store.all()
            self.cache_initialized = True

    async def member_identities(self) -> set[str]:
        await self._ensure_cache_initialized()
        return self.cache

    async def is_member_by_identity(self, uid: str) -> bool:
        return uid in await self.member_identities()

    async def is_member(self, user: tg.User) -> bool:
        try:
            uid = await self.user_identity(user)
            return await self.is_member_by_identity(uid)
        except Exception:
            return False

    def membership_required(self, bot: AsyncTeleBot, membership_required_reply_text: Optional[str] = None):
        def decorator(handler_func: HandlerFunction[tg.Message]) -> HandlerFunction[tg.Message]:
            @functools.wraps(handler_func)
            async def wrapped(*args) -> None:
                try:
                    message: tg.Message = args[0]
                    if await self.is_member(message.from_user):
                        return await invoke_handler(handler_func, message, bot)
                    else:
                        if membership_required_reply_text:
                            await bot.send_message(message.from_user.id, membership_required_reply_text)
                except Exception:
                    logger.exception("Unexpected error in membership_required decorator")
                    raise

            return wrapped  # type: ignore

        return decorator

    async def add_identity(self, uid: str) -> bool:
        await self._ensure_cache_initialized()
        if await self.store.add(uid):
            self.cache.add(uid)
            return True
        else:
            logger.error("Error saving user identity to the store")
            return False

    async def add(self, user: tg.User) -> bool:
        try:
            return await self.add_identity(await self.user_identity(user))
        except Exception:
            return False

    async def remove_identity(self, uid: str) -> bool:
        await self._ensure_cache_initialized()
        if await self.store.remove(uid):
            self.cache.discard(uid)
            return True
        else:
            logger.error("Error removing user identity from the store")
            return False

    async def remove(self, user: tg.User) -> bool:
        try:
            return await self.remove_identity(await self.user_identity(user))
        except Exception:
            return False
