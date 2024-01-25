import copy
from typing import Any

import pytest

from telebot_components.utils.diff import DiffItem, diff, patch


@pytest.mark.parametrize(
    "a, b, expected_diff",
    [
        pytest.param([], [], []),
        pytest.param({}, {}, []),
        pytest.param({}, {"a": "b"}, [{"action": "add", "added": {"a": "b"}, "path": []}]),
        pytest.param({"a": "b"}, {}, [{"action": "remove", "path": [], "removed": {"a": "b"}}]),
        pytest.param({"a": "c"}, {"a": "b"}, [{"action": "change", "new": "b", "old": "c", "path": ["a"]}]),
        pytest.param({"a": [1, 2, 3]}, {"a": "b"}, [{"action": "change", "new": "b", "old": [1, 2, 3], "path": ["a"]}]),
        pytest.param({"a": "c"}, {"a": None}, [{"action": "change", "new": None, "old": "c", "path": ["a"]}]),
        pytest.param({"a": "c"}, {"a": "c"}, []),
        pytest.param({"a": 1.0}, {"a": 1.01}, [{"action": "change", "new": 1.01, "old": 1.0, "path": ["a"]}]),
        pytest.param([], [1], [{"action": "insert_range", "at": 0, "path": [], "values": [1]}]),
        pytest.param([1, 2, 3], [1], [{"action": "delete_range", "end": 3, "path": [], "start": 1}]),
        pytest.param([1, 2, 3, 4], [1, 4], [{"action": "delete_range", "end": 3, "path": [], "start": 1}]),
        pytest.param(
            {"a": [1]},
            {"a": [1, 2, 3]},
            [{"action": "insert_range", "at": 1, "path": ["a"], "values": [2, 3]}],
        ),
        pytest.param(
            {"a": 1, "b": 12},
            {"a": 2, "c": 5},
            [
                {"path": ["a"], "action": "change", "old": 1, "new": 2},
                {"path": [], "action": "remove", "removed": {"b": 12}},
                {"path": [], "action": "add", "added": {"c": 5}},
            ],
        ),
        pytest.param(
            ["a", "b", "c", "y", "e", "f", "x"],
            ["b", "c", "x", "e", "f"],
            [
                {"path": [], "action": "delete_range", "start": 0, "end": 1},
                {"path": [3], "action": "change", "old": "y", "new": "x"},
                {"path": [], "action": "delete_range", "start": 6, "end": 7},
            ],
        ),
        pytest.param(
            ["a", "b", "c", {"nested": [{"value": "y", "other": 123}]}, "e", "f"],
            ["extra", "a", "b", "c", {"nested": [{"value": "x"}]}, "e", "f"],
            [
                {"path": [], "action": "insert_range", "at": 0, "values": ["extra"]},
                {"path": [3, "nested", 0, "value"], "action": "change", "old": "y", "new": "x"},
                {"path": [3, "nested", 0], "action": "remove", "removed": {"other": 123}},
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
                {"path": [], "action": "insert_range", "at": 0, "values": [{"id": -1, "name": "admin", "likes": 0}]},
                {"path": [], "action": "delete_range", "start": 2, "end": 3},
                {"path": [4, "likes"], "action": "change", "old": 153, "new": 155},
                {"path": [], "action": "insert_range", "at": 6, "values": [{"id": 6, "name": "Cindy", "likes": 1}]},
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
    ],
)
def test_diff(a: Any, b: Any, expected_diff: list[DiffItem]):
    a_initial = copy.deepcopy(a)
    b_initial = copy.deepcopy(b)

    diff_ = diff(a, b)
    assert diff_ == expected_diff

    b_patched = patch(a, diff_)
    assert b_patched == b
    assert a == a_initial
    assert b == b_initial

    patch(a, diff_, in_place=True)
    assert a == b
    assert a == b_initial
