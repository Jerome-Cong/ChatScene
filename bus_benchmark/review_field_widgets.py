"""Schema-aware ipywidgets fields for query-oracle human review.

The classes in this module deliberately contain no notebook or persistence
logic.  They edit semantic values only; source identifiers, provenance and
human-gold lifecycle state remain the responsibility of the production review
workflow.
"""

import copy
import html
import json
import math
from decimal import Decimal
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from .atoms import make_atom
from .constants import ATOM_CATEGORIES, REQUIREMENT_LAYERS
from .cpd import QUERY_BLIND_TARGET_SELECTORS, query_blind_target_selector
from .errors import ValidationError
from .jsonio import canonical_json_bytes, strict_json_value_bytes


ARGUMENT_TYPES = (
    "string",
    "integer",
    "number",
    "boolean",
    "string-list",
    "json",
)


def _parse_json(text: str, label: str) -> Any:
    try:
        value = strict_json_value_bytes(str(text).encode("utf-8"), label)
        # ``json.loads`` can turn a very large exponent into infinity without
        # invoking parse_constant.  Re-serialization closes that gap.
        canonical_json_bytes(value)
        return value
    except (TypeError, ValueError, ValidationError) as exc:
        raise ValidationError("{} is not valid JSON: {}".format(label, exc))


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _inferred_argument_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "string-list"
    return "json"


def _argument_text(value: Any, value_type: str) -> str:
    if value_type == "string":
        return value
    if value_type == "boolean":
        return "true" if value else "false"
    if value_type in ("integer", "number"):
        return str(value)
    return _json_text(value)


def _parse_typed_value(raw: str, value_type: str, label: str) -> Any:
    if value_type == "string":
        return raw
    if value_type == "integer":
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValidationError("{} is not an integer".format(label))
        normalized = str(raw).strip()
        if str(value) != normalized and not (
            normalized.startswith("+") and str(value) == normalized[1:]
        ):
            raise ValidationError("{} is not a canonical integer".format(label))
        return value
    if value_type == "number":
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValidationError("{} is not a number".format(label))
        if not math.isfinite(value):
            raise ValidationError("{} must be finite".format(label))
        return value
    if value_type == "boolean":
        normalized = str(raw).strip().lower()
        if normalized not in ("true", "false"):
            raise ValidationError("{} must be true or false".format(label))
        return normalized == "true"
    if value_type == "string-list":
        value = _parse_json(raw, label)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValidationError("{} must be a JSON array of strings".format(label))
        return value
    if value_type == "json":
        return _parse_json(raw, label)
    raise ValidationError("unknown value type {!r}".format(value_type))


def _notify(callback: Optional[Callable[[], None]]) -> None:
    if callback is not None:
        callback()


def _close_widget_tree(widget: Any) -> None:
    """Close one widget tree exactly once, including detached descendants."""

    seen = set()

    def close(current: Any) -> None:
        marker = id(current)
        if marker in seen:
            return
        seen.add(marker)
        related = list(tuple(getattr(current, "children", ())))
        for attribute in ("layout", "style"):
            candidate = getattr(current, attribute, None)
            if candidate is not None and hasattr(candidate, "close"):
                related.append(candidate)
        for child in related:
            close(child)
        try:
            current.close()
        except Exception:
            # Widget cleanup must remain safe during notebook/kernel teardown.
            pass

    if widget is not None:
        close(widget)


def _close_widget_shell(widget: Any) -> None:
    """Close a transient container without closing its reused child widgets."""

    if widget is None:
        return
    for attribute in ("layout", "style"):
        candidate = getattr(widget, attribute, None)
        if candidate is not None and hasattr(candidate, "close"):
            _close_widget_tree(candidate)
    try:
        widget.close()
    except Exception:
        pass


def _json_unique_key(value: Any) -> Any:
    """Model JSON Schema equality, including ``1 == 1.0`` but not ``true``."""

    if value is None:
        return ("null",)
    if isinstance(value, bool):
        return ("boolean", value)
    if isinstance(value, int):
        return ("number", Decimal(value).normalize())
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValidationError("JSON numbers must be finite")
        return ("number", Decimal(str(value)).normalize())
    if isinstance(value, str):
        return ("string", value)
    if isinstance(value, list):
        return ("array", tuple(_json_unique_key(item) for item in value))
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValidationError("JSON object keys must be strings")
        return (
            "object",
            tuple(sorted((key, _json_unique_key(item)) for key, item in value.items())),
        )
    raise ValidationError("unsupported JSON value {!r}".format(value))


class _ArgumentRow:
    def __init__(self, widgets: Any, key: str, value: Any, on_change: Callable[[], None]):
        self._widgets = widgets
        self._on_change = on_change
        if not isinstance(key, str):
            raise ValidationError("atom argument keys must be strings")
        _json_unique_key(value)
        value_type = _inferred_argument_type(value)
        raw_text = _argument_text(value, value_type)
        self.key = widgets.Text(value=key, description="键", layout=widgets.Layout(width="27%"))
        self.value_type = widgets.Dropdown(
            options=ARGUMENT_TYPES,
            value=value_type,
            description="类型",
            layout=widgets.Layout(width="23%"),
        )
        self.raw_value = widgets.Textarea(
            value=raw_text,
            description="值",
            rows=1,
            layout=widgets.Layout(width="50%"),
        )
        self.widget = widgets.HBox([self.key, self.value_type, self.raw_value])
        for field in (self.key, self.value_type, self.raw_value):
            field.observe(self._changed, names="value")

    def _changed(self, _change: Mapping[str, Any]) -> None:
        self._on_change()

    def value(self) -> tuple:
        key = self.key.value
        if not isinstance(key, str) or not key or key != key.strip():
            raise ValidationError("argument keys must be non-empty and have no surrounding whitespace")
        value = _parse_typed_value(
            self.raw_value.value,
            self.value_type.value,
            "argument {!r}".format(key),
        )
        return key, value


