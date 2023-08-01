from cryptography.fernet import Fernet

from telebot_components.utils.secrets import RedisSecretStore
from telebot_components.redis_utils.emulation import RedisEmulation


async def test_redis_secret_store() -> None:
    owner_id = 100310134
    ss = RedisSecretStore(
        redis=RedisEmulation(),
        encryption_key=Fernet.generate_key().decode("utf-8"),
        secrets_per_user=10,
        secret_max_len=100,
        scope_secrets_to_user=True,
    )
    await ss.save_secret("example", "1312", owner_id)
    await ss.save_secret("another-example", "hello world", owner_id)
    assert await ss.get_secret("example", owner_id) == "1312"
    assert await ss.get_secret("another-example", owner_id) == "hello world"
    assert set(await ss.list_secrets(owner_id)) == {"example", "another-example"}
    assert set(await ss.list_secrets(owner_id + 1)) == set()

