import copy
import functools
import operator
from collections import defaultdict
from dataclasses import dataclass
from itertools import chain, zip_longest
from types import GenericAlias
from typing import (  # type: ignore
    Any,
    Collection,
    Mapping,
    Optional,
    Type,
    Union,
    _TypedDictMeta,
    get_args,
)

from telebot_components.form.field import (
    FormField,
    FormFieldResultFormattingOpts,
    NextFieldGetter,
)
from telebot_components.form.types import FormBranchCondition
from telebot_components.language import any_text_to_str
from telebot_components.stores.language import MaybeLanguage
from telebot_components.utils import telegram_html_escape

FieldName = Optional[str]


@dataclass
class FormBranch:
    members: list[Union[FormField, "FormBranch"]]
    condition: FormBranchCondition

    @property
    def start_field_name(self) -> str:
        if not isinstance(self.members[0], FormField):
            raise ValueError(f"First segment member must be a field, found {self.members[0]}")
        return self.members[0].name


class Form:
    """Container for collection of fields linked together via get_next_field_getter() attribute. Does not modify passed
    objects, creates private copies.

    If allow_cyclic param is False (default), performs topological sort to validate form acyclicity and can print
    it's graph structure in ASCII with print_graph method.
    """

    def __init__(
        self, fields: Collection[FormField], start_field: Optional[FormField] = None, allow_cyclic: bool = False
    ):
        if not fields:
            raise ValueError("Fields list can't be empty")

        self.allow_cyclic = allow_cyclic

        # copy fields to avoid modifying user's objects; this allows safely reusing one field in multiple forms
        self.fields = [copy.deepcopy(f) for f in fields]
        if start_field is None:
            start_field = next(iter(fields))
        self.start_field = copy.deepcopy(start_field)

        # if any field has the next field getter omitted, use default sequential connection
        for field, next_field in zip_longest(self.fields, self.fields[1:]):
            if field.next_field_getter is None:
                if isinstance(next_field, FormField):
                    field.next_field_getter = NextFieldGetter.by_name(next_field.name)
                else:
                    field.next_field_getter = NextFieldGetter.form_end()

        # validate field name uniqueness
        field_names = [f.name for f in self.fields]
        for fn in field_names:
            if field_names.count(fn) > 1:
                raise ValueError(f"All fields must have unique names, but there is at least one duplicate: {fn}!")

        # bind next field getters so that they can look up next form field by its name
        self.fields_by_name = {f.name: f for f in self.fields}
        for f in self.fields:
            f.get_next_field_getter().fields_by_name = self.fields_by_name

        # validate that field graph is connected and that the start field has no incoming edges
        reachable_field_names = set(
            chain.from_iterable(f.get_next_field_getter().possible_next_field_names for f in self.fields)
        )
        if not self.allow_cyclic and self.start_field.name in reachable_field_names:
            raise ValueError(
                f"Form configuration error: start field '{self.start_field.name}' is reachable from other field(s)"
            )
        reachable_field_names.add(self.start_field.name)
        if None not in reachable_field_names:
            raise ValueError("Endless form: no field has None (form end) as a possible next field")
        reachable_field_names.remove(None)
        field_names_set = set(field_names)
        if reachable_field_names != field_names_set:
            unreachable_field_names = field_names_set.difference(reachable_field_names)
            if unreachable_field_names:
                raise ValueError(
                    "Disconnected form graph, there are unreachable fields: " + ", ".join(unreachable_field_names)
                )
            unknown_reachable_field_names = reachable_field_names.difference(field_names)
            if unknown_reachable_field_names:
                raise ValueError(
                    "Form configuration error: some fields list non-existing fields as reachable:"
                    + ", ".join([str(n) for n in unknown_reachable_field_names])
                )

        # pre-calculating some generic graph-related stuff
        self.next_field_names: dict[FieldName, set[FieldName]] = {
            f.name: set(f.get_next_field_getter().possible_next_field_names) for f in self.fields
        }
        self_cycling_fields = [fname for fname, next_fnames in self.next_field_names.items() if fname in next_fnames]
        if not self.allow_cyclic and self_cycling_fields:
            raise ValueError(
                "Field(s) are referencing themself as possible "
                + f"next field: {self_cycling_fields} ({self.next_field_names = })"
            )
        self.prev_field_names: dict[FieldName, set[FieldName]] = defaultdict(set)
        for field_name, nexts in self.next_field_names.items():
            for next_ in nexts:
                self.prev_field_names[next_].add(field_name)

        # calculating global requiredness for fields
        if not self.allow_cyclic:

            def paths_from(from_field: FieldName) -> list[list[FieldName]]:
                if from_field is None:
                    return [[]]
                paths: list[list[FieldName]] = []
                for step in self.next_field_names[from_field]:
                    paths.extend([[from_field, *subpath] for subpath in paths_from(step)])
                return paths

            path_fields = [set(p) for p in paths_from(self.start_field.name)]
            self.globally_required_fields: Optional[set[FieldName]] = functools.reduce(operator.and_, path_fields)
        else:
            self.globally_required_fields = None

        # topological sort to validate acyclicity + for nice rendering
        if not self.allow_cyclic:
            next_field_names = copy.deepcopy(self.next_field_names)
            prev_field_names = copy.deepcopy(self.prev_field_names)
            topologically_sorted: list[str] = []
            # NOTE: this is semantically set, but we use list for predictability
            vertices_without_incoming_edges: list[Optional[str]] = [self.start_field.name]
            while vertices_without_incoming_edges:
                vertices_without_incoming_edges.sort()
                from_ = vertices_without_incoming_edges.pop(0)
                if from_ is not None:
                    topologically_sorted.append(from_)
                tos = self.next_field_names.get(from_)
                if tos is None:
                    continue
                tos = tos.copy()
                for to in tos:
                    next_field_names[from_].remove(to)
                    prev_field_names[to].remove(from_)
                    if not prev_field_names[to]:
                        vertices_without_incoming_edges.append(to)

            if any(next_field_names.values()):
                raise ValueError("Form graph has at least one cycle")
            self.topologically_sorted_field_names: Optional[list[str]] = topologically_sorted
        else:
            self.topologically_sorted_field_names = None

    @classmethod
    def _flatten_segment_members(
        cls,
        members: list[Union[FormField, FormBranch]],
        after_segment: FieldName,
    ) -> list[FormField]:
        members = members.copy()  # no need for deepcopy since the function is itself recursive
        first_field = members[0]
        if not isinstance(first_field, FormField):
            raise ValueError(f"First member of a segmented form must be a field, found {first_field}")

        fields: list[FormField] = [first_field]
        current_branches: list[FormBranch] = []
        members_with_padding: list[Union[FormField, FormBranch, None]] = [*members[1:], None]
        for member in members_with_padding:
            if isinstance(member, FormBranch):
                current_branches.append(member)
                continue
            next_field_name = member.name if member is not None else after_segment
            if current_branches:
                # done parsing current branches, time to process them
                fields[-1].next_field_getter = NextFieldGetter.from_condition_list(
                    [(s.start_field_name, s.condition) for s in current_branches],
                    fallback=next_field_name,
                )
                for segment in current_branches:
                    fields.extend(cls._flatten_segment_members(segment.members, after_segment=next_field_name))
                current_branches.clear()
            else:
                # regular sequential fields
                fields[-1].next_field_getter = NextFieldGetter.by_name(next_field_name)
            if member is not None:
                fields.append(member)

        return fields

    @classmethod
    def branching(cls, top_level_members: list[Union[FormField, "FormBranch"]]) -> "Form":
        """
        Branching form is a simplified way to configure form, using a generally linear flow with braches entered
        with condition on previous field's value. After branch is completed, the form "merges" back and proceeds
        sequentially.
        """
        return Form(
            fields=cls._flatten_segment_members(
                top_level_members,
                after_segment=None,  # end form
            ),
            start_field=None,
            allow_cyclic=False,
        )

    @staticmethod
    def _field_value_type(field: FormField) -> Any:
        custom_value_type = field.custom_value_type()
        if custom_value_type is not None:
            return custom_value_type
        else:
            for base_type in type(field).__orig_bases__:  # type: ignore
                generic_type_args = get_args(base_type)
                if generic_type_args:
                    return generic_type_args[0]
            else:
                raise TypeError(
                    f"Unable to infer field value type for {field}; "
                    + "consider overriding custom_value_type() method for it"
                )

    @staticmethod
    def _field_value_type_to_string(fvt: Any) -> str:
        if isinstance(fvt, GenericAlias):
            return repr(fvt)  # generic aliases are nicely formatted for printing and include type args
        else:
            try:
                return fvt.__name__
            except Exception:
                return str(fvt)

    def validate_result_type(self, typed_dict_type: Type[Mapping]):
        if not isinstance(typed_dict_type, _TypedDictMeta):
            raise TypeError(f"TypedDict instance/subclass expected, found {typed_dict_type!r}")
        typed_dict_annotations = typed_dict_type.__annotations__
        for field in self.fields:
            if field.name not in typed_dict_annotations:
                raise TypeError(f"Invalid result type: missing required key {field.name!r}")
            expected_value_type = self._field_value_type(field)
            actual_value_type = typed_dict_annotations[field.name]
            if not field.required:
                expected_value_type = Optional[expected_value_type]  # type: ignore
            if actual_value_type != expected_value_type:
                raise TypeError(
                    f"Invalid result type: {field.name!r} must be typed "
                    + f"as {self._field_value_type_to_string(expected_value_type)}, not "
                    + f"{self._field_value_type_to_string(actual_value_type)}"
                    + (" (Optional reflects the fact that the field is not required)" if not field.required else "")
                )

    def generate_result_type(self) -> str:
        indent = " " * 4
        lines = ["class MyFormResultT(TypedDict):", indent + '"""Generated by Form.generate_result_type() method"""']
        fields = self.fields.copy()
        fields.sort(key=lambda ff: ff.name)  # first, alphabetical sort
        if self.topologically_sorted_field_names is not None:
            # then, if possible, topological
            fields.sort(key=lambda ff: self.topologically_sorted_field_names.index(ff.name))  # type: ignore
        for field in fields:
            field_type_str = self._field_value_type_to_string(self._field_value_type(field))
            if not field.required:
                field_type_str = f"Optional[{field_type_str}]"
            if self.globally_required_fields is not None and field.name not in self.globally_required_fields:
                field_type_str = f"NotRequired[{field_type_str}]"
            lines.append(indent + f"{field.name}: {field_type_str}")
        return "\n".join(lines)

    def result_to_html(
        self,
        result: Mapping[str, Any],
        lang: MaybeLanguage,
        omitted_fields_count_template: Optional[str] = "<i>+{} omitted</i>",
    ) -> str:
        formatting_opts: dict[str, FormFieldResultFormattingOpts] = dict()
        for f in self.fields:
            if isinstance(f.result_formatting_opts, FormFieldResultFormattingOpts):
                formatting_opts[f.name] = f.result_formatting_opts
            elif f.result_formatting_opts:
                formatting_opts[f.name] = FormFieldResultFormattingOpts(descr=f.query_message, is_multiline=False)

        blocks: list[str] = []
        omitted_field_count = 0
        field_names = self.topologically_sorted_field_names or sorted([f.name for f in self.fields])
        for field_name in field_names:
            field = self.fields_by_name[field_name]

            if field_name not in result:
                if self.globally_required_fields is not None and field_name in self.globally_required_fields:
                    raise ValueError(f"Globally required field {field_name!r} not found in result")
                else:
                    continue

            opts = formatting_opts.get(field_name)
            if opts is None:
                omitted_field_count += 1
                continue

            field_descr_str: str
            if isinstance(opts.descr, str):
                field_descr_str = opts.descr
            else:
                try:
                    field_descr_str = any_text_to_str(opts.descr, lang)
                except Exception:
                    field_descr_str = any_text_to_str(
                        opts.descr,
                        next(iter(opts.descr.keys())),  # type: ignore
                    )

            field_value = result[field_name]
            field_value_formatter = opts.value_formatter or field.value_to_str
            if field_value is not None:
                blocks.append(
                    format_named_value(
                        field_descr_str,
                        field_value_formatter(field_value, lang),
                        single_line=not opts.is_multiline,
                    )
                )

        if omitted_field_count and omitted_fields_count_template is not None:
            blocks.append(omitted_fields_count_template.format(omitted_field_count))
        return "\n".join(blocks)

    def result_to_export(self, result: Mapping[str, Any]) -> dict[Any, Any]:
        exported: dict[Any, Any] = dict()
        for name, value in result.items():
            field = self.fields_by_name[name]
            if field.export_opts is None:
                continue
            if field.export_opts.value_mapping is not None:
                value_for_export = field.export_opts.value_mapping[value]
            elif field.export_opts.value_processor is not None:
                value_for_export = field.export_opts.value_processor(value)
            else:
                value_for_export = value
            exported[field.export_opts.column] = value_for_export
        return exported

    # VVVVVV messy shitty terminal visualization code VVVVVV

    def format_graph(self) -> str:
        if self.topologically_sorted_field_names is None:
            raise ValueError("print_graph method only available for acyclic form graphs")
        DEFAULT_FORM_END_PRINT = "END"
        form_end_print_name = DEFAULT_FORM_END_PRINT
        idx = 0
        while form_end_print_name in self.topologically_sorted_field_names:
            idx += 1
            form_end_print_name = f"{DEFAULT_FORM_END_PRINT} ({idx})"

        def to_print(name: FieldName) -> str:
            if isinstance(name, str):
                return name
            else:
                return form_end_print_name

        vertex_names = self.topologically_sorted_field_names + [form_end_print_name]
        max_vertex_name_len = max([len(v) for v in vertex_names]) + 2

        edges: set[tuple[str, str]] = set()
        for f in self.fields:
            for next_field_name in f.get_next_field_getter().possible_next_field_names:
                edges.add((to_print(f.name), to_print(next_field_name)))

        is_vert_connected_to_next: list[bool] = []
        for name_curr, name_next in zip(vertex_names[:-1], vertex_names[1:]):
            is_vert_connected_to_next.append((name_curr, name_next) in edges)
            edges.discard((name_curr, name_next))
        is_vert_connected_to_next.append(False)

        n_arcs_out: dict[str, int] = defaultdict(int)
        n_arcs_in: dict[str, int] = defaultdict(int)
        edge_arcs: list[EdgeArc] = []
        for from_, to in edges:
            n_arcs_out[from_] += 1
            n_arcs_in[to] += 1
            length = vertex_names.index(to) - vertex_names.index(from_)
            edge_arcs.append(EdgeArc(from_, to, length))
        edge_arcs.sort(key=lambda arc: arc.length)

        canvas = CharCanvas()

        BETWEEN_VERTEX_BLOCKS = 2
        i_box_center: dict[str, int] = dict()
        for idx, name in enumerate(vertex_names):
            vertex_box = VertexBox(name, max_vertex_name_len, n_arcs_in=n_arcs_in[name], n_arcs_out=n_arcs_out[name])
            subcanvas = vertex_box.canvas()
            canvas.join_down(subcanvas, 0, front=True)
            i_box_center[name] = canvas.height - vertex_box.n_arcs_out - 2
            if idx + 1 < len(vertex_names):
                spacer = (
                    get_sequential_vertices_arrow_canvas(BETWEEN_VERTEX_BLOCKS, max_vertex_name_len + 2)
                    if is_vert_connected_to_next[idx]
                    else get_spacer_canvas(BETWEEN_VERTEX_BLOCKS)
                )
                canvas.join_down(spacer, 0)

        for arc_idx, arc in enumerate(edge_arcs):
            arc_start_i = i_box_center[arc.from_] + n_arcs_out[arc.from_]
            arc_end_i = i_box_center[arc.to] - n_arcs_in[arc.to]
            n_arcs_out[arc.from_] -= 1
            n_arcs_in[arc.to] -= 1
            arc_canvas = CharCanvas()
            arc_margin = 1 + 2 * arc_idx
            arc_canvas.insert_string(0, 0, ("─" * arc_margin) + "┐")
            arc_canvas.insert_string(1, arc_margin, "|" * (arc_end_i - arc_start_i - 1), vertical=True)
            arc_canvas.insert_string(arc_end_i - arc_start_i, 0, ("─" * arc_margin) + "┘")
            canvas.overlay(arc_canvas, arc_start_i, max_vertex_name_len + 3, front=False)

        return canvas.format()

    def print_graph(self) -> None:
        print(self.format_graph())


