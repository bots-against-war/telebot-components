from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from typing import Optional

from telebot import types as tg

from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.generic import KeyIntegerStore


class AntiSpamStatus(Enum):
    CLEAR = 0
    THROTTLING = 1
    SOFT_BAN = 2


@dataclass
class AntiSpamConfig:
    throttle_after_messages: int
    throttle_duration: timedelta
    soft_ban_after_throttle_violations: int
    soft_ban_duration: timedelta


class AntiSpamInterface(ABC):
    @abstractmethod
    async def status(self, user: tg.User) -> AntiSpamStatus:
        pass


class DisabledAntiSpam(AntiSpamInterface):
    async def status(self, user: tg.User) -> AntiSpamStatus:
        return AntiSpamStatus.CLEAR


class AntiSpam(AntiSpamInterface):
    def __init__(self, redis: RedisInterface, bot_prefix: str, config: AntiSpamConfig, name: Optional[str] = None):
        self.config = config
        if name:
            bot_prefix = bot_prefix + f"-antispam-{name}"
        self.recent_messages_counter = KeyIntegerStore(
            name="recent-msg-counter-for",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=self.config.throttle_duration,
        )
        self.recent_throttle_violations_counter = KeyIntegerStore(
            name="recent-throttle-violations-for",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=self.config.soft_ban_duration,
        )

    async def status(self, user: tg.User) -> AntiSpamStatus:
        violations = await self.recent_throttle_violations_counter.load(user.id)
        if violations is not None and int(violations) >= self.config.soft_ban_after_throttle_violations:
            return AntiSpamStatus.SOFT_BAN
        message_count = await self.recent_messages_counter.increment(user.id, reset_ttl=True)
        if message_count > self.config.throttle_after_messages:
            await self.recent_throttle_violations_counter.increment(user.id, reset_ttl=True)
            return AntiSpamStatus.THROTTLING
        else:
            return AntiSpamStatus.CLEAR
