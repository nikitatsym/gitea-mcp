"""Conformance check: every hand-written API call matches Gitea's swagger spec.

Gitea silently ignores query params and body fields it does not know, so a
misspelled name is an invisible bug: the call still returns 200 and the filter
simply never applies (`milestone` where the API wants `milestones` was found
that way). Neither the type system nor the integration suite can see that class
of typo, so this test reads every registered op in `gitea_mcp.tools` off its own
AST and asserts each call's method, path, query-param names, body-field names,
and body requiredness against the live pinned instance's `swagger.v1.json`.

Waiving a shape waives only what the reader could not see. An op that hides one
call behind an expression the extractor cannot read still has its other calls
checked, and an unreadable payload does not stop that call's method and path
from being checked.

Three checks the siblings of this test carry are deliberately absent, because
Gitea's spec or this package's idioms leave them nothing to act on. Each is
replaced by a guard that fails once the assumption stops holding:

- readOnly body properties are not dropped from the accepted field set: the
  spec marks no property readOnly (see
  `test_spec_marks_no_body_property_read_only`);
- a raw body is not told apart from a JSON object: no endpoint declares a
  schemaless body parameter (see `test_spec_declares_no_schemaless_body`), and
  the client carries no raw-body kwarg - an op reaching for one blocks as a
  payload in an unknown keyword;
- a payload the caller hands over whole is not modelled: no op forwards a bare
  parameter as its payload, and one that did would block on the missing local
  assignment rather than pass unchecked.

Stated limits of the oracle:

- a helper reached through a value rather than a name (`getattr(mod, name)()`)
  is invisible, while one named anywhere in the op is followed;
- names are resolved against module globals with no lexical-scope analysis, so
  a parameter or local shadowing a wire helper's name reads as that helper.
  That errs towards a waiver on a clean op, never towards a missed request;
- enum checking maps an arg to its wire name best-effort, so an arg transformed
  on the way to the wire is simply not enum-checked.
"""

from __future__ import annotations

import ast
import functools
import inspect
import re
import sys
import textwrap
import typing
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Literal, TypeGuard

import httpx
import pytest

from gitea_mcp import server, tools

pytestmark = pytest.mark.integration

# Shapes the extractor below cannot read, mapped to the exact reasons it
# reports. The reasons are matched entry for entry, so a second unreadable shape
# in an already-waived op still surfaces. ONLY code shapes belong here - never a
# name mismatch, which is the whole point of this test.
UNANALYZABLE_OK: dict[str, tuple[str, ...]] = {
    # The body comes from the _hook_body() helper. Both ops' method and path are
    # still checked; the four field names are hand-checked against CreateHookOption.
    "create_org_webhook": ("payload is a Call, not a readable dict",),
    "create_repo_webhook": ("payload is a Call, not a readable dict",),
    # POST /users/{username}/tokens goes through _basic_auth_request (raw
    # basic-auth httpx), because the endpoint refuses token auth; its names are
    # hand-checked against CreateAccessTokenOption. The op's GET /user is read
    # and checked as usual.
    "create_user_access_token": (
        "calls _basic_auth_request(), which makes HTTP calls this extractor cannot read",
    ),
    # The path is assembled conditionally into a local variable; both variants
    # (/repos/{owner}/{repo}/git/refs[/{ref_type}]) hand-checked against the spec.
    "list_repo_refs": ("path is a Name, not a literal",),
    # The blocking waiters poll through asyncio.to_thread(_fetch_*), so their
    # requests are made by a helper this extractor cannot read. Those helpers hit
    # the same endpoints get_workflow_run / get_workflow_job /
    # list_workflow_run_jobs / get_workflow_job_logs check in their own right.
    "workflow_jobs_wait": (
        "passes around _fetch_job_slim(), which makes HTTP calls this extractor cannot read",
        "passes around _job_log_tail(), which makes HTTP calls this extractor cannot read",
    ),
    "workflow_runs_wait": (
        (
            "passes around _fetch_failed_job_logs(), which makes HTTP calls this "
            "extractor cannot read"
        ),
        (
            "passes around _fetch_run_jobs_slim(), which makes HTTP calls this "
            "extractor cannot read"
        ),
        "passes around _fetch_run_slim(), which makes HTTP calls this extractor cannot read",
    ),
    # Same polling helpers, one indirection further: the op hands the poll and
    # enrichment callables to the background loop.
    "workflow_jobs_wait_start": (
        "passes around _do_job_poll(), which makes HTTP calls this extractor cannot read",
        "passes around _enrich_job_final(), which makes HTTP calls this extractor cannot read",
        "passes around _job_loop(), which makes HTTP calls this extractor cannot read",
    ),
    "workflow_runs_wait_start": (
        "passes around _do_run_poll(), which makes HTTP calls this extractor cannot read",
        "passes around _enrich_run_final(), which makes HTTP calls this extractor cannot read",
        "passes around _run_loop(), which makes HTTP calls this extractor cannot read",
    ),
}