class _AtomEditor:
    def __init__(
        self,
        widgets: Any,
        atom: Mapping[str, Any],
        on_change: Callable[[], None],
    ):
        self._widgets = widgets
        self._on_change = on_change
        self._argument_rows: List[_ArgumentRow] = []
        self._argument_row_wrappers: List[Any] = []
        self._argument_remove_buttons: List[Any] = []
        if not isinstance(atom, Mapping):
            raise ValidationError("atom must be an object")
        category = atom.get("category", ATOM_CATEGORIES[0])
        layer = atom.get("layer", REQUIREMENT_LAYERS[0])
        polarity = atom.get("polarity", "present")
        if category not in ATOM_CATEGORIES:
            raise ValidationError("unknown atom category {!r}".format(category))
        if layer not in REQUIREMENT_LAYERS:
            raise ValidationError("unknown requirement layer {!r}".format(layer))
        if polarity not in ("present", "absent"):
            raise ValidationError("unknown atom polarity {!r}".format(polarity))
        arguments = atom.get("arguments", {})
        if not isinstance(arguments, Mapping):
            raise ValidationError("atom arguments must be an object")
        predicate = atom.get("predicate", "")
        notes = atom.get("notes", "")
        if not isinstance(predicate, str) or not isinstance(notes, str):
            raise ValidationError("atom predicate and notes must be strings")
        if any(not isinstance(key, str) for key in arguments):
            raise ValidationError("atom argument keys must be strings")
        for argument_value in arguments.values():
            _json_unique_key(argument_value)
            # ``str``/``json.dumps`` can fail for otherwise JSON-shaped
            # values (for example Python's configured integer digit limit).
            # Exercise the exact display conversion before allocating any
            # widget so a failed child constructor cannot leak partial comms.
            argument_type = _inferred_argument_type(argument_value)
            _argument_text(argument_value, argument_type)

        self.category = widgets.Dropdown(
            options=(
                ("参与者", "actor"),
                ("道路", "road"),
                ("空间关系", "spatial"),
                ("外部事件", "event"),
                ("事件时序", "temporal"),
                ("规则与风险", "normative"),
            ),
            value=category,
            description="要求类别",
        )
        self.predicate = widgets.Text(value=predicate, description="谓词")
        self.layer = widgets.Dropdown(
            options=(
                ("必须满足（所有同 intent 表述）", "core_required"),
                ("当前文本明确要求", "surface_required"),
                ("允许出现，但缺失不扣分", "permitted"),
                ("禁止出现", "forbidden"),
            ),
            value=layer,
            description="计分严格程度",
        )
        self.polarity = widgets.Dropdown(
            options=(("要求出现", "present"), ("要求不出现", "absent")),
            value=polarity,
            description="出现要求",
        )
        self.notes = widgets.Textarea(value=notes, description="备注", rows=2)
        self.arguments_box = widgets.VBox()
        self.add_argument_button = widgets.Button(description="添加参数", icon="plus")
        self.confirm_argument_delete = widgets.Checkbox(
            value=False,
            description="我确认下一次删除参数",
            indent=False,
        )
        self.add_argument_button.on_click(lambda _button: self.add_argument())
        self.confirm_argument_delete.observe(
            self._argument_delete_confirmation_changed, names="value"
        )
        self.widget = widgets.VBox(
            [
                widgets.HBox([self.category, self.predicate]),
                widgets.HBox([self.layer, self.polarity]),
                widgets.HTML("<b>参数</b>"),
                self.arguments_box,
                widgets.HBox([self.add_argument_button, self.confirm_argument_delete]),
                self.notes,
            ]
        )
        for field in (self.category, self.predicate, self.layer, self.polarity, self.notes):
            field.observe(self._changed, names="value")
        for key, value in arguments.items():
            self.add_argument(key, copy.deepcopy(value), notify=False)
        self._refresh_argument_box()

    def _changed(self, _change: Mapping[str, Any]) -> None:
        self._on_change()

    def _refresh_argument_box(self) -> None:
        for widget in self._argument_remove_buttons + self._argument_row_wrappers:
            _close_widget_shell(widget)
        self._argument_remove_buttons = []
        self._argument_row_wrappers = []
        rows = []
        for row in self._argument_rows:
            remove = self._widgets.Button(
                description="删除",
                icon="trash",
                disabled=not self.confirm_argument_delete.value,
                layout=self._widgets.Layout(width="82px"),
            )
            remove.on_click(
                lambda button, target=row: self._remove_argument_from_ui(button, target)
            )
            wrapper = self._widgets.HBox([row.widget, remove])
            self._argument_remove_buttons.append(remove)
            self._argument_row_wrappers.append(wrapper)
            rows.append(wrapper)
        self.arguments_box.children = tuple(rows)

    def _argument_delete_confirmation_changed(self, _change: Mapping[str, Any]) -> None:
        # Confirmation is UI state, not semantic form state; do not autosave it.
        self._refresh_argument_box()

    def _remove_argument_from_ui(self, button: Any, row: _ArgumentRow) -> None:
        if (
            button not in self._argument_remove_buttons
            or not self.confirm_argument_delete.value
        ):
            return
        self.confirm_argument_delete.value = False
        try:
            index = self._argument_rows.index(row)
            self.remove_argument(index)
        except (ValueError, IndexError, ValidationError):
            # A stale detached button is a no-op, never an exception escaping
            # the ipywidgets dispatcher.
            return

    def add_argument(self, key: str = "", value: Any = "", notify: bool = True) -> None:
        self._argument_rows.append(_ArgumentRow(self._widgets, key, value, self._on_change))
        self._refresh_argument_box()
        if notify:
            self._on_change()

    def remove_argument(self, index: int) -> None:
        if type(index) is not int or index < 0 or index >= len(self._argument_rows):
            raise IndexError("argument index out of range")
        removed = self._argument_rows.pop(index)
        self._refresh_argument_box()
        _close_widget_tree(removed.widget)
        self._on_change()

    def close(self) -> None:
        self.arguments_box.children = ()
        for row in self._argument_rows:
            _close_widget_tree(row.widget)
        self._argument_rows = []
        for widget in self._argument_remove_buttons + self._argument_row_wrappers:
            _close_widget_shell(widget)
        self._argument_remove_buttons = []
        self._argument_row_wrappers = []
        _close_widget_tree(self.widget)

    def value(self) -> Dict[str, Any]:
        predicate = self.predicate.value
        if not isinstance(predicate, str) or not predicate.strip():
            raise ValidationError("atom predicate must be non-empty")
        if predicate != predicate.strip():
            raise ValidationError("atom predicate cannot have surrounding whitespace")
        arguments: Dict[str, Any] = {}
        for row in self._argument_rows:
            key, value = row.value()
            if key in arguments:
                raise ValidationError("duplicate atom argument key {!r}".format(key))
            arguments[key] = value
        semantic = {
            "category": self.category.value,
            "predicate": predicate,
            "arguments": arguments,
            "layer": self.layer.value,
            "polarity": self.polarity.value,
            "notes": self.notes.value,
        }
        # Keep this editor aligned with the production constructor instead of
        # only trusting mutable widget options.
        make_atom(**semantic)
        return semantic


