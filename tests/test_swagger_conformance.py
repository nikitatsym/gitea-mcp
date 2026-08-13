"""Conformance check: every hand-written API call matches Gitea's swagger spec.

Gitea silently ignores query params and body fields it does not know, so a
misspelled name is an invisible bug: the call still returns 200 and the filter
simply never applies (`milestone` where the API wants `milestones` was found
that way). Neither the type system nor the integration suite can see that class
of typo, so this test reads every registered op in `gitea_mcp.tools` off its own
AST and asserts each call's method, path, query-param names and body-field names
against the live pinned instance's `swagger.v1.json`.
"""

from __future__ import annotations

import ast
import functools
import inspect
import re
import textwrap
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, TypeGuard

import httpx
import pytest

from gitea_mcp import tools

pytestmark = pytest.mark.integration

# Ops whose call shape the extractor below cannot read. ONLY code shapes belong
# here - never a name mismatch, which is the whole point of this test.
UNANALYZABLE_OK: dict[str, str] = {
    "create_org_webhook": "body comes from the _hook_body() helper, not a literal or _body()",
    "create_repo_webhook": "body comes from the _hook_body() helper, not a literal or _body()",
    "create_user_access_token": "POST goes through _basic_auth_request (raw basic-auth httpx); names hand-checked against CreateAccessTokenOption",
    "list_repo_refs": "path is assembled conditionally into a local variable",
}

# Ops with no wire call of their own: they only drive other registered ops,
# whose calls are checked in their own right.
NO_WIRE_CALL_OK: frozenset[str] = frozenset({
    "waits_list",
    "workflow_jobs_wait",
    "workflow_jobs_wait_cancel",
    "workflow_jobs_wait_poll",
    "workflow_jobs_wait_start",
    "workflow_runs_wait",
    "workflow_runs_wait_cancel",
    "workflow_runs_wait_poll",
    "workflow_runs_wait_start",
})

_PLACEHOLDER = re.compile(r"\{(\w+)\}")

# Ops whose endpoint is deliberately absent from the pinned instance's spec.
# Reviewed entries only; the conformance test fails if the endpoint reappears.
SPEC_GAPS: dict[str, str] = {
    "get_nodeinfo": "gone from the spec and a 501 stub since Gitea 1.26; kept for older live instances",
}

# Client verb -> HTTP method. `paginate` is a GET that also injects limit/page.
_CLIENT_VERBS = {
    "get": "GET",
    "get_text": "GET",
    "paginate": "GET",
    "post": "POST",
    "put": "PUT",
    "patch": "PATCH",
    "delete": "DELETE",
}
_RAW_VERBS = {"_json", "_text"}
_NO_PAYLOAD_KWARGS = {"headers"}

# Call plumbing the extractor already models; everything else that reaches
# these markers inside a helper is a wire call hiding from the check.
_PLUMBING = frozenset({"_call", "_body", "_get_client", "locals"})
_WIRE_MARKER = re.compile(r"_get_client\(|httpx\.|\b_call\(")


@functools.cache
def _hits_wire(target: Callable[..., Any]) -> bool:
    # getsource failing here is a loud test error by design: an unreadable
    # helper cannot be assumed clean.
    return bool(_WIRE_MARKER.search(inspect.getsource(target)))


@dataclass(frozen=True)
class _WireCall:
    """One outbound HTTP call, as read off the source of an op."""

    method: str
    path: str
    query: frozenset[str]
    body: frozenset[str]


# Stand-in for a call the extractor gave up on; dropped with the rest once the
# op is marked unreadable.
_UNREADABLE = _WireCall("", "", frozenset(), frozenset())


