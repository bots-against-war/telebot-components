from itertools import chain

from telebot_components.form.field import FormField, NextFieldGetter


class Form:
    def __init__(self, fields: list[FormField], start_field: FormField):
        self.fields = fields
        self.start_field = start_field

        # binding next field getters so that they can look up next form field by its name
        fields_by_name = {f.name: f for f in fields}
        for f in fields:
            if isinstance(f.next_field_getter, NextFieldGetter):
                f.next_field_getter.fields_by_name = fields_by_name

        # validating field name uniqueness
        field_names = [f.name for f in fields]
        for fn in field_names:
            if field_names.count(fn) > 1:
                raise ValueError(f"All fields must have unique names, but there is at least one duplicate: {fn}!")

        # validating that field graph is connected
        reachable_field_names = chain.from_iterable(f.next_field_getter.possible_next_field_names for f in fields)
