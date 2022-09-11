from typing import Optional

from telebot import types as tg

from telebot_components.constants import times
from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.generic import SetStore


class BannedUsersStore:
    """Simple store for banned users. Implements inmemory cache: we have to check
    if a user is banned far more often than we need to ban someone.

    Currently only supports permanent ban.

    NOTE: deprecated, for new applications use UserGroupStore
    """

    def __init__(self, redis: RedisInterface, bot_prefix: str, cached: bool):
        self.banned_user_ids_store = SetStore[int](
            name="banned-user-ids",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=times.FOREVER,
        )
        self.cached = cached
        self._banned_user_ids_cache: Optional[set[int]] = None

    async def ban_user(self, user_id: int) -> bool:
        if not (await self.banned_user_ids_store.add(user_id)):
            return False
        if self.cached:
            if self._banned_user_ids_cache is None:
                self._banned_user_ids_cache = await self.banned_user_ids_store.all()
            self._banned_user_ids_cache.add(user_id)
        return True

    async def is_banned(self, user_id: int) -> bool:
        if self.cached:
            if self._banned_user_ids_cache is None:
                self._banned_user_ids_cache = await self.banned_user_ids_store.all()
            return user_id in self._banned_user_ids_cache
        else:
            return await self.banned_user_ids_store.includes(user_id)

    async def not_from_banned_user(self, update_content: tg.Message) -> bool:
        """Can be used in telebot's 'func' filter"""
        return not await self.is_banned(update_content.from_user.id)