def _is_named(node: ast.expr | None, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _is_call_to(node: ast.expr | None, name: str) -> TypeGuard[ast.Call]:
    return isinstance(node, ast.Call) and _is_named(node.func, name)


# -- AST extraction ---------------------------------------------------------


class _OpExtractor:
    """Reads the wire calls an op makes straight off its source.

    Every shape outside the grammar records a reason in `blocked` and the op is
    reported as unanalyzable rather than half-checked.
    """

    def __init__(self, fn: Callable[..., Any]) -> None:
        self.params = list(inspect.signature(fn).parameters)
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        self.stmts: list[ast.stmt] = tree.body[0].body  # type: ignore[attr-defined]
        self.blocked: str | None = None

    def calls(self) -> list[_WireCall]:
        found = []
        for node in self._walk():
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if _is_named(fn, "_call"):
                found.append(self._from_call_helper(node))
            elif isinstance(fn, ast.Attribute) and _is_call_to(fn.value, "_get_client"):
                found.append(self._from_client(node, fn.attr))
            elif isinstance(fn, ast.Name):
                self._check_helper(fn.id)
        return [] if self.blocked else found

    def _check_helper(self, name: str) -> None:
        """Block ops whose helpers hit the wire where this test cannot see."""
        if name in _PLUMBING:
            return
        target = getattr(tools, name, None)
        # Registered ops a waiter drives are checked in their own right.
        if not inspect.isfunction(target) or hasattr(target, "_mcp_group"):
            return
        if _hits_wire(target):
            self._block(f"calls {name}(), which makes HTTP calls this extractor cannot read")

    def _new_binding_before(self, call: ast.Call) -> bool:
        """True if a non-parameter name is bound before `call` captures locals().

        A rebind of a signature parameter keeps the locals() key set intact, and
        a binding whose statement contains `call` itself completes only after
        locals() is read - both are safe. Everything else could smuggle an
        extra name onto the wire.
        """
        for stmt in self.stmts:
            for node in ast.walk(stmt):
                if isinstance(node, ast.NamedExpr):
                    return True
                new_binding = False
                if isinstance(node, ast.Assign):
                    names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                    new_binding = len(names) != len(node.targets) or not all(
                        n in self.params for n in names
                    )
                elif isinstance(node, ast.AnnAssign):
                    new_binding = not (
                        isinstance(node.target, ast.Name) and node.target.id in self.params
                    )
                elif isinstance(node, (ast.For, ast.With, ast.AugAssign)):
                    new_binding = True
                if new_binding and (
                    node.end_lineno or node.lineno,
                    node.end_col_offset or 0,
                ) < (call.lineno, call.col_offset):
                    return True
        return False

    def _walk(self) -> Iterator[ast.AST]:
        for stmt in self.stmts:
            yield from ast.walk(stmt)

    def _block(self, reason: str) -> None:
        if self.blocked is None:
            self.blocked = reason

    # -- literals -----------------------------------------------------------

    def _const_str(self, node: ast.expr | None) -> str:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        self._block(f"expected a string literal, found {type(node).__name__}")
        return ""

    def _str_tuple(self, node: ast.expr) -> set[str]:
        if isinstance(node, (ast.Tuple, ast.List)):
            return {self._const_str(e) for e in node.elts}
        self._block("exclude= is not a literal tuple")
        return set()

    def _str_dict(self, node: ast.expr) -> dict[str, str]:
        if isinstance(node, ast.Dict):
            return {self._const_str(k): self._const_str(v) for k, v in zip(node.keys, node.values)}
        self._block("rename= is not a literal dict")
        return {}

    def _dict_keys(self, node: ast.Dict) -> set[str]:
        if any(k is None for k in node.keys):
            self._block("dict literal uses ** unpacking")
            return set()
        return {self._const_str(k) for k in node.keys}

    # -- name derivation ----------------------------------------------------

    def _forwarded(self, drop: set[str], node: ast.Call) -> set[str]:
        """Signature params that reach the wire, after exclude= and rename=."""
        excluded = set(drop)
        renames: dict[str, str] = {}
        for kw in node.keywords:
            if kw.arg == "exclude":
                excluded |= self._str_tuple(kw.value)
            elif kw.arg == "rename":
                renames = self._str_dict(kw.value)
            elif kw.arg != "keep_null":  # keep_null changes values, not names
                self._block(f"unsupported keyword {kw.arg!r}")
        return {renames.get(p, p) for p in self.params if p not in excluded}

    def _body_call_names(self, node: ast.Call) -> set[str]:
        if not node.args or not _is_call_to(node.args[0], "locals"):
            self._block("_body() is not called on locals()")
            return set()
        if self._new_binding_before(node):
            self._block("a local is bound before _body(locals()) reads the frame")
            return set()
        return self._forwarded(set(), node)

    def _payload_names(self, value: ast.expr | None) -> set[str]:
        if value is None or (isinstance(value, ast.Constant) and value.value is None):
            return set()
        if isinstance(value, ast.Dict):
            return self._dict_keys(value)
        if isinstance(value, ast.Name):
            return self._resolve_dict_var(value.id)
        if isinstance(value, ast.BoolOp) and isinstance(value.op, ast.Or):
            names: set[str] = set()
            for alt in value.values:
                if not (isinstance(alt, ast.Constant) and alt.value is None):
                    names |= self._payload_names(alt)
            return names
        if _is_call_to(value, "_body"):
            return self._body_call_names(value)
        self._block(f"payload is a {type(value).__name__}, not a readable dict")
        return set()

    def _resolve_dict_var(self, name: str) -> set[str]:
        """Union of the keys a local dict variable can end up carrying."""
        keys: set[str] = set()
        assigned = False
        for node in self._walk():
            targets: list[ast.expr] = []
            value: ast.expr | None = None
            if isinstance(node, ast.Assign):
                targets, value = list(node.targets), node.value
            elif isinstance(node, ast.AnnAssign):
                targets, value = [node.target], node.value
            elif isinstance(node, ast.AugAssign) and _is_named(node.target, name):
                self._block(f"augmented assignment to {name!r}")
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and _is_named(node.func.value, name)
            ):
                self._block(f"{name}.{node.func.attr}() mutates the payload")
            for target in targets:
                if _is_named(target, name):
                    keys |= self._initial_keys(value)
                    assigned = True
                elif isinstance(target, ast.Subscript) and _is_named(target.value, name):
                    keys.add(self._const_str(target.slice))
        if not assigned:
            self._block(f"no assignment to {name!r} found in the op")
        return keys

    def _initial_keys(self, value: ast.expr | None) -> set[str]:
        if isinstance(value, ast.Dict):
            return self._dict_keys(value)
        if _is_call_to(value, "_body"):
            return self._body_call_names(value)
        self._block(f"dict built from a {type(value).__name__}, not a literal")
        return set()

    # -- call shapes --------------------------------------------------------

    def _from_call_helper(self, node: ast.Call) -> _WireCall:
        if len(node.args) != 3 or not _is_call_to(node.args[2], "locals"):
            self._block("_call() is not shaped (method, path, locals())")
            return _UNREADABLE
        if self._new_binding_before(node):
            self._block("a local is bound before _call(locals()) reads the frame")
            return _UNREADABLE
        method = self._const_str(node.args[0]).upper()
        path = self._const_str(node.args[1])
        names = frozenset(self._forwarded(set(_PLACEHOLDER.findall(path)), node))
        if method in ("GET", "DELETE"):
            return _WireCall(method, path, names, frozenset())
        return _WireCall(method, path, frozenset(), names)

    def _from_client(self, node: ast.Call, verb: str) -> _WireCall:
        args = list(node.args)
        if verb in _RAW_VERBS:
            if len(args) < 2:
                self._block(f"{verb}() called without (method, path)")
                return _UNREADABLE
            method, path, extra = self._const_str(args[0]).upper(), self._path(args[1]), args[2:]
        elif verb in _CLIENT_VERBS:
            if not args:
                self._block(f"{verb}() called without a path")
                return _UNREADABLE
            method, path, extra = _CLIENT_VERBS[verb], self._path(args[0]), args[1:]
        else:
            self._block(f"unknown client method {verb!r}")
            return _UNREADABLE
        if extra:
            self._block(f"{verb}() passes a payload positionally")
            return _UNREADABLE
        query: set[str] = set()
        body: set[str] = set()
        for kw in node.keywords:
            if kw.arg == "params":
                query |= self._payload_names(kw.value)
            elif kw.arg == "json":
                body |= self._payload_names(kw.value)
            elif kw.arg not in _NO_PAYLOAD_KWARGS:
                self._block(f"{verb}() carries a payload in {kw.arg!r}")
        if verb == "paginate":
            query |= {"limit", "page"}
        return _WireCall(method, path, frozenset(query), frozenset(body))

    def _path(self, node: ast.expr) -> str:
        if isinstance(node, ast.Constant):
            return self._const_str(node)
        if not isinstance(node, ast.JoinedStr):
            self._block(f"path is a {type(node).__name__}, not a literal")
            return ""
        parts = []
        for piece in node.values:
            if isinstance(piece, ast.Constant):
                parts.append(str(piece.value))
            elif isinstance(piece, ast.FormattedValue) and isinstance(piece.value, ast.Name):
                parts.append("{" + piece.value.id + "}")
            else:
                self._block("path f-string interpolates an expression")
                return ""
        return "".join(parts)


