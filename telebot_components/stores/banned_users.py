from typing import Optional

from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.generic import SetStore
from telebot_components.constants import time


class BannedUsersStore:
    """Simple store for banned users. Implements inmemory cache -- we have to check
    if a user is banned far more often than we need to ban someone"""

    def __init__(self, bot_prefix: str, redis: RedisInterface, cached: bool):
        self.banned_user_ids_store = SetStore[int](
            name="banned-user-ids",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=time.FOREVER,
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
