from typing import Any, Callable, Mapping, TypeVar, Union

FormFieldValueId = str  # see FormField.value_id method
PredicateBranchCondition = Callable[[FormFieldValueId], bool]
ValueMatchBranchCondition = FormFieldValueId
FormBranchCondition = Union[PredicateBranchCondition, ValueMatchBranchCondition]
FormResultT = TypeVar("FormResultT", bound=Mapping[str, Any])
FormDynamicDataT = TypeVar("FormDynamicDataT")
