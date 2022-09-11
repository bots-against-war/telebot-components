from telebot import types as tg

from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.user_group import UserGroupStore


async def test_user_group_store_basic(redis: RedisInterface):
    user_group = UserGroupStore(
        redis=redis,
        prefix="testing",
        group_name="something",
    )

    assert not (await user_group.member_identities())

    user_1 = tg.User(id=1, is_bot=False, first_name="Alice")
    user_2 = tg.User(id=2, is_bot=False, first_name="Bob")
    user_3 = tg.User(id=3, is_bot=False, first_name="Cecile")

    for u in (user_1, user_2, user_3):
        assert not await user_group.is_member(u)
        assert not await user_group.is_member_by_identity(str(u.id))

    assert await user_group.add(user_1)
    assert not await user_group.add(user_1)

    assert await user_group.member_identities() == {"1"}
    assert await user_group.is_member(user_1)
    assert not await user_group.is_member(user_2)
    assert not await user_group.is_member(user_3)

    assert await user_group.add(user_2)
    assert await user_group.member_identities() == {"1", "2"}
    assert await user_group.is_member(user_1)
    assert await user_group.is_member(user_2)
    assert not await user_group.is_member(user_3)

    assert await user_group.add(user_3)
    assert await user_group.remove(user_2)
    assert await user_group.member_identities() == {"1", "3"}
    assert await user_group.is_member(user_1)
    assert not await user_group.is_member(user_2)
    assert await user_group.is_member(user_3)


async def test_user_group_store_custom_identity(redis: RedisInterface):
    async def custom_identity(user: tg.User) -> str:
        if user.username is None:
            raise ValueError("No valid identity for the user")
        return f"{user.full_name} (@{user.username})"

    user_group = UserGroupStore(
        redis=redis,
        prefix="testing",
        group_name="something",
        user_identity=custom_identity,
    )

    user_1 = tg.User(id=1, is_bot=False, first_name="Alice", username="alice")
    user_2 = tg.User(id=2, is_bot=False, first_name="Bob", last_name="Silva", username="fire_walk")
    user_3 = tg.User(id=3, is_bot=False, first_name="Cecile")

    assert await user_group.add(user_1)

    assert await user_group.member_identities() == {"Alice (@alice)"}
    assert await user_group.is_member(user_1)
    assert not await user_group.is_member(user_2)
    assert not await user_group.is_member(user_3)

    assert await user_group.add(user_2)
    assert await user_group.member_identities() == {"Alice (@alice)", "Bob Silva (@fire_walk)"}
    assert await user_group.is_member(user_1)
    assert await user_group.is_member(user_2)
    assert not await user_group.is_member(user_3)

    assert not await user_group.add(user_3), "should not be able to add user without valid identity (no username)"
    assert not await user_group.is_member(user_3)

    assert await user_group.remove(user_1)
    assert not await user_group.remove(user_1), "repeated remove should return False - user was not in a group"
    assert await user_group.member_identities() == {"Bob Silva (@fire_walk)"}
