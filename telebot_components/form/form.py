import copy
from collections import defaultdict
from dataclasses import dataclass
from itertools import chain
from typing import Optional

from telebot_components.form.field import FormField, NextFieldGetter

FieldNameT = Optional[str]


class Form:
    """Container for collection of fields linked together via next_field_getter attribute. Does not modify passed
    objects, creates private copies.

    If allow_cyclic param is False (default) performs topological sort to validate form acyclicity and can print
    it's graph structure in ASCII with print_graph method.
    """

    def __init__(self, fields: list[FormField], start_field: FormField, allow_cyclic: bool = False):
        self.fields = [copy.deepcopy(f) for f in fields]  # copying fields to avoid modifying user's objects
        self.start_field = copy.deepcopy(start_field)

        # validating field name uniqueness
        field_names = [f.name for f in self.fields]
        for fn in field_names:
            if field_names.count(fn) > 1:
                raise ValueError(f"All fields must have unique names, but there is at least one duplicate: {fn}!")

        # binding next field getters so that they can look up next form field by its name
        fields_by_name = {f.name: f for f in self.fields}
        for f in self.fields:
            if isinstance(f.next_field_getter, NextFieldGetter):
                f.next_field_getter.fields_by_name = fields_by_name

        # validating that field graph is connected and that the start field has no incoming edges
        reachable_field_names = set(
            chain.from_iterable(f.next_field_getter.possible_next_field_names for f in self.fields)
        )
        if not allow_cyclic and self.start_field.name in reachable_field_names:
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

        # topological sort to validate acyclicity + for nice rendering
        if not allow_cyclic:
            next_field_names: dict[FieldNameT, set[FieldNameT]] = {
                f.name: set(f.next_field_getter.possible_next_field_names) for f in self.fields
            }
            prev_field_names: dict[FieldNameT, set[FieldNameT]] = defaultdict(set)
            for field_name, nexts in next_field_names.items():
                for next in nexts:
                    prev_field_names[next].add(field_name)

            topologically_sorted: list[Optional[str]] = []
            vertices_without_incoming_edges: set[Optional[str]] = {self.start_field.name}
            while vertices_without_incoming_edges:
                from_ = vertices_without_incoming_edges.pop()
                topologically_sorted.append(from_)
                tos = next_field_names.get(from_)
                if tos is None:
                    continue
                tos = tos.copy()
                for to in tos:
                    next_field_names[from_].remove(to)
                    prev_field_names[to].remove(from_)
                    if not prev_field_names[to]:
                        vertices_without_incoming_edges.add(to)

            if any(next_field_names.values()):
                raise ValueError("Form graph has at least one cycle")
            self.topologically_sorted_field_names: Optional[list[FieldNameT]] = topologically_sorted
        else:
            self.topologically_sorted_field_names = None

    def print_graph(self):
        if self.topologically_sorted_field_names is None:
            raise ValueError("print_graph method only available for acyclic form graphs")
        DEFAULT_FORM_END_PRINT = "END"
        form_end_print_name = DEFAULT_FORM_END_PRINT
        idx = 0
        while form_end_print_name in self.topologically_sorted_field_names:
            idx += 1
            form_end_print_name = f"{DEFAULT_FORM_END_PRINT} ({idx})"

        def to_print(name: FieldNameT) -> str:
            if isinstance(name, str):
                return name
            else:
                return form_end_print_name

        vertex_names = [to_print(fn) for fn in self.topologically_sorted_field_names]
        max_vertex_name_len = max([len(v) for v in vertex_names]) + 2

        edges: set[tuple[str, str]] = set()
        for f in self.fields:
            for next_field_name in f.next_field_getter.possible_next_field_names:
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

        canvas.print()


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
    def __init__(self):
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

    def print(self):
        for row in self._array:
            print("".join(row))


def pad_to_len(string: str, length: int) -> str:
    add_to_right = True
    while len(string) < length:
        if add_to_right:
            string = string + " "
        else:
            string = " " + string
        add_to_right = not add_to_right
    return string
