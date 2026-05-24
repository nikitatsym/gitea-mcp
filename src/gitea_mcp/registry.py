"""Tool registration primitives."""


class Group:
    """A named group of MCP tool operations exposed as a single meta-tool."""

    __slots__ = ("name", "doc")

    def __init__(self, name: str, doc: str):
        self.name = name
        self.doc = doc


ROOT = Group("root", "")


class _Unset:
    """Sentinel singleton: caller did not pass this field.

    Distinct from `None`. `None` means "caller explicitly passed null" — the
    Gitea API treats null as a clearing operation on some nullable fields
    (e.g. clearing a milestone or assignee). Optional body params declared
    with default `_UNSET` carry the omitted-vs-cleared distinction through
    Pydantic validation (`exclude_unset=True`) and on to the wire (`_body`
    drops `_UNSET` but, when the field is listed in `keep_null=`, keeps an
    explicit `None`).

    See `prepare.py:_body` for the dispatch rules.
    """

    _instance: "_Unset | None" = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "_UNSET"

    def __bool__(self) -> bool:
        return False


_UNSET = _Unset()


def _op(group: Group):
    """Mark a function as an MCP tool in the given group.

    A Pydantic params model is built from the signature at server registration
    time; descriptions/constraints in `Annotated[T, Field(...)]` flow into the
    JSON Schema returned by `operation='schema'`.
    """
    def decorator(fn):
        if not fn.__doc__:
            raise RuntimeError(f"Tool function {fn.__name__!r} has no docstring")
        fn._mcp_group = group
        return fn
    return decorator
