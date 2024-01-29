"""
Diff and patch operations for list- and dict-like structures.

Heaviliy based on dictdiffer (https://github.com/inveniosoftware/dictdiffer) package,
with the main changes:
- reduced functionality we don't need
- different diff format with TypedDict-based typing
- better list diff support with difflib.SequenceMatcher
  (as proposed in https://github.com/inveniosoftware/dictdiffer/issues/100)
"""


import copy
import dataclasses
import difflib
import math
import sys
from collections import defaultdict
from collections.abc import Generator, MutableMapping, MutableSequence
from dataclasses import dataclass
from typing import Any, Hashable, Iterable, Literal, TypedDict, TypeVar

from diff_match_patch import diff_match_patch  # type: ignore

NUMERIC_TYPES = int, float

Diffable = MutableMapping | MutableSequence | str | float
Key = str
Index = int
Path = list[Key | Index]
KeyOrIndex = TypeVar("KeyOrIndex", bound=Key | Index)


def hashable(path: Path) -> Hashable:
    return tuple(path)


def are_different(first: Any, second: Any, tolerance: float, absolute_tolerance: float | None = None):
    if isinstance(first, NUMERIC_TYPES) and isinstance(second, NUMERIC_TYPES):
        return not math.isclose(
            first,
            second,
            rel_tol=tolerance or 0,
            abs_tol=absolute_tolerance or 0,
        )
    else:
        return first != second


@dataclass
class HashableWrapper:
    data: Any

    def _guaranteed_hash(self, obj: Any) -> int:
        try:
            return hash(obj)
        except TypeError:
            try:
                if isinstance(obj, (list, tuple)):
                    return hash(tuple(map(self._guaranteed_hash, obj)))
                elif isinstance(obj, set):
                    return hash(tuple(map(self._guaranteed_hash, sorted(obj))))
                elif isinstance(obj, dict):
                    return hash(tuple(map(self._guaranteed_hash, sorted(obj.items()))))
            except Exception:
                pass
            # fallback to id to make anything hashable
            return id(obj)

    def __hash__(self) -> int:
        return self._guaranteed_hash(self.data)


class _DiffAction(TypedDict):
    path: Path


class SetPathAction(_DiffAction):
    action: Literal["change"]
    new: Any


class PatchStringAction(_DiffAction):
    action: Literal["patch_string"]
    delta: str


class AddKeysAction(_DiffAction):
    action: Literal["add"]
    items: dict[str, Any]


class RemoveKeysAction(_DiffAction):
    action: Literal["remove"]
    keys: list[str]


class AddRangeAction(_DiffAction):
    action: Literal["add_range"]
    start: int
    values: list[Any]


class RemoveRangeAction(_DiffAction):
    action: Literal["remove_range"]
    start: int
    length: int


DiffAction = SetPathAction | PatchStringAction | AddKeysAction | RemoveKeysAction | AddRangeAction | RemoveRangeAction


ItemT = TypeVar("ItemT")


def copy_list(iterable: Iterable[ItemT]) -> list[ItemT]:
    return [copy.deepcopy(el) for el in iterable]


# google's diff-match-patch wrapper functions


_dmp = diff_match_patch()


def diff_text(first: str, second: str) -> str:
    str_diff = _dmp.diff_main(first, second)
    _dmp.diff_cleanupEfficiency(str_diff)
    return _dmp.diff_toDelta(str_diff)


def patch_text(first: str, text_diff: str) -> str:
    dmp_diff = _dmp.diff_fromDelta(first, text_diff)
    patches = _dmp.patch_make(first, dmp_diff)
    return _dmp.patch_apply(patches, first)[0]


