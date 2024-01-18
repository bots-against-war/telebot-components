import collections
from dataclasses import dataclass
from enum import Enum
from typing import Generic, Type, TypeVar

ValueT = TypeVar("ValueT")


@dataclass
class EnumOption(Generic[ValueT]):
    name: str
    value: ValueT


def create_dynamic_enum_class(class_id: str, options: list[EnumOption]) -> Type[Enum]:
    """
    Programmatically create Enum class from a set of options and inject it into module's scope.

    Warning! Users are responsible for providing a consistent and unique class id. Failure to do so will lead to
    deserialization error when loading Form state.
    """
    options_by_name = {o.name: o for o in options}
    if len(options_by_name) != len(options):
        dumplicate_names = [name for name, count in collections.Counter(o.name for o in options).items() if count > 1]
        raise ValueError(f"Non-unique option names: {dumplicate_names}")

    enum_class_name = f"{class_id}_dynamic_enum"
    if existing_enum := globals().get(enum_class_name):
        # attempting to validate and reuse existing enum
        try:
            assert len(existing_enum) == len(options)
            for existing_option in existing_enum:
                option = options_by_name[existing_option.name]
                assert option.value == existing_option.value
        except Exception:
            raise ValueError(
                f"Enum {enum_class_name!r} already exists and contains options conflicting with the ones provided"
            )
        return existing_enum  # type: ignore

    # see https://docs.python.org/3/howto/enum.html#functional-api
    enum_def = [(o.name, o.value) for o in options]
    res: Type[Enum] = Enum(enum_class_name, enum_def, module=__name__)  # type: ignore

    # but also, to use this in Form's enum-defined fields, we need to inject this class into
    # global module's scope so that (de)serializers can find the class declaration and use it
    globals()[enum_class_name] = res

    return res
