"""Gitea MCP server — auto-discovery, Pydantic validation, schema introspection, dispatch."""

from __future__ import annotations

import inspect
import types
import typing
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, ValidationError, create_model

from . import tools as _tools_module
from .client import GiteaError
from .registry import ROOT

mcp = FastMCP("gitea")


# ── Helpers ──────────────────────────────────────────────────────────────────


def _to_pascal(name: str) -> str:
    """get_today → GetToday"""
    return "".join(w.capitalize() for w in name.split("_"))


def _build_params_model(fn) -> type[BaseModel]:
    """Build a Pydantic model from a function's signature.

    - Parameters without a default become required fields (missing → error).
    - `Annotated[T, Field(description=..., ...)]` is honored — description and
      constraints flow into the generated JSON Schema.
    - `extra='forbid'` — unknown keys are rejected at validation time.
    """
    hints = typing.get_type_hints(fn, include_extras=True)
    sig = inspect.signature(fn)
    fields: dict[str, Any] = {}
    for name, param in sig.parameters.items():
        ann = hints.get(name, Any)
        if param.default is inspect.Parameter.empty:
            fields[name] = (ann, ...)
        else:
            fields[name] = (ann, param.default)
    return create_model(
        f"{_to_pascal(fn.__name__)}Params",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )


def _format_validation_error(err: ValidationError, op_name: str) -> str:
    """Pydantic ValidationError → readable multi-line message."""
    lines = [f"Invalid params for {op_name}:"]
    for e in err.errors():
        loc = ".".join(str(x) for x in e["loc"]) or "<root>"
        msg = e["msg"]
        got = repr(e.get("input"))
        if len(got) > 80:
            got = got[:77] + "..."
        lines.append(f"  - {loc}: {msg} (got {got})")
    lines.append(f"Call operation='schema', params={{'op': {op_name!r}}} for full parameter spec.")
    return "\n".join(lines)


def _coerce_call(fn, params: dict, op_name: str):
    """Validate params via the function's Pydantic model, then call fn.

    Field-level type mismatches, missing required fields, and unknown keys all
    raise ValueError pointing at the offending field. No silent coercion of
    unrelated types — Pydantic 2 default mode coerces only sane conversions
    (e.g. numeric str → int) and rejects nonsense (e.g. 'frontend' → int).
    """
    model: type[BaseModel] = fn._params_model
    try:
        validated = model.model_validate(params)
    except ValidationError as e:
        raise ValueError(_format_validation_error(e, op_name)) from e
    return fn(**validated.model_dump(exclude_unset=True))


# ── Type rendering for help text ─────────────────────────────────────────────


def _type_to_str(hint) -> str:
    """Compact human-readable rendering of a type hint for help text.

    Optional[T] is rendered as just `T` — the `?` marker in the parameter name
    already conveys "may be omitted". Use `T|None` explicitly if you need to
    convey that the value itself can be null.
    """
    if hint is type(None):
        return "None"
    if hint is Any:
        return "any"
    origin = typing.get_origin(hint)
    args = typing.get_args(hint)
    if origin is typing.Literal:
        return "|".join(repr(a) for a in args)
    if origin is typing.Union or isinstance(hint, types.UnionType):
        non_none = [a for a in args if a is not type(None)]
        return "|".join(_type_to_str(a) for a in non_none) or "any"
    if origin is list:
        return f"list[{_type_to_str(args[0])}]" if args else "list"
    if origin is dict:
        if len(args) == 2:
            return f"dict[{_type_to_str(args[0])},{_type_to_str(args[1])}]"
        return "dict"
    if origin is tuple:
        return f"tuple[{','.join(_type_to_str(a) for a in args)}]" if args else "tuple"
    if hasattr(hint, "__name__"):
        return hint.__name__
    return str(hint).replace("typing.", "")


