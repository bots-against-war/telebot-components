from typing import Callable, Union

FormFieldValueId = str  # see FormField.value_id method


PredicateBranchCondition = Callable[[FormFieldValueId], bool]
ValueMatchBranchCondition = FormFieldValueId
FormBranchCondition = Union[PredicateBranchCondition, ValueMatchBranchCondition]
