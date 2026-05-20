"""Tool registration primitives."""


class Group:
    """A named group of MCP tool operations exposed as a single meta-tool."""

    __slots__ = ("name", "doc")

    def __init__(self, name: str, doc: str):
        self.name = name
        self.doc = doc


ROOT = Group("root", "")


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