def _format_param_for_help(name: str, field) -> str:
    """Render one parameter line: `name: type` (required) or `name?: type[=default]`."""
    type_str = _type_to_str(field.annotation)
    if field.is_required():
        return f"{name}: {type_str}"
    default = field.default
    if default is None:
        return f"{name}?: {type_str}"
    return f"{name}?: {type_str}={default!r}"


# ── Module-level state (populated by _register_tools) ────────────────────────

_group_ops: dict[str, dict[str, Any]] = {}    # {group_name: {PascalName: fn}}
_all_grouped: dict[str, str] = {}             # {PascalName: group_name}


def _build_help(group_name: str) -> str:
    """One line per operation with parameter names + types."""
    ops = _group_ops[group_name]
    lines = []
    for pascal_name, fn in ops.items():
        model: type[BaseModel] = fn._params_model
        parts = [
            _format_param_for_help(n, f)
            for n, f in model.model_fields.items()
        ]
        doc = inspect.getdoc(fn) or ""
        head = doc.split("\n", 1)[0].strip()
        lines.append(f"  {pascal_name}({', '.join(parts)}) — {head}")
    header = (
        f"{len(ops)} operations available. "
        f"Call operation='schema', params={{'op': 'OpName'}} for full JSON Schema "
        "(descriptions, constraints, enums)."
    )
    return f"{header}\n" + "\n".join(lines)


def _build_schema(group_name: str, op_name: str | None) -> dict:
    """JSON Schema for one op (params={'op': 'X'}) or list of op names (params={})."""
    ops = _group_ops[group_name]
    if op_name is None:
        return {
            "operations": sorted(ops.keys()),
            "hint": f"Pass params={{'op': '<OpName>'}} to get the full JSON Schema.",
        }
    if op_name not in ops:
        raise ValueError(
            f"Unknown operation {op_name!r} in {group_name}. "
            f"Available: {sorted(ops)}"
        )
    fn = ops[op_name]
    model: type[BaseModel] = fn._params_model
    schema = model.model_json_schema()
    doc = inspect.getdoc(fn) or ""
    if doc:
        schema["description"] = doc
    return schema


def _dispatch(operation: str, group_name: str, params: dict):
    """Route an operation call: schema/help-aware, fails loud on anything wrong."""
    if operation == "schema":
        return _build_schema(group_name, params.get("op"))
    ops = _group_ops[group_name]
    if operation not in ops:
        if operation in _all_grouped:
            correct = _all_grouped[operation]
            raise ValueError(
                f"{operation!r} belongs to {correct!r}, not {group_name!r}. "
                f"Call {correct}(operation={operation!r}, ...) instead."
            )
        raise ValueError(
            f"Unknown operation {operation!r} in {group_name}. "
            "Use operation='help' to list or operation='schema' for details."
        )
    return _coerce_call(ops[operation], params, operation)


# ── Registration ─────────────────────────────────────────────────────────────


def _register_tools():
    """Discover @_op-decorated functions, build Pydantic models, register MCP tools."""
    groups: dict[str, tuple] = {}

    for name, fn in inspect.getmembers(_tools_module, inspect.isfunction):
        if not hasattr(fn, "_mcp_group"):
            continue
        fn._params_model = _build_params_model(fn)
        group = fn._mcp_group
        if group is ROOT:
            mcp.tool()(fn)
        else:
            if group.name not in groups:
                groups[group.name] = (group, {})
            groups[group.name][1][name] = fn

    for group_name, (group, fns) in groups.items():
        ops = {_to_pascal(n): fn for n, fn in fns.items()}
        _group_ops[group_name] = ops
        for pascal_name in ops:
            _all_grouped[pascal_name] = group_name

        def _make_tool(gname, gdoc):
            def tool_fn(operation: str, params: dict = {}):
                if operation == "help":
                    return _build_help(gname)
                return _dispatch(operation, gname, params)
            tool_fn.__name__ = gname
            tool_fn.__qualname__ = gname
            tool_fn.__doc__ = gdoc
            return tool_fn

        mcp.tool()(_make_tool(group_name, group.doc))


_register_tools()