@dataclass(frozen=True)
class _Ops:
    analyzed: dict[str, list[_WireCall]]
    unanalyzable: dict[str, str]
    no_wire_call: list[str]


@functools.lru_cache(maxsize=1)
def _extract_ops() -> _Ops:
    analyzed: dict[str, list[_WireCall]] = {}
    unanalyzable: dict[str, str] = {}
    no_wire_call: list[str] = []
    members = inspect.getmembers(
        tools, lambda o: inspect.isfunction(o) and hasattr(o, "_mcp_group")
    )
    for name, fn in sorted(members):
        extractor = _OpExtractor(fn)
        calls = extractor.calls()
        if extractor.blocked:
            unanalyzable[name] = extractor.blocked
        elif calls:
            analyzed[name] = calls
        else:
            no_wire_call.append(name)  # waiter machinery, delegates to other ops
    return _Ops(analyzed, unanalyzable, no_wire_call)


# -- Swagger index ----------------------------------------------------------


_PLACEHOLDER_PART = re.compile(r"^\{\w+\}$")


def _segments(path: str) -> list[list[str | None]]:
    """Split into /-segments, then .-parts; placeholder parts become None."""
    return [
        [None if _PLACEHOLDER_PART.match(part) else part for part in seg.split(".")]
        for seg in path.strip("/").split("/")
    ]


