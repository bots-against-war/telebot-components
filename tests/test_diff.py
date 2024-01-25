import copy
from typing import Any

import pytest

from telebot_components.utils.diff import DiffAction, diff, patch


@pytest.mark.parametrize(
    "a, b, expected_diff",
    [
        pytest.param([], [], []),
        pytest.param({}, {}, []),
        pytest.param({}, {"a": "b"}, [{"action": "add", "values": {"a": "b"}, "path": []}]),
        pytest.param({"a": "b"}, {}, [{"action": "remove", "path": [], "values": {"a": "b"}}]),
        pytest.param({"a": "c"}, {"a": "b"}, [{"action": "change", "new": "b", "old": "c", "path": ["a"]}]),
        pytest.param({"a": [1, 2, 3]}, {"a": "b"}, [{"action": "change", "new": "b", "old": [1, 2, 3], "path": ["a"]}]),
        pytest.param({"a": "c"}, {"a": None}, [{"action": "change", "new": None, "old": "c", "path": ["a"]}]),
        pytest.param({"a": "c"}, {"a": "c"}, []),
        pytest.param({"a": 1.0}, {"a": 1.01}, [{"action": "change", "new": 1.01, "old": 1.0, "path": ["a"]}]),
        pytest.param([], [1], [{"action": "add_range", "start": 0, "path": [], "values": [1]}]),
        pytest.param([1, 2, 3], [1], [{"action": "remove_range", "path": [], "start": 1, "values": [2, 3]}]),
        pytest.param([1, 2, 3, 4], [1, 4], [{"action": "remove_range", "path": [], "start": 1, "values": [2, 3]}]),
        pytest.param(
            {"a": [1]},
            {"a": [1, 2, 3]},
            [{"action": "add_range", "start": 1, "path": ["a"], "values": [2, 3]}],
        ),
        pytest.param(
            {"a": 1, "b": 12},
            {"a": 2, "c": 5},
            [
                {"path": ["a"], "action": "change", "old": 1, "new": 2},
                {"path": [], "action": "remove", "values": {"b": 12}},
                {"path": [], "action": "add", "values": {"c": 5}},
            ],
        ),
        pytest.param(
            ["a", "b", "c", "y", "e", "f", "x"],
            ["b", "c", "x", "e", "f"],
            [
                {"path": [], "action": "remove_range", "start": 0, "values": ["a"]},
                {"path": [3], "action": "change", "old": "y", "new": "x"},
                {"path": [], "action": "remove_range", "start": 6, "values": ["x"]},
            ],
        ),
        pytest.param(
            ["a", "b", "c", {"nested": [{"value": "y", "other": 123}]}, "e", "f"],
            ["extra", "a", "b", "c", {"nested": [{"value": "x"}]}, "e", "f"],
            [
                {"path": [], "action": "add_range", "start": 0, "values": ["extra"]},
                {"path": [3, "nested", 0, "value"], "action": "change", "old": "y", "new": "x"},
                {"path": [3, "nested", 0], "action": "remove", "values": {"other": 123}},
            ],
        ),
        pytest.param(
            [
                {"id": 0, "name": "Alice", "likes": 100},
                {"id": 1, "name": "Bob", "likes": 10},
                {"id": 2, "name": "Clare", "likes": 15},
                {"id": 3, "name": "Peter", "likes": 0},
                {"id": 4, "name": "Mary", "likes": 153},
                {"id": 5, "name": "Carl", "likes": 5},
            ],
            [
                {"id": -1, "name": "admin", "likes": 0},
                {"id": 0, "name": "Alice", "likes": 100},
                {"id": 1, "name": "Bob", "likes": 10},
                {"id": 3, "name": "Peter", "likes": 0},
                {"id": 4, "name": "Mary", "likes": 155},
                {"id": 5, "name": "Carl", "likes": 5},
                {"id": 6, "name": "Cindy", "likes": 1},
            ],
            [
                {"path": [], "action": "add_range", "start": 0, "values": [{"id": -1, "name": "admin", "likes": 0}]},
                {"path": [], "action": "remove_range", "start": 2, "values": [{"id": 2, "name": "Clare", "likes": 15}]},
                {"path": [4, "likes"], "action": "change", "old": 153, "new": 155},
                {"path": [], "action": "add_range", "start": 6, "values": [{"id": 6, "name": "Cindy", "likes": 1}]},
            ],
        ),
        pytest.param(
            {
                "id": 1,
                "code": None,
                "type": "foo",
                "bars": [{"id": 6934900}, {"id": 6934977}, {"id": 6934992}, {"id": 6934993}, {"id": 6935014}],
                "n": 10,
                "date_str": "2013-07-08 00:00:00",
                "float_here": 0.454545,
                "complex": [{"id": 83865, "goal": 2.0, "state": "active"}],
                "profile_id": None,
                "state": "active",
            },
            {
                "id": "2",
                "code": None,
                "type": "foo",
                "bars": [{"id": 6934900}, {"id": 6934977}, {"id": 6935000}, {"id": 6934993}, {"id": 6935014}],
                "n": 10,
                "date_str": "2013-07-08 00:00:00",
                "float_here": 0.454545,
                "complex": [{"id": 83865, "goal": 2.0, "state": "active"}],
                "profile_id": None,
                "state": "active",
            },
            [
                {"path": ["id"], "action": "change", "old": 1, "new": "2"},
                {"path": ["bars", 2, "id"], "action": "change", "old": 6934992, "new": 6935000},
            ],
        ),
        pytest.param({0: 1}, {0: 2}, [{"action": "change", "new": 2, "old": 1, "path": [0]}]),
        pytest.param(
            {0: {"1": {2: 3}}},
            {0: {"1": {2: "haha"}}},
            [{"action": "change", "new": "haha", "old": 3, "path": [0, "1", 2]}],
        ),
        pytest.param(
            [*range(10), 1, 2, 3, 4, 5, *range(10)],
            [*range(10), 7, 8, *range(10)],
            [
                {"path": [10], "action": "change", "old": 1, "new": 7},
                {"path": [11], "action": "change", "old": 2, "new": 8},
                {"path": [], "action": "remove_range", "start": 12, "values": [3, 4, 5]},
            ],
        ),
        pytest.param(
            [*range(100), 1, 2, 3, 4, 5, *range(200)],
            [*range(100), 100, 4, 300, *range(100)],
            [
                {"path": [], "action": "remove_range", "start": 0, "values": list(range(100)) + list(range(1, 6))},
                {"path": [206], "action": "change", "old": 101, "new": 4},
                {"path": [207], "action": "change", "old": 102, "new": 300},
                {"path": [208], "action": "change", "old": 103, "new": 0},
                {"path": [209], "action": "change", "old": 104, "new": 1},
                {"path": [], "action": "remove_range", "start": 210, "values": list(range(105, 200))},
                {"path": [], "action": "add_range", "start": 305, "values": list(range(2, 100))},
            ],
        ),
    ],
)
@pytest.mark.parametrize("invert_a_b", [True, False])
def test_diff(a: Any, b: Any, expected_diff: list[DiffAction], invert_a_b: bool) -> None:
    # ensuring test isolation for repeated a, b value tests
    a = copy.deepcopy(a)
    b = copy.deepcopy(b)

    if invert_a_b:
        b, a = a, b

    a_initial = copy.deepcopy(a)
    b_initial = copy.deepcopy(b)

    diff_ = diff(a, b)

    if not invert_a_b:
        # we only specify expected diff for non-inverted, but everything else we still can check
        assert diff_ == expected_diff

    b_patched = patch(a, diff_)
    assert b_patched == b
    assert a == a_initial
    assert b == b_initial

    patch(a, diff_, in_place=True)
    assert a == b
    assert a == b_initial