# Ops with no wire call of their own: they only drive the local wait registry,
# whose polling happens in the *_wait_start ops' background task.
NO_WIRE_CALL_OK: frozenset[str] = frozenset({
    "waits_list",
    "workflow_jobs_wait_cancel",
    "workflow_jobs_wait_poll",
    "workflow_runs_wait_cancel",
    "workflow_runs_wait_poll",
})


@dataclass(frozen=True)
class _SpecGap:
    """One call an op deliberately makes to an endpoint the spec does not cover.

    The method and path are pinned, not just the op name: a waiver covering the
    whole op would also excuse a typo in the very path it documents, and would
    hide a second call the op grows later.
    """

    method: str
    path: str
    why: str

    @property
    def call(self) -> str:
        return f"{self.method} {self.path}"


# Reviewed entries only. The tests fail if the endpoint reappears in the spec,
# and if the op stops making exactly this call.
SPEC_GAPS: dict[str, _SpecGap] = {
    "get_nodeinfo": _SpecGap(
        "GET",
        "/nodeinfo",
        "gone from the spec and a 501 stub since Gitea 1.26; kept for older live instances",
    ),
}

_PLACEHOLDER = re.compile(r"\{(\w+)\}")

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

_PACKAGE = tools.__name__.split(".")[0]


def _ours(obj: Any) -> Callable[..., Any] | None:
    """`obj` as a plain function of this package, or None if it is neither.

    Bound methods resolve to their underlying function; anything from outside
    the package is not one of our helpers, and reaching for httpx itself is
    blocked where an op names it.
    """
    fn = getattr(obj, "__func__", obj)
    if not inspect.isfunction(fn):
        return None
    return fn if fn.__module__.split(".")[0] == _PACKAGE else None


@functools.cache
def _hits_wire(target: Callable[..., Any]) -> bool:
    """True if `target` reaches the wire, directly or through another helper.

    Transitive on purpose: the response helpers live in `prepare.py`, so a walk
    that resolved callees in `tools` alone would stop at the import boundary and
    let a request one level further down hide from the extractor completely.
    """
    return _reaches_wire(target, frozenset())


def _reaches_wire(target: Callable[..., Any], seen: frozenset[str]) -> bool:
    """Walk the names `target` mentions, resolving each in its own module.

    A helper handed to something else counts as reached: the request
    `asyncio.to_thread(_fetch_run_slim, ...)` makes is as invisible as an inline
    one. Cycles end the walk on a module-qualified key, so two helpers of the
    same name in different modules do not shadow each other.
    """
    # getsource failing here is a loud test error by design: an unreadable
    # helper cannot be assumed clean.
    source = inspect.getsource(target)
    if _WIRE_MARKER.search(source):
        return True
    module = sys.modules.get(target.__module__)
    seen = seen | {_key(target)}
    for node in ast.walk(ast.parse(textwrap.dedent(source))):
        if not isinstance(node, ast.Name):
            continue
        callee = _ours(getattr(module, node.id, None))
        # Registered ops are checked in their own right.
        if callee is None or _key(callee) in seen or hasattr(callee, "_mcp_group"):
            continue
        if _reaches_wire(callee, seen):
            return True
    return False


def _key(fn: Callable[..., Any]) -> str:
    return f"{fn.__module__}.{fn.__qualname__}"


@dataclass(frozen=True)
class _WireCall:
    """One outbound HTTP call, as read off the source of an op."""

    method: str
    path: str
    query: frozenset[str]
    body: frozenset[str]

    @property
    def endpoint(self) -> str:
        return f"{self.method} {self.path}"


