import pytest
import pytest_mock

from telebot_components.redis_utils.emulation import RedisEmulation
from tests.utils import TimeSupplier


@pytest.fixture
def redis() -> RedisEmulation:
    return RedisEmulation()


@pytest.fixture
def time_supplier(mocker: pytest_mock.MockerFixture) -> TimeSupplier:
    return TimeSupplier(mocker)