class AtomListEditor:
    """Edit zero or more semantic atoms without carrying IDs or provenance.

    Multiple output atoms are the representation used for split and new atom
    decisions.  ``split_current`` clones an atom into editable parts.  The
    internal ``merge_selected`` helper is intentionally not exposed in the UI;
    production merges require a cross-source wizard with explicit provenance.
    """

    def __init__(
        self,
        widgets: Any,
        atoms: Optional[Iterable[Mapping[str, Any]]] = None,
        on_change: Optional[Callable[[], None]] = None,
    ):
        self._widgets = widgets
        self._on_change = on_change
        self.atom_editors: List[_AtomEditor] = []
        self.current = widgets.Dropdown(description="当前 atom")
        self.selected = widgets.SelectMultiple(description="批量选择", rows=5)
        self.editor_box = widgets.VBox()
        self.new_button = widgets.Button(description="新建", icon="plus")
        self.clone_button = widgets.Button(description="克隆", icon="copy")
        self.split_button = widgets.Button(description="拆分", icon="code-fork")
        self.remove_button = widgets.Button(description="删除", icon="trash")
        self.confirm_delete = widgets.Checkbox(
            value=False,
            description="我确认下一次删除 atom",
            indent=False,
        )
        self.action_status = widgets.HTML()
        self.operation_warning = widgets.HTML(
            "<small style='color:#9c2f00'>删除前必须勾选一次性确认；"
            "执行后确认会自动复位。跨来源合并请使用主工作台向导。</small>"
        )
        self.new_button.on_click(lambda _button: self.add_atom())
        self.clone_button.on_click(self._clone_clicked)
        self.split_button.on_click(self._split_clicked)
        self.remove_button.on_click(self._remove_clicked)
        self.current.observe(self._current_changed, names="value")
        self.confirm_delete.observe(self._delete_confirmation_changed, names="value")
        self.widget = widgets.VBox(
            [
                self.current,
                widgets.HBox(
                    [
                        self.new_button,
                        self.clone_button,
                        self.split_button,
                        self.remove_button,
                        self.confirm_delete,
                    ]
                ),
                self.operation_warning,
                self.action_status,
                self.editor_box,
            ]
        )
        try:
            self.set_value(list(atoms or []), notify=False)
        except Exception:
            self.close()
            raise

    def _has_current(self) -> bool:
        return (
            type(self.current.value) is int
            and 0 <= self.current.value < len(self.atom_editors)
        )

    def _update_action_states(self) -> None:
        has_current = self._has_current()
        self.clone_button.disabled = not has_current
        self.split_button.disabled = not has_current
        self.confirm_delete.disabled = not has_current
        self.remove_button.disabled = not has_current or not self.confirm_delete.value

    def _delete_confirmation_changed(self, _change: Mapping[str, Any]) -> None:
        # Confirmation is transient UI state and must not trigger autosave.
        self._update_action_states()

    def _clone_clicked(self, _button: Any) -> None:
        if not self._has_current():
            return
        try:
            self.clone_current()
            self.action_status.value = ""
        except (ValidationError, IndexError) as exc:
            self.action_status.value = "<small style='color:#9c2f00'>{}</small>".format(
                html.escape(str(exc))
            )

    def _split_clicked(self, _button: Any) -> None:
        if not self._has_current():
            return
        try:
            self.split_current()
            self.action_status.value = ""
        except (ValidationError, IndexError) as exc:
            self.action_status.value = "<small style='color:#9c2f00'>{}</small>".format(
                html.escape(str(exc))
            )

    def _remove_clicked(self, _button: Any) -> None:
        if not self._has_current() or not self.confirm_delete.value:
            return
        self.confirm_delete.value = False
        try:
            self.remove_current()
            self.action_status.value = ""
        except (ValidationError, IndexError) as exc:
            self.action_status.value = "<small style='color:#9c2f00'>{}</small>".format(
                html.escape(str(exc))
            )

    def _label(self, index: int) -> str:
        editor = self.atom_editors[index]
        predicate = editor.predicate.value.strip() or "<未填写谓词>"
        return "{} · {} · {}".format(index + 1, editor.category.value, predicate)

    def _refresh_navigation(self, current_index: Optional[int] = None) -> None:
        old_selected = tuple(value for value in self.selected.value if value < len(self.atom_editors))
        options = [(self._label(index), index) for index in range(len(self.atom_editors))]
        self.current.options = options
        self.selected.options = options
        self.selected.value = old_selected
        if not options:
            self.current.value = None
            self.editor_box.children = ()
            self._update_action_states()
            return
        if current_index is None or current_index >= len(options):
            current_index = 0
        self.current.value = current_index
        self.editor_box.children = (self.atom_editors[current_index].widget,)
        self._update_action_states()

    def _child_changed(self) -> None:
        current = self.current.value
        self._refresh_navigation(current if isinstance(current, int) else 0)
        _notify(self._on_change)

    def _current_changed(self, change: Mapping[str, Any]) -> None:
        # A confirmation armed for a previous selection must never carry over
        # to a newly selected atom or one of its hidden argument editors.
        self.confirm_delete.value = False
        for editor in self.atom_editors:
            if editor.confirm_argument_delete.value:
                editor.confirm_argument_delete.value = False
        index = change.get("new")
        if isinstance(index, int) and 0 <= index < len(self.atom_editors):
            self.editor_box.children = (self.atom_editors[index].widget,)
        else:
            self.editor_box.children = ()
        self._update_action_states()

    def set_value(self, atoms: Sequence[Mapping[str, Any]], notify: bool = True) -> None:
        new_editors = []
        try:
            for atom in atoms:
                new_editors.append(
                    _AtomEditor(self._widgets, copy.deepcopy(atom), self._child_changed)
                )
        except Exception:
            for editor in new_editors:
                editor.close()
            raise
        old_editors = self.atom_editors
        self.confirm_delete.value = False
        self.selected.value = ()
        self.editor_box.children = ()
        self.atom_editors = new_editors
        self._refresh_navigation(0)
        for editor in old_editors:
            editor.close()
        if notify:
            _notify(self._on_change)

    def add_atom(self, atom: Optional[Mapping[str, Any]] = None) -> int:
        if atom is None:
            value = {
                "category": "actor",
                "predicate": "",
                "arguments": {},
                "layer": "core_required",
                "polarity": "present",
                "notes": "",
            }
        elif not isinstance(atom, Mapping):
            raise ValidationError("atom must be an object")
        else:
            value = atom
        self.atom_editors.append(_AtomEditor(self._widgets, copy.deepcopy(value), self._child_changed))
        index = len(self.atom_editors) - 1
        self._refresh_navigation(index)
        _notify(self._on_change)
        return index

    def clone_current(self) -> int:
        if not self.atom_editors or not isinstance(self.current.value, int):
            raise ValidationError("cannot clone without a current atom")
        return self.add_atom(self.atom_editors[self.current.value].value())

    def split_current(self, parts: int = 2) -> List[int]:
        if isinstance(parts, bool) or not isinstance(parts, int) or parts < 2:
            raise ValidationError("split requires at least two parts")
        if not self.atom_editors or not isinstance(self.current.value, int):
            raise ValidationError("cannot split without a current atom")
        source = self.atom_editors[self.current.value].value()
        created = []
        for _index in range(parts - 1):
            created.append(self.add_atom(source))
        return created

    def merge_selected(self, indices: Optional[Sequence[int]] = None) -> int:
        selected = tuple(indices if indices is not None else self.selected.value)
        if len(selected) < 2 or len(set(selected)) != len(selected):
            raise ValidationError("merge requires at least two distinct atoms")
        if any(type(index) is not int or index < 0 or index >= len(self.atom_editors) for index in selected):
            raise ValidationError("merge atom index out of range")
        selected = tuple(sorted(selected))
        merged = self.atom_editors[selected[0]].value()
        first = selected[0]
        # Index-based selections become stale as soon as entries are removed.
        # Clear them before rebuilding navigation so a second click cannot
        # merge newly shifted, unintended atoms.
        self.selected.value = ()
        removed = []
        for index in reversed(selected):
            removed.append(self.atom_editors.pop(index))
        self.atom_editors.insert(first, _AtomEditor(self._widgets, merged, self._child_changed))
        self._refresh_navigation(first)
        for editor in removed:
            editor.close()
        _notify(self._on_change)
        return first

    def remove_current(self) -> None:
        if not self.atom_editors or not isinstance(self.current.value, int):
            raise ValidationError("cannot remove without a current atom")
        index = self.current.value
        self.selected.value = ()
        removed = self.atom_editors.pop(index)
        self._refresh_navigation(min(index, len(self.atom_editors) - 1))
        removed.close()
        _notify(self._on_change)

    def close(self) -> None:
        self.editor_box.children = ()
        for editor in self.atom_editors:
            editor.close()
        self.atom_editors = []
        _close_widget_tree(self.selected)
        _close_widget_tree(self.widget)

    def value(self) -> List[Dict[str, Any]]:
        return [editor.value() for editor in self.atom_editors]