def _is_named(node: ast.expr | None, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _is_call_to(node: ast.expr | None, name: str) -> TypeGuard[ast.Call]:
    return isinstance(node, ast.Call) and _is_named(node.func, name)


# -- AST extraction ---------------------------------------------------------


class _OpExtractor:
    """Reads the wire calls an op makes straight off its source.

    Every shape outside the grammar records a reason in `reasons`, which the op
    has to account for in UNANALYZABLE_OK. What was read alongside it is still
    returned, so waiving a shape never waives the rest of the op.
    """

    def __init__(self, fn: Callable[..., Any]) -> None:
        self.module = sys.modules[fn.__module__]
        self.params = list(inspect.signature(fn).parameters)
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        self.stmts: list[ast.stmt] = tree.body[0].body  # type: ignore[attr-defined]
        self.reasons: list[str] = []
        self.clients = self._client_bindings()

    def calls(self) -> list[_WireCall]:
        """Every call whose method and path could be read, blocked op or not.

        A call is dropped only when its own method or path is unreadable - there
        is then nothing left to assert. A call whose payload alone defeated the
        reader keeps the names that were read: those are real, only the rest is
        unknown.
        """
        found: list[_WireCall] = []
        called: set[int] = set()
        client_attrs: list[ast.Attribute] = []
        receivers: set[int] = set()
        for node in self._walk():
            if isinstance(node, ast.Attribute) and self._is_client(node.value):
                client_attrs.append(node)
                receivers.add(id(node.value))
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            call: _WireCall | None = None
            if isinstance(fn, ast.Attribute) and self._is_client(fn.value):
                called.add(id(fn))
                call = self._from_client(node, fn.attr)
            elif _is_named(fn, "_call"):
                called.add(id(fn))
                call = self._from_call_helper(node)
            elif isinstance(fn, ast.Attribute):
                self._check_attribute(fn)
            elif isinstance(fn, ast.Name):
                called.add(id(fn))
                self._check_called(fn.id)
            if call is not None:
                found.append(call)
        # A client method referenced but never called here is handed to
        # something else, which then makes the request out of sight.
        for attr in client_attrs:
            if id(attr) not in called:
                self._block(f"client method {attr.attr!r} is passed around, not called here")
        self._check_references(called)
        self._check_client_escape(receivers)
        return found

    # -- helpers and clients ------------------------------------------------

    def _client_bindings(self) -> set[str]:
        """Local names bound to the client, so calls through them are still read."""
        names: set[str] = set()
        for node in self._walk():
            targets: list[ast.expr] = []
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                if not _is_call_to(node.value, "_get_client"):
                    continue
                targets = list(node.targets) if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
                else:
                    self._block("the client is bound to something other than a plain name")
        return names

    def _is_client(self, node: ast.expr | None) -> bool:
        return _is_call_to(node, "_get_client") or (
            isinstance(node, ast.Name) and node.id in self.clients
        )

    def _check_client_escape(self, receivers: set[int]) -> None:
        """Block if a bound client is used as anything but a call receiver."""
        for node in self._walk():
            if not (isinstance(node, ast.Name) and node.id in self.clients):
                continue
            if isinstance(node.ctx, ast.Store) or id(node) in receivers:
                continue
            self._block(f"the client bound to {node.id!r} escapes this op")

    def _check_references(self, called: set[int]) -> None:
        """A wire primitive named but not called takes its requests out of sight."""
        for node in self._walk():
            if not (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)):
                continue
            if id(node) not in called:
                self._check_referenced(node.id)

    def _check_attribute(self, fn: ast.Attribute) -> None:
        """Route `<module-or-object>.<attr>()` through the same wire check.

        httpx is called out by name: the wire marker only guards helpers, so an
        op reaching for httpx itself would read as making no call at all.
        """
        if _is_named(fn.value, "httpx"):
            self._block(f"calls httpx.{fn.attr}() directly, bypassing the client")
        elif isinstance(fn.value, ast.Name):
            owner = getattr(self.module, fn.value.id, None)
            self._check_target(getattr(owner, fn.attr, None), f"{fn.value.id}.{fn.attr}", "calls")

    def _check_called(self, name: str) -> None:
        """Plumbing is modelled where it is called; anything else must stay clean."""
        if name not in _PLUMBING:
            self._check_target(getattr(self.module, name, None), name, "calls")

    def _check_referenced(self, name: str) -> None:
        """Same check for a name handed off rather than called - plumbing included.

        The plumbing grammar reads `_call(...)` and `_body(locals())` in place;
        `wire = _call` moves the request behind a local this extractor does not
        follow, so the modelling no longer holds.
        """
        if name in _PLUMBING:
            self._block(f"{name}() is passed around, not called here")
            return
        self._check_target(getattr(self.module, name, None), name, "passes around")

    def _check_target(self, obj: Any, label: str, how: str) -> None:
        """Block ops whose helpers hit the wire where this test cannot see."""
        target = _ours(obj)
        # Registered ops another op drives are checked in their own right.
        if target is None or hasattr(target, "_mcp_group"):
            return
        if _hits_wire(target):
            self._block(f"{how} {label}(), which makes HTTP calls this extractor cannot read")

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
        # Every occurrence is kept, not just every distinct message: a second
        # unreadable shape that happens to read the same way is a second hole,
        # and an allowlist entry that swallowed it would waive the new one too.
        self.reasons.append(reason)

    def _unreadable_since(self, mark: int) -> bool:
        return len(self.reasons) != mark

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
            elif isinstance(node, ast.Delete) and any(
                isinstance(t, ast.Subscript) and _is_named(t.value, name) for t in node.targets
            ):
                self._block(f"del {name}[...] drops a field this test would still count")
            elif isinstance(node, ast.Call):
                self._check_payload_escape(name, node)
            if targets:
                self._check_alias(name, value)
            for target in targets:
                if _is_named(target, name):
                    keys |= self._initial_keys(value)
                    assigned = True
                elif isinstance(target, ast.Subscript) and _is_named(target.value, name):
                    keys.add(self._const_str(target.slice))
        if not assigned:
            self._block(f"no assignment to {name!r} found in the op")
        return keys

    def _check_alias(self, name: str, value: ast.expr | None) -> None:
        """A second handle on the payload can add keys where `name` never appears.

        `mutate = body.update` is the same escape as `other = body`: any
        attribute taken off the payload carries it, bound method or not.
        """
        if _is_named(value, name):
            self._block(f"{name!r} is aliased to another local")
        elif isinstance(value, ast.Attribute) and _is_named(value.value, name):
            self._block(f"{name}.{value.attr} is bound to another local")

    def _check_payload_escape(self, name: str, node: ast.Call) -> None:
        """A tracked payload another call can reach may gain keys off-screen."""
        if isinstance(node.func, ast.Attribute) and _is_named(node.func.value, name):
            self._block(f"{name}.{node.func.attr}() mutates the payload")
            return
        # The wire call is where the payload is meant to go; anything else could
        # put a name on it that this test never sees.
        if isinstance(node.func, ast.Attribute) and self._is_client(node.func.value):
            return
        arguments = [*node.args, *(kw.value for kw in node.keywords)]
        if any(_is_named(argument, name) for argument in arguments):
            self._block(f"{name!r} is handed to another call, which could add keys")

    def _initial_keys(self, value: ast.expr | None) -> set[str]:
        if isinstance(value, ast.Dict):
            return self._dict_keys(value)
        if _is_call_to(value, "_body"):
            return self._body_call_names(value)
        self._block(f"dict built from a {type(value).__name__}, not a literal")
        return set()

    # -- call shapes --------------------------------------------------------

    def _from_call_helper(self, node: ast.Call) -> _WireCall | None:
        if len(node.args) != 3 or not _is_call_to(node.args[2], "locals"):
            self._block("_call() is not shaped (method, path, locals())")
            return None
        mark = len(self.reasons)
        method = self._const_str(node.args[0]).upper()
        path = self._const_str(node.args[1])
        if self._unreadable_since(mark):
            return None
        if self._new_binding_before(node):
            # The signature params are a sound subset of what locals() sends, so
            # they are still worth checking; the extra names an early binding
            # could smuggle in are exactly what the recorded reason waives.
            self._block("a local is bound before _call(locals()) reads the frame")
        names = frozenset(self._forwarded(set(_PLACEHOLDER.findall(path)), node))
        if method in ("GET", "DELETE"):
            return _WireCall(method, path, names, frozenset())
        return _WireCall(method, path, frozenset(), names)

    def _from_client(self, node: ast.Call, verb: str) -> _WireCall | None:
        args = list(node.args)
        mark = len(self.reasons)
        if verb in _RAW_VERBS:
            if len(args) < 2:
                self._block(f"{verb}() called without (method, path)")
                return None
            method, path, extra = self._const_str(args[0]).upper(), self._path(args[1]), args[2:]
        elif verb in _CLIENT_VERBS:
            if not args:
                self._block(f"{verb}() called without a path")
                return None
            method, path, extra = _CLIENT_VERBS[verb], self._path(args[0]), args[1:]
        else:
            self._block(f"unknown client method {verb!r}")
            return None
        if self._unreadable_since(mark):
            return None
        if extra:
            self._block(f"{verb}() passes a payload positionally")
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
    """Readable calls per op, plus what defeated the reader where it did."""

    calls: dict[str, list[_WireCall]]
    blocked: dict[str, tuple[str, ...]]
    no_wire_call: list[str]