def diff_gen(
    first: Diffable,
    second: Diffable,
    ignore: list[Path] | None = None,
    float_tol: float = sys.float_info.epsilon,
    diff_strings: bool = True,
) -> Generator[DiffAction, None, None]:
    def _recurse(first: Diffable, second: Diffable, _path: Path | None = None):
        path = _path.copy() if _path else []
        if isinstance(first, MutableMapping) and isinstance(second, MutableMapping):

            def is_ignored(key: str):
                return ignore is not None and (path + [key] not in ignore)

            removed: dict[str, Any] = {}
            for first_key, first_value in first.items():
                if is_ignored(first_key):
                    continue
                if first_key in second:
                    yield from _recurse(first_value, second[first_key], _path=path + [first_key])
                else:
                    removed[first_key] = copy.deepcopy(first_value)
            if removed:
                yield RemoveKeysAction(
                    path=path,
                    action="remove",
                    keys=list(removed.keys()),
                )

            added: dict[str, Any] = {}
            for second_key, second_value in second.items():
                if is_ignored(second_key):
                    continue
                if second_key not in first:
                    added[second_key] = copy.deepcopy(second_value)
            if added:
                yield AddKeysAction(
                    path=path,
                    action="add",
                    items=added,
                )
        elif isinstance(first, MutableSequence) and isinstance(second, MutableSequence):
            matcher = difflib.SequenceMatcher(
                isjunk=None,
                a=[HashableWrapper(e) for e in first],
                b=[HashableWrapper(e) for e in second],
            )
            for opcode, i1, i2, j1, j2 in matcher.get_opcodes():
                match opcode:
                    case "replace":
                        # if we have "several for several" replacement for small range lengths it's
                        # reasonable to assume they were modified one-by-one. but for larger lengths
                        # we fall back to remove-then-delete scheme
                        overlap_len = min(5, min(i2 - i1, j2 - j1))
                        for delta in range(overlap_len):
                            yield from _recurse(first[i1 + delta], second[j1 + delta], path + [i1 + delta])

                        if i2 - i1 > overlap_len:
                            yield RemoveRangeAction(
                                path=path,
                                action="remove_range",
                                start=i1 + overlap_len,
                                length=i2 - (i1 + overlap_len),
                            )
                        if j2 - j1 > overlap_len:
                            yield AddRangeAction(
                                path=path,
                                action="add_range",
                                start=i2,
                                values=copy_list(second[j1 + overlap_len : j2]),
                            )
                    case "delete":
                        yield RemoveRangeAction(
                            path=path,
                            action="remove_range",
                            start=i1,
                            length=i2 - i1,
                        )
                    case "insert":
                        yield AddRangeAction(
                            path=path,
                            action="add_range",
                            start=i1,
                            values=copy_list(second[j1:j2]),
                        )
        elif isinstance(first, str) and isinstance(second, str) and len(first) > 32 and len(second) > 32:
            yield PatchStringAction(
                path=path,
                action="patch_string",
                delta=diff_text(first, second),
            )
        elif are_different(first, second, float_tol):
            yield SetPathAction(
                path=path,
                action="change",
                new=copy.deepcopy(second),
            )

    yield from _recurse(first, second)


def diff(
    first: Diffable,
    second: Diffable,
    ignore: list[Path] | None = None,
    float_tol: float = sys.float_info.epsilon,
) -> list[DiffAction]:
    return list(diff_gen(first, second, ignore, float_tol))


@dataclasses.dataclass
class InplacePatchImpossible(Exception):
    patched_value: Diffable