class _TypedValueRow:
    def __init__(self, widgets: Any, value: Any, on_change: Callable[[], None]):
        self._on_change = on_change
        _json_unique_key(value)
        value_type = _inferred_argument_type(value)
        raw_text = _argument_text(value, value_type)
        self.value_type = widgets.Dropdown(
            options=ARGUMENT_TYPES,
            value=value_type,
            description="类型",
            layout=widgets.Layout(width="32%"),
        )
        self.raw_value = widgets.Text(
            value=raw_text,
            description="值",
            layout=widgets.Layout(width="68%"),
        )
        self.widget = widgets.HBox([self.value_type, self.raw_value])
        self.value_type.observe(self._changed, names="value")
        self.raw_value.observe(self._changed, names="value")

    def _changed(self, _change: Mapping[str, Any]) -> None:
        self._on_change()

    def value(self) -> Any:
        return _parse_typed_value(
            self.raw_value.value,
            self.value_type.value,
            "CPD allowed value",
        )


class _BinRow:
    def __init__(self, widgets: Any, key: str, value: Any, on_change: Callable[[], None]):
        if not isinstance(key, str):
            raise ValidationError("CPD bin names must be strings")
        try:
            numeric_value = float(value)
        except (TypeError, ValueError, OverflowError):
            numeric_value = float("nan")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(numeric_value)
            or numeric_value < 0
        ):
            raise ValidationError("CPD bin boundaries must be finite non-negative numbers")
        self._on_change = on_change
        self.key = widgets.Text(
            value=key, description="分箱键", layout=widgets.Layout(width="55%")
        )
        self.boundary = widgets.FloatText(
            value=numeric_value, description="米", layout=widgets.Layout(width="45%")
        )
        self.widget = widgets.HBox([self.key, self.boundary])
        self.key.observe(self._changed, names="value")
        self.boundary.observe(self._changed, names="value")

    def _changed(self, _change: Mapping[str, Any]) -> None:
        self._on_change()

    def value(self) -> tuple:
        key = self.key.value
        boundary = self.boundary.value
        if not isinstance(key, str) or not key or key != key.strip():
            raise ValidationError("CPD bin names must be non-empty and trimmed")
        if not math.isfinite(float(boundary)) or float(boundary) < 0:
            raise ValidationError("CPD bin boundaries must be finite non-negative numbers")
        return key, float(boundary)


