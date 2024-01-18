from dataclasses import dataclass
from enum import Enum
from typing import Generic, Type, TypeVar

ValueT = TypeVar("ValueT")


@dataclass
class EnumOption(Generic[ValueT]):
    name: str
    value: ValueT


def create_enum_class(class_id: str, options: list[EnumOption]) -> Type[Enum]:
    """
    Programmatically create Enum class from a set of options and inject it into module's scope.

    Warning! Users are responsible for providing a consistent and unique class id. Failure to do so will lead to
    deserialization error when loading Form state.
    """
    # see https://docs.python.org/3/howto/enum.html#functional-api

    enum_def = [(o.name, o.value) for o in options]
    enum_class_name = f"{class_id}_dynamic_enum"
    res: Type[Enum] = Enum(enum_class_name, enum_def, module=__name__)  # type: ignore

    # but also, to use this in Form's enum-defined fields, we need to inject this class into
    # global module's scope so that (de)serializers can find the class declaration and use it
    globals()[enum_class_name] = res

    return res
