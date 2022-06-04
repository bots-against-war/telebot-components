import random
from telebot import types as tg

import pytest
from _pytest import fixtures

from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.banned_users import BannedUsersStore

from tests.utils import mock_bot_user_json


@pytest.fixture(params=[True, False])
def banned_users_store(request: fixtures.SubRequest, redis: RedisInterface) -> BannedUsersStore:
    return BannedUsersStore(
        bot_prefix="testing",
        redis=redis,
        cached=request.param,
    )


async def test_banned_users_store(banned_users_store: BannedUsersStore):

    def new_user() -> tg.User:
        json = mock_bot_user_json()
        json["id"] = random.randint(10**4, 10**5)
        return tg.User.de_json(json)

    user_1 = new_user()
    user_2 = new_user()
    assert not await banned_users_store.is_banned(user_1.id)
    assert not await banned_users_store.is_banned(user_2.id)
    await banned_users_store.ban_user(user_1.id)
    assert await banned_users_store.is_banned(user_1.id)
    assert not await banned_users_store.is_banned(user_2.id)