class _DimensionEditor:
    _ACTOR_CLASSES = ("ego_bus", "motor_vehicle", "cyclist", "pedestrian")

    def __init__(
        self,
        widgets: Any,
        dimension: Mapping[str, Any],
        on_change: Callable[[], None],
    ):
        self._widgets = widgets
        self._on_change = on_change
        self.allowed_rows: List[_TypedValueRow] = []
        self.bin_rows: List[_BinRow] = []
        self._allowed_row_wrappers: List[Any] = []
        self._allowed_remove_buttons: List[Any] = []
        self._bin_row_wrappers: List[Any] = []
        self._bin_remove_buttons: List[Any] = []
        if not isinstance(dimension, Mapping):
            raise ValidationError("CPD dimension must be an object")
        allowed_dimension_keys = {
            "name",
            "common_semantic",
            "cardinality",
            "allowed_values",
            "reason",
            "bin_definition_m",
            "target_selector",
        }
        if set(dimension) - allowed_dimension_keys:
            raise ValidationError("CPD dimension contains unsupported fields")
        if dimension.get("common_semantic", True) is not True:
            raise ValidationError("CPD dimensions must be common semantic")
        if dimension.get("cardinality", "exactly_one") != "exactly_one":
            raise ValidationError("CPD dimensions must use exactly_one cardinality")
        name = dimension.get("name", "")
        reason = dimension.get("reason", "")
        allowed_values = dimension.get("allowed_values", [])
        if not isinstance(name, str) or not isinstance(reason, str):
            raise ValidationError("CPD dimension name and reason must be strings")
        if not isinstance(allowed_values, list):
            raise ValidationError("CPD allowed_values must be an array")
        # Validate JSON-compatibility before widgets can normalize any value.
        for allowed_value in allowed_values:
            _json_unique_key(allowed_value)
            allowed_type = _inferred_argument_type(allowed_value)
            _argument_text(allowed_value, allowed_type)
        bins = dimension.get("bin_definition_m", {})
        if not isinstance(bins, Mapping):
            raise ValidationError("CPD bin_definition_m must be an object")
        for key, boundary in bins.items():
            if not isinstance(key, str):
                raise ValidationError("CPD bin names must be strings")
            try:
                finite_boundary = math.isfinite(float(boundary))
            except (TypeError, ValueError, OverflowError):
                finite_boundary = False
            if (
                isinstance(boundary, bool)
                or not isinstance(boundary, (int, float))
                or not finite_boundary
                or boundary < 0
            ):
                raise ValidationError(
                    "CPD bin boundaries must be finite non-negative numbers"
                )
        selector = dimension.get("target_selector", {})
        if "target_selector" in dimension and not isinstance(selector, Mapping):
            raise ValidationError("CPD target_selector must be an object")
        signature = selector.get("target_signature", {}) if isinstance(selector, Mapping) else {}
        actor_class = signature.get("actor_class", "motor_vehicle")
        if actor_class not in self._ACTOR_CLASSES:
            raise ValidationError("invalid CPD target actor class {!r}".format(actor_class))
        if "target_selector" in dimension:
            try:
                expected_selector = query_blind_target_selector(name, actor_class)
            except ValidationError:
                raise ValidationError(
                    "CPD target_selector must match a frozen query-blind selector"
                )
            if canonical_json_bytes(selector) != canonical_json_bytes(expected_selector):
                raise ValidationError(
                    "CPD target_selector must match a frozen query-blind selector"
                )
        self.name = widgets.Text(value=name, description="名称")
        self.reason = widgets.Textarea(
            value=reason, description="原因", rows=2
        )
        self.allowed_values_box = widgets.VBox()
        self.add_allowed_button = widgets.Button(description="添加允许值", icon="plus")
        self.add_allowed_button.on_click(lambda _button: self.add_allowed_value())
        self.confirm_field_delete = widgets.Checkbox(
            value=False,
            description="我确认下一次删除允许值/分箱",
            indent=False,
        )
        self.confirm_field_delete.observe(
            self._field_delete_confirmation_changed, names="value"
        )
        self.bin_enabled = widgets.Checkbox(
            value="bin_definition_m" in dimension, description="使用距离分箱"
        )
        self.bin_rows_box = widgets.VBox()
        self.add_bin_button = widgets.Button(description="添加分箱边界", icon="plus")
        self.add_bin_button.on_click(lambda _button: self.add_bin())
        self.bin_panel = widgets.VBox([self.bin_rows_box, self.add_bin_button])
        self.selector_enabled = widgets.Checkbox(
            value="target_selector" in dimension, description="使用冻结目标选择器"
        )
        self.target_actor_class = widgets.Dropdown(
            options=self._ACTOR_CLASSES,
            value=actor_class,
            description="目标类别",
        )
        self.selector_preview = widgets.HTML()
        self.selector_panel = widgets.VBox([self.target_actor_class, self.selector_preview])
        self.widget = widgets.VBox(
            [
                self.name,
                widgets.HTML("<code>common_semantic=true; cardinality=exactly_one</code>"),
                widgets.HTML("<b>允许值</b>"),
                self.allowed_values_box,
                widgets.HBox([self.add_allowed_button, self.confirm_field_delete]),
                self.reason,
                self.bin_enabled,
                self.bin_panel,
                self.selector_enabled,
                self.selector_panel,
            ]
        )
        for allowed_value in allowed_values:
            self.add_allowed_value(copy.deepcopy(allowed_value), notify=False)
        for key, boundary in bins.items():
            self.add_bin(key, boundary, notify=False)
        for field in (
            self.name,
            self.reason,
            self.bin_enabled,
            self.selector_enabled,
            self.target_actor_class,
        ):
            field.observe(self._changed, names="value")
        self._refresh_allowed_values()
        self._refresh_bins()
        self._refresh_conditional_fields()

    def _changed(self, _change: Mapping[str, Any]) -> None:
        self._refresh_conditional_fields()
        self._on_change()

    def _refresh_allowed_values(self) -> None:
        for widget in self._allowed_remove_buttons + self._allowed_row_wrappers:
            _close_widget_shell(widget)
        self._allowed_remove_buttons = []
        self._allowed_row_wrappers = []
        rows = []
        for row in self.allowed_rows:
            remove = self._widgets.Button(
                description="删除",
                icon="trash",
                disabled=not self.confirm_field_delete.value,
                layout=self._widgets.Layout(width="82px"),
            )
            remove.on_click(
                lambda button, target=row: self._remove_allowed_from_ui(button, target)
            )
            wrapper = self._widgets.HBox([row.widget, remove])
            self._allowed_remove_buttons.append(remove)
            self._allowed_row_wrappers.append(wrapper)
            rows.append(wrapper)
        self.allowed_values_box.children = tuple(rows)

    def _field_delete_confirmation_changed(self, _change: Mapping[str, Any]) -> None:
        # Confirmation is transient UI state and must not trigger autosave.
        self._refresh_allowed_values()
        self._refresh_bins()

    def _remove_allowed_from_ui(self, button: Any, row: _TypedValueRow) -> None:
        if button not in self._allowed_remove_buttons or not self.confirm_field_delete.value:
            return
        self.confirm_field_delete.value = False
        try:
            index = self.allowed_rows.index(row)
            self.remove_allowed_value(index)
        except (ValueError, IndexError, ValidationError):
            return

    def add_allowed_value(self, value: Any = "", notify: bool = True) -> None:
        self.allowed_rows.append(_TypedValueRow(self._widgets, value, self._on_change))
        self._refresh_allowed_values()
        if notify:
            self._on_change()

    def remove_allowed_value(self, index: int) -> None:
        if type(index) is not int or index < 0 or index >= len(self.allowed_rows):
            raise IndexError("allowed-value index out of range")
        removed = self.allowed_rows.pop(index)
        self._refresh_allowed_values()
        _close_widget_tree(removed.widget)
        self._on_change()

    def _refresh_bins(self) -> None:
        for widget in self._bin_remove_buttons + self._bin_row_wrappers:
            _close_widget_shell(widget)
        self._bin_remove_buttons = []
        self._bin_row_wrappers = []
        rows = []
        for row in self.bin_rows:
            remove = self._widgets.Button(
                description="删除",
                icon="trash",
                disabled=not self.confirm_field_delete.value,
                layout=self._widgets.Layout(width="82px"),
            )
            remove.on_click(
                lambda button, target=row: self._remove_bin_from_ui(button, target)
            )
            wrapper = self._widgets.HBox([row.widget, remove])
            self._bin_remove_buttons.append(remove)
            self._bin_row_wrappers.append(wrapper)
            rows.append(wrapper)
        self.bin_rows_box.children = tuple(rows)

    def _remove_bin_from_ui(self, button: Any, row: _BinRow) -> None:
        if button not in self._bin_remove_buttons or not self.confirm_field_delete.value:
            return
        self.confirm_field_delete.value = False
        try:
            index = self.bin_rows.index(row)
            self.remove_bin(index)
        except (ValueError, IndexError, ValidationError):
            return

    def add_bin(self, key: str = "", value: float = 0.0, notify: bool = True) -> None:
        self.bin_rows.append(_BinRow(self._widgets, key, value, self._on_change))
        self._refresh_bins()
        if notify:
            self._on_change()

    def remove_bin(self, index: int) -> None:
        if type(index) is not int or index < 0 or index >= len(self.bin_rows):
            raise IndexError("bin index out of range")
        removed = self.bin_rows.pop(index)
        self._refresh_bins()
        _close_widget_tree(removed.widget)
        self._on_change()

    def close(self) -> None:
        self.allowed_values_box.children = ()
        self.bin_rows_box.children = ()
        for row in self.allowed_rows:
            _close_widget_tree(row.widget)
        for row in self.bin_rows:
            _close_widget_tree(row.widget)
        self.allowed_rows = []
        self.bin_rows = []
        for widget in (
            self._allowed_remove_buttons
            + self._allowed_row_wrappers
            + self._bin_remove_buttons
            + self._bin_row_wrappers
        ):
            _close_widget_shell(widget)
        self._allowed_remove_buttons = []
        self._allowed_row_wrappers = []
        self._bin_remove_buttons = []
        self._bin_row_wrappers = []
        _close_widget_tree(self.widget)

    def _refresh_conditional_fields(self) -> None:
        self.bin_panel.layout.display = "" if self.bin_enabled.value else "none"
        self.selector_panel.layout.display = "" if self.selector_enabled.value else "none"
        name = self.name.value.strip()
        if name not in QUERY_BLIND_TARGET_SELECTORS:
            self.selector_preview.value = (
                "<em>该名称没有冻结目标选择器；保存时会失效关闭。</em>"
            )
            return
        selector = query_blind_target_selector(name, self.target_actor_class.value)
        self.selector_preview.value = (
            "<small>算法由冻结契约自动派生：<code>{}</code></small>".format(
                selector["selector_id"]
            )
        )

    def value(self) -> Dict[str, Any]:
        name = self.name.value
        if not isinstance(name, str) or not name or name != name.strip():
            raise ValidationError("CPD dimension names must be non-empty and trimmed")
        allowed = [row.value() for row in self.allowed_rows]
        if not allowed:
            raise ValidationError("CPD allowed_values must be non-empty")
        encoded = [_json_unique_key(item) for item in allowed]
        if len(set(encoded)) != len(encoded):
            raise ValidationError("CPD allowed_values must be unique")
        value: Dict[str, Any] = {
            "name": name,
            "common_semantic": True,
            "cardinality": "exactly_one",
            "allowed_values": allowed,
        }
        reason = self.reason.value
        if reason:
            if reason != reason.strip():
                raise ValidationError("CPD dimension reason cannot have surrounding whitespace")
            value["reason"] = reason
        if self.bin_enabled.value:
            bins: Dict[str, float] = {}
            for row in self.bin_rows:
                key, boundary = row.value()
                if key in bins:
                    raise ValidationError("CPD bin names must be unique")
                bins[key] = boundary
            value["bin_definition_m"] = bins
        if self.selector_enabled.value:
            if name not in QUERY_BLIND_TARGET_SELECTORS:
                raise ValidationError(
                    "CPD target_selector must match a frozen query-blind selector"
                )
            value["target_selector"] = query_blind_target_selector(
                name, self.target_actor_class.value
            )
        elif name in QUERY_BLIND_TARGET_SELECTORS:
            raise ValidationError("CPD dimension {!r} requires target_selector".format(name))
        return value


