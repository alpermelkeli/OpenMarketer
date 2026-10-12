"""How text a model wrote is read: as strict JSON, into types that open and import nothing.

A tool's arguments and an action's payload are written by a model, so they are data and
never a decision. This module is the one reading of them. ``read_strict_json`` converts
nothing to the type the model wants (``"7"`` is not 7), lets nothing JSON cannot write pass
(NaN, infinity) and, when the data does not fit, says which fields are at fault and never
what was sent: a path in a refusal keeps only the names the code declared and the positions
in a list (see ``UNKNOWN_FIELD``), so a key the model invented cannot put its text into an
error that is logged or shown to the model again.

What may be read is checked when it is declared, with two questions about the model that holds
the fields. ``lenient_types`` finds what the library would silently drop a misspelt or
invented field from, which would turn a typing mistake into a default.
``fields_that_name_files_or_code`` finds a field whose type turns text into a file path or an
importable name (``Path``, ``ImportString``): a model would then choose what the process opens
or imports.

This module runs nothing of an agent and knows no graph framework, workflow engine or database.
Whom an error is shown to, and whether it ends a run, is for its callers.
"""

import dataclasses
import json
from collections.abc import Iterator, Mapping
from typing import Any, TypeAliasType, cast, get_args, get_type_hints, is_typeddict

from pydantic import BaseModel, ImportString, PydanticUserError, ValidationError
from pydantic.types import PathType

# The most of a name, or of a list of names, an error message repeats of what the model sent.
MAX_SHOWN_CHARACTERS = 60
MAX_SHOWN_FIELDS = 5

# What a part of a field's path is shown as when the code did not declare it: a key the model
# invented, a key of a free-form dictionary, the name of a member of a union.
UNKNOWN_FIELD = "<field>"

# The library declares ``ImportString`` as an alias of its argument for a type checker, and it is
# a class: ``ImportString`` bare is the annotation, and ``ImportString[Any]`` is an ``Annotated``
# whose metadata is an instance of it.
_IMPORT_STRING = cast("type[Any]", ImportString)

# What is in a model's schema that is a model, a dataclass or a typed dict, and so reads fields.
_STRUCTURES = ("model", "dataclass", "typed-dict")
# What in the library's schema is not a schema of the data: Python objects and values that code
# declared, which a walk must not take for sub-schemas.
_NOT_SUBSCHEMAS = frozenset({"cls", "default", "expected", "metadata", "serialization"})


class UnreadableModel(ValueError):
    """A model cannot read anything yet: a type it holds is not defined."""

    def __init__(self, model: type[BaseModel], reason: str) -> None:
        super().__init__(f"model {model.__name__} cannot be read from: {reason}")


class DoesNotFit(Exception):
    """What was sent does not fit the model that reads it; ``fields`` names where, never what."""

    def __init__(self, fields: tuple[str, ...]) -> None:
        super().__init__(f"does not fit{fields_phrase(fields)}")
        self.fields = fields