@dataclass
class VertexBox:
    name: str
    name_print_len: int
    n_arcs_out: int
    n_arcs_in: int

    def center_j(self) -> int:
        return 1 + self.n_arcs_in

    def canvas(self) -> "CharCanvas":
        canvas = CharCanvas()
        row_idx = 0
        canvas.insert_string(row_idx, 0, "┌" + "─" * self.name_print_len + "┐ ")
        for _ in range(self.n_arcs_in):
            row_idx += 1
            canvas.insert_string(row_idx, 0, "│" + " " * self.name_print_len + "│<")
        row_idx += 1
        canvas.insert_string(row_idx, 0, "│" + pad_to_len(self.name, self.name_print_len) + "│ ")
        for _ in range(self.n_arcs_out):
            row_idx += 1
            canvas.insert_string(row_idx, 0, "│" + " " * self.name_print_len + "│─")
        row_idx += 1
        canvas.insert_string(row_idx, 0, "└" + "─" * self.name_print_len + "┘ ")
        return canvas


@dataclass
class EdgeArc:
    from_: str
    to: str
    length: int  # in vertices according to topological sort, not in chars


def get_spacer_canvas(height: int) -> "CharCanvas":
    c = CharCanvas()
    c.insert_char(height - 1, 0, " ")
    return c