@functools.lru_cache(maxsize=1)
def _extract_ops() -> _Ops:
    calls: dict[str, list[_WireCall]] = {}
    blocked: dict[str, tuple[str, ...]] = {}
    no_wire_call: list[str] = []
    members = inspect.getmembers(
        tools, lambda o: inspect.isfunction(o) and hasattr(o, "_mcp_group")
    )
    for name, fn in sorted(members):
        extractor = _OpExtractor(fn)
        found = extractor.calls()
        if found:
            calls[name] = found
        if extractor.reasons:
            blocked[name] = tuple(sorted(extractor.reasons))
        elif not found:
            no_wire_call.append(name)  # waiter machinery, delegates to other ops
    return _Ops(calls, blocked, no_wire_call)


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


@dataclass(frozen=True)
class _Endpoint:
    """What one (path, method) of the spec accepts on the wire."""

    query: frozenset[str]
    required_query: frozenset[str]
    query_enums: dict[str, frozenset[str]]
    body: frozenset[str]
    required_body: frozenset[str]
    read_only_body: frozenset[str]
    # True when a body parameter carries no schema at all - a raw upload, whose
    # field names are not the spec's business.
    schemaless_body: bool


class _Swagger:
    """Query/body name sets per (path template, method), matched structurally."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self._defs: dict[str, Any] = spec.get("definitions") or {}
        self.endpoints: dict[tuple[str, str], _Endpoint] = {}
        self._paths: set[str] = set(spec["paths"])
        self._templates: list[tuple[str, list[list[str | None]]]] = [
            (path, _segments(path)) for path in spec["paths"]
        ]
        for path, item in spec["paths"].items():
            for method, operation in item.items():
                if isinstance(operation, dict):
                    self.endpoints[path, method.upper()] = self._endpoint(operation)

    def _endpoint(self, operation: dict[str, Any]) -> _Endpoint:
        query: set[str] = set()
        required_query: set[str] = set()
        enums: dict[str, frozenset[str]] = {}
        body: set[str] = set()
        required_body: set[str] = set()
        read_only: set[str] = set()
        schemaless = False
        for param in operation.get("parameters") or []:
            where = param.get("in")
            if where == "query":
                query.add(param["name"])
                if param.get("required"):
                    required_query.add(param["name"])
                # Formal enums only; prose-documented value sets are not
                # machine-checkable and are skipped.
                values = param.get("enum") or (param.get("items") or {}).get("enum")
                if values:
                    enums[param["name"]] = frozenset(values)
            elif where == "formData":
                body.add(param["name"])
                if param.get("required"):
                    required_body.add(param["name"])
            elif where == "body":
                schema = param.get("schema") or {}
                schemaless = schemaless or not schema
                names, hidden, needed = self._properties(schema)
                body |= names
                required_body |= needed
                read_only |= hidden
        return _Endpoint(
            frozenset(query),
            frozenset(required_query),
            enums,
            frozenset(body),
            frozenset(required_body),
            frozenset(read_only),
            schemaless,
        )

    def _properties(
        self, schema: dict[str, Any], depth: int = 0
    ) -> tuple[set[str], set[str], set[str]]:
        """Declared property names, readOnly names, and required names."""
        if depth > 8 or not isinstance(schema, dict):
            return set(), set(), set()
        ref = schema.get("$ref")
        if ref:
            return self._properties(self._defs.get(ref.rsplit("/", 1)[-1]) or {}, depth + 1)
        declared: dict[str, Any] = schema.get("properties") or {}
        names: set[str] = set(declared)
        required: set[str] = set(schema.get("required") or ())
        read_only: set[str] = {
            name for name, prop in declared.items()
            if isinstance(prop, dict) and prop.get("readOnly")
        }
        for member in schema.get("allOf") or []:
            more, more_read_only, more_required = self._properties(member, depth + 1)
            names |= more
            read_only |= more_read_only
            required |= more_required
        return names, read_only, required

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


def _is_spec_gap(op: str, call: _WireCall) -> bool:
    gap = SPEC_GAPS.get(op)
    return gap is not None and (gap.method, gap.path) == (call.method, call.path)


def _checked_calls() -> Iterator[tuple[str, _WireCall]]:
    """Every readable call whose endpoint is expected to be in the spec."""
    for op, calls in sorted(_extract_ops().calls.items()):
        yield from ((op, call) for call in calls if not _is_spec_gap(op, call))


def _endpoints_we_call(swagger: _Swagger) -> Iterator[tuple[str, _Endpoint]]:
    for _, call in _checked_calls():
        for path in swagger.candidates(call.path, call.method):
            yield f"{call.method} {path}", swagger.endpoints[path, call.method]


# -- Tests ------------------------------------------------------------------


def test_every_exposed_operation_is_checked() -> None:
    """Ops are discovered by an attribute, so renaming the registry marker would
    silently empty this whole suite. Anchor discovery to what the server exposes."""
    ops = _extract_ops()
    discovered = set(ops.calls) | set(ops.blocked) | set(ops.no_wire_call)
    covered = {server._to_pascal(name) for name in discovered}
    # Group meta-tools dispatch to grouped ops; anything else registered
    # directly is a ROOT op and is exposed under its own name.
    root_tools = set(server.mcp._tool_manager._tools) - set(server._group_ops)
    exposed = {op for group in server._group_ops.values() for op in group} | {
        server._to_pascal(name) for name in root_tools
    }
    missing = sorted(exposed - covered)
    assert exposed and not missing, (
        f"{len(missing)} operation(s) the server exposes are invisible to this "
        f"conformance test: {missing}"
    )


def test_every_unreadable_shape_is_allowlisted() -> None:
    """The reasons have to match entry for entry, not just the op name."""
    unknown = {
        name: reasons
        for name, reasons in _extract_ops().blocked.items()
        if UNANALYZABLE_OK.get(name) != reasons
    }
    assert not unknown, (
        "Shapes this test cannot read are missing from UNANALYZABLE_OK, or the "
        "recorded reasons no longer match. Reshape the op into a readable form, "
        "teach the extractor the shape, or allowlist it with its exact reasons - "
        "code shapes only, NEVER a name mismatch:\n"
        + "\n".join(f"  {name}: {list(reasons)}" for name, reasons in sorted(unknown.items()))
    )


def test_allowlists_have_no_stale_entries() -> None:
    ops = _extract_ops()
    stale = sorted(set(UNANALYZABLE_OK) - set(ops.blocked))
    assert not stale, (
        "These ops read cleanly now - drop them from UNANALYZABLE_OK so the "
        f"allowlist can only shrink: {stale}"
    )
    orphaned = sorted(
        f"{op}: {gap.call}"
        for op, gap in SPEC_GAPS.items()
        if not any(_is_spec_gap(op, call) for call in ops.calls.get(op, ()))
    )
    assert not orphaned, (
        "SPEC_GAPS entries name a call their op no longer makes, so the gap "
        f"covers nothing and the real call goes unchecked: {orphaned}"
    )


def test_no_wire_call_ops_are_expected() -> None:
    ops = _extract_ops()
    unexpected = sorted(set(ops.no_wire_call) - NO_WIRE_CALL_OK)
    assert not unexpected, (
        "Ops with no readable wire call of their own. If they truly only "
        f"drive other registered ops, add them to NO_WIRE_CALL_OK: {unexpected}"
    )
    stale = sorted(NO_WIRE_CALL_OK - set(ops.no_wire_call))
    assert not stale, f"NO_WIRE_CALL_OK entries no longer match reality: {stale}"


def test_spec_marks_no_body_property_read_only(swagger: _Swagger) -> None:
    """Guard for a check this module does not carry.

    No definition the endpoints we write to reach marks a property readOnly, so
    every declared name is a name we may legitimately send and the accepted set
    needs no filtering. If that changes, drop readOnly names from the accepted
    set: sending one is a no-op the server never complains about.
    """
    found = sorted(
        f"{where}: {sorted(endpoint.read_only_body)}"
        for where, endpoint in _endpoints_we_call(swagger)
        if endpoint.read_only_body
    )
    assert not found, (
        "The spec now marks body properties readOnly on endpoints we call; drop "
        "them from the accepted field set:\n" + "\n".join(f"  {f}" for f in found)
    )


def test_spec_declares_no_schemaless_body(swagger: _Swagger) -> None:
    """Guard for a check this module does not carry.

    Every body parameter in the spec carries a schema, so a body is always an
    object whose field names are comparable, and there is no raw-upload case to
    tell apart. If that changes, compare the kind of body too: handing bytes to
    an endpoint that documents a schema (or an object to one that wants bytes)
    is a wire mismatch even when no field name is readable on our side.
    """
    found = sorted(
        where for where, endpoint in _endpoints_we_call(swagger) if endpoint.schemaless_body
    )
    assert not found, (
        "The spec now declares a body parameter with no schema on endpoints we "
        "call; add a raw-vs-JSON body check:\n" + "\n".join(f"  {f}" for f in found)
    )


def test_required_query_params_are_sent(swagger: _Swagger) -> None:
    """A required query param we never send fails as silently as a misspelled
    one: Gitea resolves the empty value to its default and still returns 200."""
    findings: list[str] = []
    for op, call in _checked_calls():
        matches = swagger.candidates(call.path, call.method)
        if len(matches) != 1:
            continue  # reported by test_wire_calls_match_swagger
        missing = sorted(swagger.endpoints[matches[0], call.method].required_query - call.query)
        if missing:
            findings.append(
                f"{op}: {call.method} {call.path} never sends required query params {missing}"
            )
    assert not findings, (
        f"{len(findings)} call(s) omit a query param the spec marks required:\n"
        + "\n".join(f"  {f}" for f in findings)
    )


def _arg_wire_names(fn: Callable[..., Any]) -> dict[str, str]:
    """Best-effort map from a signature arg to the wire name it is sent under.

    Sources: `rename=` dicts of _call/_body, dict literals `{"key": arg}`, and
    subscript assigns `params["key"] = arg`. Args sent under their own name need
    no entry; args whose value is transformed before sending stay unmapped and
    are simply not enum-checked.
    """
    mapping: dict[str, str] = {}
    for node in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(fn)))):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Subscript)
            and isinstance(node.targets[0].slice, ast.Constant)
            and isinstance(node.value, ast.Name)
        ):
            mapping[node.value.id] = node.targets[0].slice.value
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and isinstance(value, ast.Name):
                    mapping[value.id] = key.value
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("_call", "_body")
        ):
            for kw in node.keywords:
                if kw.arg == "rename" and isinstance(kw.value, ast.Dict):
                    for key, value in zip(kw.value.keys, kw.value.values):
                        if isinstance(key, ast.Constant) and isinstance(value, ast.Constant):
                            mapping[key.value] = value.value
    return mapping


def test_required_body_params_are_required(swagger: _Swagger) -> None:
    """An omitted MCP param cannot omit a body field Gitea requires."""
    findings: list[str] = []
    for op, call in _checked_calls():
        matches = swagger.candidates(call.path, call.method)
        if len(matches) != 1:
            continue  # reported by test_wire_calls_match_swagger
        endpoint = swagger.endpoints[matches[0], call.method]
        required_on_wire = endpoint.required_body & call.body
        if not required_on_wire:
            continue
        fn = getattr(tools, op)
        required_args = set(fn._params_model.model_json_schema().get("required") or ())
        wire_of = _arg_wire_names(fn)
        for arg, param in inspect.signature(fn).parameters.items():
            wire = wire_of.get(arg, arg)
            if wire not in required_on_wire or arg in required_args:
                continue
            default_is_omitted = param.default is None or isinstance(
                param.default, server._Unset
            )
            if default_is_omitted:
                findings.append(
                    f"{op}.{arg} -> {call.method} {call.path} body field {wire!r}: "
                    "required by Gitea but omitted when the MCP param is absent"
                )
    assert not findings, (
        f"{len(findings)} optional MCP body parameter(s) omit fields Gitea requires:\n"
        + "\n".join(f"  {f}" for f in findings)
    )


def _literal_values(annotation: Any) -> frozenset[str]:
    """String values of every Literal reachable inside the annotation."""
    values: set[str] = set()
    stack = [annotation]
    while stack:
        ann = stack.pop()
        if typing.get_origin(ann) is Literal:
            values |= {a for a in typing.get_args(ann) if isinstance(a, str)}
        else:
            stack.extend(typing.get_args(ann))
    return frozenset(values)


def test_query_enum_values_match_swagger(swagger: _Swagger) -> None:
    """A Literal value the spec's enum lacks is the silent-lie class again:
    Gitea quietly falls back to its default instead of erroring. Query params
    only - body enums are rare and not modeled here."""
    findings: list[str] = []
    for op, calls in sorted(_extract_ops().calls.items()):
        fn = getattr(tools, op)
        hints = typing.get_type_hints(fn, include_extras=True, localns=vars(tools))
        wire_of = _arg_wire_names(fn)
        for call in calls:
            matches = swagger.candidates(call.path, call.method)
            if _is_spec_gap(op, call) or len(matches) != 1:
                continue
            enums = swagger.endpoints[matches[0], call.method].query_enums
            for arg, annotation in hints.items():
                values = _literal_values(annotation)
                wire = wire_of.get(arg, arg)
                if not values or wire not in call.query:
                    continue
                extra = sorted(values - enums[wire]) if wire in enums else []
                if extra:
                    findings.append(
                        f"{op}.{arg} -> {call.method} {call.path} ?{wire}: Literal "
                        f"values {extra} are not in the spec enum {sorted(enums[wire])}"
                    )
    assert not findings, (
        f"{len(findings)} Literal(s) advertise values the spec enum lacks; Gitea "
        "silently substitutes its default for these:\n"
        + "\n".join(f"  {f}" for f in findings)
    )


def test_wire_calls_match_swagger(swagger: _Swagger) -> None:
    findings: list[str] = []
    for op, calls in sorted(_extract_ops().calls.items()):
        gap = SPEC_GAPS.get(op)
        # A list, not a set: a gap waives one call, so a second call to the same
        # missing endpoint has to surface rather than collapse into the first.
        unserved: list[str] = []
        for call in calls:
            where = f"{op}: {call.endpoint}"
            matches = swagger.candidates(call.path, call.method)
            if not matches:
                unserved.append(call.endpoint)
                if gap is None:
                    findings.append(
                        f"{where}: no such endpoint in the spec; it has "
                        f"{swagger.describe_pool(call.path)}"
                    )
                continue
            if _is_spec_gap(op, call):
                findings.append(f"{where}: endpoint is back in the spec - drop it from SPEC_GAPS")
                continue
            if len(matches) > 1:
                findings.append(f"{where}: ambiguous, matches spec paths {matches}")
                continue
            endpoint = swagger.endpoints[matches[0], call.method]
            bad_query = sorted(call.query - endpoint.query)
            if bad_query:
                findings.append(
                    f"{where}: query params {bad_query} are not in the spec; "
                    f"it accepts {sorted(endpoint.query)}"
                )
            bad_body = sorted(call.body - endpoint.body)
            if bad_body:
                findings.append(
                    f"{where}: body fields {bad_body} are not in the spec; "
                    f"it accepts {sorted(endpoint.body)}"
                )
        if gap is not None and sorted(unserved) != [gap.call]:
            findings.append(
                f"{op}: recorded as absent from the spec for [{gap.call}], but its "
                f"unserved calls are {sorted(unserved) or 'none - drop it from SPEC_GAPS'}"
            )
    assert not findings, (
        f"{len(findings)} call(s) disagree with the Gitea spec. Gitea drops "
        "unknown names silently, so each of these is a request that quietly "
        "does not do what it says:\n" + "\n".join(f"  {f}" for f in findings)
    )
