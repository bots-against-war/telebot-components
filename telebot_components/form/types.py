from typing import Callable, Union

FormFieldValueId = str  # see FormField.value_id method


PredicateFormSegemtnCondition = Callable[[FormFieldValueId], bool]
ValueMatchFormSegmentCondition = FormFieldValueId
FormSegmentCondition = Union[PredicateFormSegemtnCondition, ValueMatchFormSegmentCondition]