def get_sequential_vertices_arrow_canvas(height: int, width: int) -> "CharCanvas":
    c = CharCanvas()
    c.insert_string(0, width // 2, "|" * (height - 1), vertical=True)
    c.insert_char(height - 1, width // 2, "V")
    return c


class CharCanvas:
    def __init__(self) -> None:
        self._array: list[list[str]] = []

    @property
    def width(self) -> int:
        if self._array:
            return len(self._array[0])
        else:
            return 0

    @property
    def height(self) -> int:
        return len(self._array)

    def _grow_row(self):
        self._array.append([" "] * self.width)

    def _grow_col(self):
        for row in self._array:
            row.append(" ")

    def insert_string(self, i: int, j: int, string: str, front: bool = True, vertical: bool = False):
        for idx, char in enumerate(string):
            self.insert_char(i=i + (0 if not vertical else idx), j=j + (0 if vertical else idx), char=char, front=front)

    def insert_char(self, i: int, j: int, char: str, front: bool = True):
        assert len(char) == 1, "only single characters can be inserted into CharArray"
        height = self.height
        for _ in range(i - height + 1):
            self._grow_row()
        width = self.width
        for _ in range(j - width + 1):
            self._grow_col()
        if not front and self._array[i][j] != " ":
            return
        if char != " ":
            self._array[i][j] = char

    def overlay(self, other: "CharCanvas", i_top: int, j_left: int, front: bool = True):
        for i_local, row in enumerate(other._array):
            for j_local, char in enumerate(row):
                self.insert_char(i_top + i_local, j_left + j_local, char, front)

    def join_down(self, other: "CharCanvas", j_left: int, front: bool = True):
        self.overlay(other, self.height, j_left, front)

    def format(self):
        return "\n".join("".join(row) for row in self._array)

    def print(self):
        print(self.format())


def pad_to_len(string: str, length: int) -> str:
    add_to_right = True
    while len(string) < length:
        if add_to_right:
            string = string + " "
        else:
            string = " " + string
        add_to_right = not add_to_right
    return string


def format_named_value(name: str, value: str, single_line: bool = True) -> str:
    sep = ": " if single_line else "\n"
    result = f"<b>{telegram_html_escape(name)}</b>{sep}{telegram_html_escape(value)}"
    if not single_line:
        result += "\n"
    return result