def _matches(
    ours: list[list[str | None]], spec: list[list[str | None]], *, strict: bool
) -> bool:
    """A spec placeholder part accepts anything; our placeholder needs one.

    Strict mode compares part-by-part within each segment, which tells
    `{sha}.diff` apart from a bare `{sha}` while letting it match
    `{sha}.{diffType}`. Lenient mode additionally lets a whole-segment spec
    placeholder swallow a composite segment, so `{base}...{head}` matches the
    spec's `{basehead}`.
    """
    if len(ours) != len(spec):
        return False
    for our_seg, spec_seg in zip(ours, spec):
        if not strict and spec_seg == [None]:
            continue
        if len(our_seg) != len(spec_seg):
            return False
        for our_part, spec_part in zip(our_seg, spec_seg):
            if spec_part is not None and our_part != spec_part:
                return False
    return True


class _Swagger:
    """Query/body name sets per (path template, method), matched structurally."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self._defs: dict[str, Any] = spec.get("definitions") or {}
        self.endpoints: dict[tuple[str, str], tuple[frozenset[str], frozenset[str]]] = {}
        self._paths: set[str] = set(spec["paths"])
        self._templates: list[tuple[str, list[list[str | None]]]] = [
            (path, _segments(path)) for path in spec["paths"]
        ]
        for path, item in spec["paths"].items():
            for method, operation in item.items():
                if not isinstance(operation, dict):
                    continue
                query: set[str] = set()
                body: set[str] = set()
                for param in operation.get("parameters") or []:
                    where = param.get("in")
                    if where == "query":
                        query.add(param["name"])
                    elif where == "formData":
                        body.add(param["name"])
                    elif where == "body":
                        body |= self._properties(param.get("schema") or {})
                self.endpoints[path, method.upper()] = (frozenset(query), frozenset(body))

    def _properties(self, schema: dict[str, Any], depth: int = 0) -> set[str]:
        if depth > 8 or not isinstance(schema, dict):
            return set()
        ref = schema.get("$ref")
        if ref:
            return self._properties(self._defs.get(ref.rsplit("/", 1)[-1]) or {}, depth + 1)
        names = set(schema.get("properties") or {})
        for member in schema.get("allOf") or []:
            names |= self._properties(member, depth + 1)
        return names

    def _pool(self, path: str) -> list[str]:
        # An exact template match is unambiguous by construction; structural
        # matching is the fallback for paths whose placeholders differ.
        if path in self._paths:
            return [path]
        ours = _segments(path)
        # Strict wins so `{sha}.diff` resolves to `{sha}.{diffType}`, not `{sha}`.
        for strict in (True, False):
            pool = [
                p
                for p, template in self._templates
                if _matches(ours, template, strict=strict)
            ]
            if pool:
                return pool
        return []

    def candidates(self, path: str, method: str) -> list[str]:
        return [p for p in self._pool(path) if (p, method) in self.endpoints]

    def describe_pool(self, path: str) -> str:
        known = sorted(
            f"{m} {p}" for p in self._pool(path) for (p2, m) in self.endpoints if p2 == p
        )
        return ", ".join(known) if known else "nothing with this path shape"


@pytest.fixture(scope="session")
def swagger(gitea_instance: str) -> _Swagger:
    """The pinned instance's own spec. Served at the server root, not under /api/v1."""
    response = httpx.get(f"{gitea_instance}/swagger.v1.json", timeout=60.0)
    response.raise_for_status()
    return _Swagger(response.json())