class CPDPolicyEditor:
    """Edit one draft CPD policy with structured dimension fields."""

    def __init__(
        self,
        widgets: Any,
        policy: Optional[Mapping[str, Any]] = None,
        on_change: Optional[Callable[[], None]] = None,
    ):
        self._widgets = widgets
        self._on_change = on_change
        self._suspend_notifications = False
        self.dimension_editors: List[_DimensionEditor] = []
        source = copy.deepcopy(
            policy
            if policy is not None
            else {
                "candidate": False,
                "eligible": False,
                "dimensions": [],
                "decision_status": "draft",
                "cross_platform_judgeable": None,
            }
        )
        if source.get("decision_status", "draft") != "draft":
            raise ValidationError("CPDPolicyEditor only emits draft policies")
        self.candidate = widgets.Checkbox(
            value=False, description="存在值得评价的合理变化"
        )
        self.eligible = widgets.Checkbox(
            value=False, description="满足 CPD_common 正式条件"
        )
        self.cross_platform_judgeable = widgets.Dropdown(
            options=(("待确认", None), ("是", True), ("否", False)),
            value=None,
            description="跨平台可判定",
        )
        self.current = widgets.Dropdown(description="当前维度")
        self.dimension_box = widgets.VBox()
        self.new_button = widgets.Button(description="新建维度", icon="plus")
        self.clone_button = widgets.Button(description="克隆维度", icon="copy")
        self.remove_button = widgets.Button(description="删除维度", icon="trash")
        self.confirm_delete = widgets.Checkbox(
            value=False,
            description="我确认下一次删除维度",
            indent=False,
        )
        self.action_status = widgets.HTML()
        self.operation_warning = widgets.HTML(
            "<small style='color:#9c2f00'>删除维度前必须勾选一次性确认；"
            "执行后确认会自动复位。</small>"
        )
        self.new_button.on_click(lambda _button: self.add_dimension())
        self.clone_button.on_click(self._clone_clicked)
        self.remove_button.on_click(self._remove_clicked)
        self.current.observe(self._current_changed, names="value")
        self.confirm_delete.observe(self._delete_confirmation_changed, names="value")
        for field in (self.candidate, self.eligible, self.cross_platform_judgeable):
            field.observe(self._child_changed_event, names="value")
        self.widget = widgets.VBox(
            [
                self._widgets.HBox(
                    [self.candidate, self.eligible, self.cross_platform_judgeable]
                ),
                self._widgets.HBox(
                    [
                        self.current,
                        self.new_button,
                        self.clone_button,
                        self.remove_button,
                        self.confirm_delete,
                    ]
                ),
                self.operation_warning,
                self.action_status,
                self.dimension_box,
            ]
        )
        try:
            self.set_value(source, notify=False)
        except Exception:
            self.close()
            raise

    def _has_current(self) -> bool:
        return (
            type(self.current.value) is int
            and 0 <= self.current.value < len(self.dimension_editors)
        )

    def _update_action_states(self) -> None:
        has_current = self._has_current()
        self.clone_button.disabled = not has_current
        self.confirm_delete.disabled = not has_current
        self.remove_button.disabled = not has_current or not self.confirm_delete.value

    def _delete_confirmation_changed(self, _change: Mapping[str, Any]) -> None:
        self._update_action_states()

    def _clone_clicked(self, _button: Any) -> None:
        if not self._has_current():
            return
        try:
            self.clone_current()
            self.action_status.value = ""
        except (ValidationError, IndexError) as exc:
            self.action_status.value = "<small style='color:#9c2f00'>{}</small>".format(
                html.escape(str(exc))
            )

    def _remove_clicked(self, _button: Any) -> None:
        if not self._has_current() or not self.confirm_delete.value:
            return
        self.confirm_delete.value = False
        try:
            self.remove_current()
            self.action_status.value = ""
        except (ValidationError, IndexError) as exc:
            self.action_status.value = "<small style='color:#9c2f00'>{}</small>".format(
                html.escape(str(exc))
            )

    def _label(self, index: int) -> str:
        name = self.dimension_editors[index].name.value.strip() or "<未填写名称>"
        return "{} · {}".format(index + 1, name)

    def _refresh_navigation(self, current_index: Optional[int] = None) -> None:
        options = [(self._label(index), index) for index in range(len(self.dimension_editors))]
        self.current.options = options
        if not options:
            self.current.value = None
            self.dimension_box.children = ()
            self._update_action_states()
            return
        if current_index is None or current_index >= len(options):
            current_index = 0
        self.current.value = current_index
        self.dimension_box.children = (self.dimension_editors[current_index].widget,)
        self._update_action_states()

    def _child_changed_event(self, _change: Mapping[str, Any]) -> None:
        if not self._suspend_notifications:
            _notify(self._on_change)

    def _child_changed(self) -> None:
        current = self.current.value
        self._refresh_navigation(current if isinstance(current, int) else 0)
        if not self._suspend_notifications:
            _notify(self._on_change)

    def _current_changed(self, change: Mapping[str, Any]) -> None:
        # Do not carry a destructive confirmation across dimensions.  Hidden
        # field-level confirmations are reset for the same reason.
        self.confirm_delete.value = False
        for editor in self.dimension_editors:
            if editor.confirm_field_delete.value:
                editor.confirm_field_delete.value = False
        index = change.get("new")
        if isinstance(index, int) and 0 <= index < len(self.dimension_editors):
            self.dimension_box.children = (self.dimension_editors[index].widget,)
        else:
            self.dimension_box.children = ()
        self._update_action_states()

    def set_value(self, policy: Mapping[str, Any], notify: bool = True) -> None:
        if not isinstance(policy, Mapping):
            raise ValidationError("CPD policy must be an object")
        allowed_policy_keys = {
            "candidate",
            "eligible",
            "dimensions",
            "decision_status",
            "cross_platform_judgeable",
        }
        required_policy_keys = set(allowed_policy_keys)
        if set(policy) - allowed_policy_keys:
            raise ValidationError("CPD policy contains unsupported fields")
        if required_policy_keys - set(policy):
            raise ValidationError("CPD policy is missing required fields")
        if policy.get("decision_status", "draft") != "draft":
            raise ValidationError("CPDPolicyEditor only emits draft policies")
        dimensions = policy.get("dimensions", [])
        if not isinstance(dimensions, list):
            raise ValidationError("CPD dimensions must be an array")
        candidate = policy.get("candidate", False)
        eligible = policy.get("eligible", False)
        cross_platform = policy.get("cross_platform_judgeable")
        if not isinstance(candidate, bool) or not isinstance(eligible, bool):
            raise ValidationError("CPD candidate and eligible must be booleans")
        if cross_platform is not None and not isinstance(cross_platform, bool):
            raise ValidationError(
                "CPD cross_platform_judgeable must be true, false or null"
            )
        # Construct and validate the complete replacement before touching any
        # live widget.  This makes a failed set_value transaction invisible to
        # the current form, selection and callback stream.
        new_editors = []
        try:
            for dimension in dimensions:
                new_editors.append(
                    _DimensionEditor(
                        self._widgets,
                        copy.deepcopy(dimension),
                        self._child_changed,
                    )
                )
            new_dimensions = [editor.value() for editor in new_editors]
            names = [dimension["name"] for dimension in new_dimensions]
            if len(set(names)) != len(names):
                raise ValidationError("CPD dimension names must be unique")
            if eligible and not candidate:
                raise ValidationError("an eligible CPD policy must be a candidate")
            if eligible and not new_dimensions:
                raise ValidationError("an eligible CPD policy requires at least one dimension")
            if cross_platform is True and not eligible:
                raise ValidationError(
                    "a cross-platform-judgeable CPD policy must be eligible"
                )
        except Exception:
            for editor in new_editors:
                editor.close()
            raise

        old_editors = self.dimension_editors
        self._suspend_notifications = True
        try:
            self.confirm_delete.value = False
            self.candidate.value = candidate
            self.eligible.value = eligible
            self.cross_platform_judgeable.value = cross_platform
            self.dimension_box.children = ()
            self.dimension_editors = new_editors
            self._refresh_navigation(0)
        finally:
            self._suspend_notifications = False
        for editor in old_editors:
            editor.close()
        if notify:
            _notify(self._on_change)

    def add_dimension(self, dimension: Optional[Mapping[str, Any]] = None) -> int:
        if dimension is None:
            value = {
                "name": "",
                "common_semantic": True,
                "cardinality": "exactly_one",
                "allowed_values": [],
            }
        elif not isinstance(dimension, Mapping):
            raise ValidationError("CPD dimension must be an object")
        else:
            value = dimension
        self.dimension_editors.append(
            _DimensionEditor(self._widgets, copy.deepcopy(value), self._child_changed)
        )
        index = len(self.dimension_editors) - 1
        self._refresh_navigation(index)
        _notify(self._on_change)
        return index

    def clone_current(self) -> int:
        if not self.dimension_editors or not isinstance(self.current.value, int):
            raise ValidationError("cannot clone without a current CPD dimension")
        return self.add_dimension(self.dimension_editors[self.current.value].value())

    def remove_current(self) -> None:
        if not self.dimension_editors or not isinstance(self.current.value, int):
            raise ValidationError("cannot remove without a current CPD dimension")
        index = self.current.value
        removed = self.dimension_editors.pop(index)
        self._refresh_navigation(min(index, len(self.dimension_editors) - 1))
        removed.close()
        _notify(self._on_change)

    def close(self) -> None:
        self.dimension_box.children = ()
        for editor in self.dimension_editors:
            editor.close()
        self.dimension_editors = []
        _close_widget_tree(self.widget)

    def value(self) -> Dict[str, Any]:
        candidate = self.candidate.value
        eligible = self.eligible.value
        cross_platform = self.cross_platform_judgeable.value
        if not isinstance(candidate, bool) or not isinstance(eligible, bool):
            raise ValidationError("CPD candidate and eligible must be booleans")
        if cross_platform is not None and not isinstance(cross_platform, bool):
            raise ValidationError("CPD cross_platform_judgeable must be true, false or null")
        dimensions = [editor.value() for editor in self.dimension_editors]
        names = [dimension["name"] for dimension in dimensions]
        if len(set(names)) != len(names):
            raise ValidationError("CPD dimension names must be unique")
        if eligible and not candidate:
            raise ValidationError("an eligible CPD policy must be a candidate")
        if eligible and not dimensions:
            raise ValidationError("an eligible CPD policy requires at least one dimension")
        if cross_platform is True and not eligible:
            raise ValidationError("a cross-platform-judgeable CPD policy must be eligible")
        return {
            "candidate": candidate,
            "eligible": eligible,
            "dimensions": dimensions,
            "decision_status": "draft",
            "cross_platform_judgeable": cross_platform,
        }


__all__ = ["AtomListEditor", "CPDPolicyEditor"]
