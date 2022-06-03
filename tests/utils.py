import os

import pytest
import pytest_mock


class TimeSupplier:
    def __init__(self, mocker: pytest_mock.MockerFixture):
        self.current_time = 0.0
        mocker.patch("time.time", new=self.mock_time)

    def mock_time(self) -> float:
        return self.current_time

    # TODO: mock time.sleep and asyncio.sleep functions

    def emulate_wait(self, delay: float):
        self.current_time += delay


def using_real_redis() -> bool:
    return "REDIS_URL" in os.environ


pytest_skip_on_real_redis = pytest.mark.skipif(using_real_redis(), reason="Can't emulate sleeping with real redis")