# -- Tests ------------------------------------------------------------------


def test_every_unanalyzable_op_is_allowlisted() -> None:
    unknown = {
        name: reason
        for name, reason in _extract_ops().unanalyzable.items()
        if name not in UNANALYZABLE_OK
    }
    assert not unknown, (
        "Ops whose calls this test cannot read are missing from UNANALYZABLE_OK. "
        "Reshape the op into a readable form, teach the extractor the shape, or "
        "allowlist it - code shapes only, NEVER a name mismatch:\n"
        + "\n".join(f"  {name}: {reason}" for name, reason in sorted(unknown.items()))
    )


def test_allowlist_has_no_stale_entries() -> None:
    stale = sorted(set(UNANALYZABLE_OK) - set(_extract_ops().unanalyzable))
    assert not stale, (
        "These ops are analyzable now - drop them from UNANALYZABLE_OK so the "
        f"allowlist can only shrink: {stale}"
    )
    orphaned = sorted(set(SPEC_GAPS) - set(_extract_ops().analyzed))
    assert not orphaned, f"SPEC_GAPS names ops with no analyzed wire call: {orphaned}"


def test_no_wire_call_ops_are_expected() -> None:
    ops = _extract_ops()
    unexpected = sorted(set(ops.no_wire_call) - NO_WIRE_CALL_OK)
    assert not unexpected, (
        "Ops with no readable wire call of their own. If they truly only "
        f"drive other registered ops, add them to NO_WIRE_CALL_OK: {unexpected}"
    )
    stale = sorted(NO_WIRE_CALL_OK - set(ops.no_wire_call))
    assert not stale, f"NO_WIRE_CALL_OK entries no longer match reality: {stale}"


def test_wire_calls_match_swagger(swagger: _Swagger) -> None:
    findings: list[str] = []
    for op, calls in sorted(_extract_ops().analyzed.items()):
        for call in calls:
            where = f"{op}: {call.method} {call.path}"
            matches = swagger.candidates(call.path, call.method)
            if op in SPEC_GAPS:
                if matches:
                    findings.append(
                        f"{where}: endpoint is back in the spec - drop it from SPEC_GAPS"
                    )
                continue
            if not matches:
                findings.append(
                    f"{where}: no such endpoint in the spec; it has "
                    f"{swagger.describe_pool(call.path)}"
                )
                continue
            if len(matches) > 1:
                findings.append(f"{where}: ambiguous, matches spec paths {matches}")
                continue
            allowed_query, allowed_body = swagger.endpoints[matches[0], call.method]
            bad_query = sorted(call.query - allowed_query)
            if bad_query:
                findings.append(
                    f"{where}: query params {bad_query} are not in the spec; "
                    f"it accepts {sorted(allowed_query)}"
                )
            bad_body = sorted(call.body - allowed_body)
            if bad_body:
                findings.append(
                    f"{where}: body fields {bad_body} are not in the spec; "
                    f"it accepts {sorted(allowed_body)}"
                )
    assert not findings, (
        f"{len(findings)} call(s) disagree with the Gitea spec. Gitea drops "
        "unknown names silently, so each of these is a request that quietly "
        "does not do what it says:\n" + "\n".join(f"  {f}" for f in findings)
    )