def read_strict_json[Model: BaseModel](model: type[Model], data: Mapping[str, Any]) -> Model:
    """``data`` as ``model``, read as strict JSON: the one reading of what a model sent.

    Nothing is converted to the type the model wants (``"7"`` is not 7) and
    nothing JSON cannot write (NaN, infinity) passes. A field the model does
    not declare is refused only by a model that forbids extra fields (see
    ``lenient_types``). Raises ``DoesNotFit``, which names the fields at fault
    and never their values or any name the code did not declare.
    """
    try:
        text = json.dumps(data, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise DoesNotFit(_fields_not_json(model, data)) from None
    try:
        # Strict, and from JSON text: nothing is converted to the type the model wants, and
        # the library still reads a UUID, a date or an enum value from the string JSON has.
        return model.model_validate_json(text, strict=True)
    except ValidationError as e:
        # ``from None``: the library's error holds the values it refused.
        raise DoesNotFit(_fields_at_fault(model, e)) from None


def lenient_types(model: type[BaseModel]) -> Iterator[type]:
    """Each model, dataclass or typed dict in ``model``, at any depth, that allows extra fields.

    The library's default is to drop a field it does not know, so a misspelt or invented field
    would pass unnoticed. This asks what the library will do, from the schema it built, so an
    alias, a dataclass or a typed dict is looked at as the library sees it: a dataclass or a
    typed dict with no config of its own takes the one of the model that holds it. A root
    model has no fields to be extra and is not asked. ``model`` itself is asked too.

    Raises ``UnreadableModel`` for a model that holds a type that is not defined, which has
    no schema and would be taken for one with nothing to refuse.
    """
    seen: set[type] = set()
    for node in _schema_nodes(_core_schema(model)):
        if node.get("type") not in _STRUCTURES or node.get("root_model"):
            continue
        cls: type = node["cls"]
        if cls in seen:
            continue
        seen.add(cls)
        config = node.get("config") or {}
        extra = node.get("extra_behavior") or config.get("extra_fields_behavior")
        if extra != "forbid":
            yield cls


def fields_that_name_files_or_code(model: type[BaseModel]) -> Iterator[tuple[type, str]]:
    """Each field, with the type that holds it, whose type reads text as a path or an import name.

    ``Path`` and the types made of it (``FilePath``, ``DirectoryPath``, ``NewPath``) accept
    ``../../etc/passwd``, and ``ImportString`` imports what the text names. Looking through
    aliases, unions, generics and the fields of the models, dataclasses and typed dicts
    that ``model`` holds.
    """
    for structure in _structures_of(model, set()):
        for name, parts in _fields_of(structure):
            if any(_names_files_or_code(part) for part in parts):
                yield structure, name


def fields_phrase(fields: tuple[str, ...]) -> str:
    """The fields at fault as the end of a message: ``": a, b and 3 more"``, or nothing."""
    listed = ", ".join(shown(name) for name in fields[:MAX_SHOWN_FIELDS])
    more = len(fields) - MAX_SHOWN_FIELDS
    if more > 0:
        listed += f" and {more} more"
    return f": {listed}" if listed else ""


def shown(text: str) -> str:
    """Text the model may have written, quoted on one line and short enough for a message."""
    quoted = repr(text)
    if len(quoted) <= MAX_SHOWN_CHARACTERS:
        return quoted
    return quoted[: MAX_SHOWN_CHARACTERS - 3] + "..."


# ----------------------------------------------------------- the library's schema
def _schema_nodes(schema: Any) -> Iterator[dict[str, Any]]:
    """Each dictionary of the library's schema, at any depth: its sub-schemas and their parts."""
    if isinstance(schema, dict):
        yield schema
        for key, value in schema.items():
            if key not in _NOT_SUBSCHEMAS:
                yield from _schema_nodes(value)
    elif isinstance(schema, list | tuple):
        for item in schema:
            yield from _schema_nodes(item)


def _core_schema(model: type[BaseModel]) -> Any:
    schema = model.__pydantic_core_schema__
    if isinstance(schema, dict):
        return schema
    try:
        # The type may have been defined since the model was made.
        model.model_rebuild(raise_errors=True)
    except (PydanticUserError, NameError) as e:
        raise UnreadableModel(model, str(e).split("\n")[0]) from None
    return model.__pydantic_core_schema__


def _declared_names(model: type[BaseModel]) -> frozenset[str]:
    """The names of the fields of ``model`` and of what it holds, and their aliases."""
    names: set[str] = set()
    for node in _schema_nodes(_core_schema(model)):
        kind = node.get("type")
        if kind in ("model-fields", "typed-dict"):
            for name, field in node["fields"].items():
                names.add(name)
                names.update(_alias_names(field.get("validation_alias")))
        elif kind == "dataclass-args":
            for field in node["fields"]:
                names.add(field["name"])
                names.update(_alias_names(field.get("validation_alias")))
    return frozenset(names)


def _alias_names(alias: Any) -> Iterator[str]:
    if isinstance(alias, str):
        yield alias
    elif isinstance(alias, list | tuple):
        for part in alias:
            yield from _alias_names(part)


# -------------------------------------------------------------- what is at fault
def _fields_not_json(model: type[BaseModel], data: Any) -> tuple[str, ...]:
    """The fields of ``data`` that JSON cannot hold, to name them without showing them."""
    if not isinstance(data, Mapping):
        return ()
    declared = _declared_names(model)
    not_json: list[str] = []
    for field, value in data.items():
        try:
            json.dumps({field: value}, allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            not_json.append(_shown_part(field, declared))
    return tuple(dict.fromkeys(not_json))


def _fields_at_fault(model: type[BaseModel], error: ValidationError) -> tuple[str, ...]:
    declared = _declared_names(model)
    problems = error.errors(include_input=False, include_url=False, include_context=False)
    paths = (
        ".".join(_shown_part(part, declared) for part in problem["loc"]) for problem in problems
    )
    # Several problems of one field are one field; an error of the whole model names none.
    return tuple(dict.fromkeys(path for path in paths if path))


def _shown_part(part: object, declared: frozenset[str]) -> str:
    """A part of a field's path, if it is a position or a name the code declared."""
    if isinstance(part, int) and not isinstance(part, bool):
        return str(part)
    if isinstance(part, str) and part in declared:
        return part
    return UNKNOWN_FIELD


# ------------------------------------------------- what a field's type reads text as
def _names_files_or_code(part: object) -> bool:
    if part is _IMPORT_STRING or isinstance(part, _IMPORT_STRING | PathType):
        return True
    # What ``os.PathLike`` asks of a class: ``Path`` and every other kind of path has it.
    return isinstance(part, type) and hasattr(part, "__fspath__")


def _structures_of(model: type, seen: set[type]) -> Iterator[type]:
    """``model`` and each model, dataclass or typed dict it holds, once each."""
    if model in seen:
        return
    seen.add(model)
    yield model
    for _, parts in _fields_of(model):
        for part in parts:
            if _is_structure(part):
                yield from _structures_of(part, seen)


def _is_structure(part: object) -> bool:
    return isinstance(part, type) and (
        issubclass(part, BaseModel) or dataclasses.is_dataclass(part) or is_typeddict(part)
    )


def _fields_of(structure: type) -> Iterator[tuple[str, list[Any]]]:
    """The fields of a model, dataclass or typed dict, each with every part its type is made of."""
    if issubclass(structure, BaseModel):
        for name, info in structure.model_fields.items():
            yield name, [*_parts_of(info.annotation, set()), *info.metadata]
        return
    try:
        hints = get_type_hints(structure, include_extras=True)
    except (NameError, TypeError):
        # The library refuses a type it cannot read when it builds the schema, which is asked
        # as well: this is not where that is said.
        return
    for name, annotation in hints.items():
        yield name, list(_parts_of(annotation, set()))


def _parts_of(annotation: Any, aliases: set[TypeAliasType]) -> Iterator[Any]:
    """``annotation`` and everything it is made of: aliases looked through, arguments, metadata."""
    yield annotation
    if isinstance(annotation, TypeAliasType):
        if annotation not in aliases:
            aliases.add(annotation)
            yield from _parts_of(annotation.__value__, aliases)
        return
    for argument in get_args(annotation):
        yield from _parts_of(argument, aliases)