def patch(destination: Diffable, diff: Iterable[DiffAction], in_place: bool = False) -> Diffable:
    if not in_place:
        destination = copy.deepcopy(destination)

    list_idx_offset: dict[Hashable, int] = defaultdict(int)

    def _apply_offset(key: KeyOrIndex, path: Path) -> KeyOrIndex:
        if isinstance(key, str):
            return key  # type: ignore
        else:
            offset = list_idx_offset.get(hashable(path), 0)
            return offset + key  # type: ignore

    def _access_path(source: Diffable, path: Path) -> Any:
        value: Any = source
        path_current: Path = []
        for key in path:
            if isinstance(value, list):
                value = value[_apply_offset(int(key), path_current)]
            else:
                value = value[key]
            path_current.append(key)
        return value

    for item in diff:
        path = item["path"]
        if item["action"] == "change":
            patched_value = item["new"]
            if len(path) == 0:
                if in_place:
                    raise InplacePatchImpossible(patched_value=patched_value)
                else:
                    return patched_value
            *container_path, key = path
            container = _access_path(destination, container_path)
            container[_apply_offset(key, container_path)] = patched_value
        elif item["action"] == "patch_string":
            if len(path) == 0:
                if not isinstance(destination, str):
                    raise ValueError("Top-level patch string on non-string destination")
                patched_value = patch_text(destination, item["delta"])
                if in_place:
                    raise InplacePatchImpossible(patched_value=patched_value)
                else:
                    return patched_value
            *container_path, key = path
            container = _access_path(destination, container_path)
            key = _apply_offset(key, container_path)
            container[key] = patch_text(container[key], item["delta"])
        elif item["action"] == "add":
            container = _access_path(destination, path)
            assert isinstance(container, dict)
            for key, value in item["items"].items():
                container[key] = value
        elif item["action"] == "remove":
            container = _access_path(destination, path)
            assert isinstance(container, dict)
            for key in item["keys"]:
                del container[key]
        else:
            path_dotted = hashable(path)
            offset = list_idx_offset.get(path_dotted, 0)
            container = _access_path(destination, path)
            assert isinstance(container, list), f'{item["action"]!r} can only be applied to lists'
            if item["action"] == "remove_range":
                end = item["start"] + item["length"]
                del container[offset + item["start"] : offset + end]
                list_idx_offset[path_dotted] -= end - item["start"]
            elif item["action"] == "add_range":
                for i, el in enumerate(item["values"]):
                    container.insert(offset + item["start"] + i, el)
                list_idx_offset[path_dotted] += len(item["values"])

    return destination


if __name__ == "__main__":

    def print_diff(a, b):
        print("\n======")
        print("a", a, sep="\n")
        print("b", b, sep="\n")
        diff_ = diff(a, b)
        print("\ndiff")
        print(*diff_, sep="\n")
        print()
        b_rec = patch(a, diff_)
        print("b patched", b_rec, sep="\n")
        print("OK" if b_rec == b else "ERROR")

    print_diff(
        a={"a": 1, "b": 12},
        b={"a": 2, "c": 5},
    )

    print_diff(
        a=["a", "b", "c", "y", "e", "f", "x"],
        b=["b", "c", "x", "e", "f"],
    )

    print_diff(
        a=["a", "b", "c", {"nested": [{"value": "y", "other": 123}]}, "e", "f"],
        b=["extra", "a", "b", "c", {"nested": [{"value": "x"}]}, "e", "f"],
    )

    print_diff(
        a=[
            {"id": 0, "name": "Alice", "likes": 100},
            {"id": 1, "name": "Bob", "likes": 10},
            {"id": 2, "name": "Clare", "likes": 15},
            {"id": 3, "name": "Peter", "likes": 0},
            {"id": 4, "name": "Mary", "likes": 153},
            {"id": 5, "name": "Carl", "likes": 5},
        ],
        b=[
            {"id": -1, "name": "admin", "likes": 0},
            {"id": 0, "name": "Alice", "likes": 100},
            {"id": 1, "name": "Bob", "likes": 10},
            {"id": 3, "name": "Peter", "likes": 0},
            {"id": 4, "name": "Mary", "likes": 155},
            {"id": 5, "name": "Carl", "likes": 5},
            {"id": 6, "name": "Cindy", "likes": 1},
        ],
    )

    print_diff(
        a=(
            "The patch algorithms generate (using patch_toText()) and parse "
            + "(using patch_fromText()) a textual diff format which looks a lot like the Unidiff format."
        ),
        b=(
            "The patch algorithms generate and parse an awesome textual diff "
            + "format which looks a lot like the Unidiff format!!!"
        ),
    )
