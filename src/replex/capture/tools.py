"""Tool capture and replay: the ``@replex.tool`` decorator and ``wrap_tools``.

Outside a session a decorated tool is a transparent pass-through: the wrapper reads one
module global and calls the original function.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime as _dt
import enum
import functools
import inspect
import math
import sys
import time
import typing
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeVar, overload

from pydantic import BaseModel, TypeAdapter, ValidationError

from replex import _runtime
from replex.capture.context import CaptureContext
from replex.cassette.model import ToolCallEvent, ToolError
from replex.errors import ReplayedToolError, UnrecordedToolCall

F = TypeVar("F", bound=Callable[..., Any])

REPR_TAG = "__replex_repr__"
TYPE_TAG = "__replex_type__"
BYTES_TAG = "__replex_bytes__"


@dataclass(frozen=True)
class UnserialisableValue:
    """Stands in, during replay, for a value that was recorded through the ``repr`` fallback.

    Attributes:
        type_name: Fully qualified type of the original value.
        repr: ``repr()`` of the original value (after redaction).
    """

    type_name: str
    repr: str


# --------------------------------------------------------------------- serialisation


def _type_name(obj: Any) -> str:
    t = obj if isinstance(obj, type) else type(obj)
    return t.__qualname__ if t.__module__ == "builtins" else f"{t.__module__}.{t.__qualname__}"


def to_jsonable(value: Any) -> tuple[Any, bool]:
    """Convert a value to JSON-compatible data. Returns ``(data, lossy)``.

    Pydantic models, dataclasses, enums, dates, sets, tuples and bytes are converted
    faithfully enough to compare. Anything else becomes ``{"__replex_repr__": ...,
    "__replex_type__": ...}`` and ``lossy`` is True.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value, False
    if isinstance(value, float):
        if math.isfinite(value):
            return value, False
        return {REPR_TAG: repr(value), TYPE_TAG: "float"}, True
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        lossy = False
        for k, v in value.items():
            if not isinstance(k, str):
                lossy = True
            out[str(k)], sub = to_jsonable(v)
            lossy = lossy or sub
        return out, lossy
    if isinstance(value, (list, tuple)):
        items = [to_jsonable(v) for v in value]
        return [i for i, _ in items], any(lossy for _, lossy in items)
    if isinstance(value, (set, frozenset)):
        items = [to_jsonable(v) for v in value]
        data = [i for i, _ in items]
        try:
            data = sorted(data)
        except TypeError:
            pass
        return data, any(lossy for _, lossy in items)
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json"), False
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return to_jsonable(dataclasses.asdict(value))
    if isinstance(value, enum.Enum):
        return to_jsonable(value.value)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat(), False
    if isinstance(value, (bytes, bytearray)):
        return {BYTES_TAG: base64.b64encode(bytes(value)).decode("ascii")}, False
    return {REPR_TAG: repr(value), TYPE_TAG: _type_name(value)}, True


def from_jsonable(value: Any) -> Any:
    """Inverse of :func:`to_jsonable` as far as possible (tagged values are restored)."""
    if isinstance(value, dict):
        if set(value) == {REPR_TAG, TYPE_TAG}:
            if value[TYPE_TAG] == "float":
                return float(value[REPR_TAG])
            return UnserialisableValue(str(value[TYPE_TAG]), str(value[REPR_TAG]))
        if set(value) == {BYTES_TAG}:
            return base64.b64decode(value[BYTES_TAG])
        return {k: from_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [from_jsonable(v) for v in value]
    return value


# --------------------------------------------------------------------- tool spec


@dataclass
class ToolSpec:
    """What RepleX knows about one decorated tool."""

    name: str
    fn: Callable[..., Any]
    safe: bool = False
    is_async: bool = False
    signature: inspect.Signature | None = None
    skip_first: bool = False
    _adapter: Any = field(default=None, repr=False)
    _adapter_ready: bool = field(default=False, repr=False)

    def bind(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
        """Name every argument (defaults applied) so calls compare by meaning, not position."""
        if self.signature is None:
            return {"args": list(args), "kwargs": kwargs}
        bound = self.signature.bind(*args, **kwargs)
        bound.apply_defaults()
        items = list(bound.arguments.items())
        if self.skip_first and items:
            items = items[1:]
        out: dict[str, Any] = {}
        for pname, value in items:
            kind = self.signature.parameters[pname].kind
            if kind is inspect.Parameter.VAR_KEYWORD:
                out.update(value)
            else:
                out[pname] = list(value) if kind is inspect.Parameter.VAR_POSITIONAL else value
        return out

    def coerce(self, value: Any) -> Any:
        """Rebuild the declared return type (pydantic models, dataclasses...) when possible."""
        if not self._adapter_ready:
            self._adapter_ready = True
            try:
                hint = typing.get_type_hints(self.fn).get("return")
            except Exception:
                hint = None
            if hint is not None and hint is not typing.Any and hint is not type(None):
                try:
                    self._adapter = TypeAdapter(hint)
                except Exception:
                    self._adapter = None
        if self._adapter is None or isinstance(value, UnserialisableValue):
            return value
        try:
            return self._adapter.validate_python(value)
        except ValidationError:
            return value


def _make_spec(fn: Callable[..., Any], name: str | None, safe: bool) -> ToolSpec:
    try:
        sig: inspect.Signature | None = inspect.signature(fn)
    except (TypeError, ValueError):
        sig = None
    params = list(sig.parameters) if sig else []
    return ToolSpec(
        name=name or str(getattr(fn, "__name__", "tool")),
        fn=fn,
        safe=safe,
        is_async=inspect.iscoroutinefunction(fn),
        signature=sig,
        skip_first=bool(params) and params[0] in ("self", "cls"),
    )


# --------------------------------------------------------------------- runtime behaviour


def _rebuild_exception(error: ToolError) -> BaseException:
    """The recorded exception as its original type, else a :class:`ReplayedToolError`.

    Only modules that are already imported are consulted: a cassette is data, and replaying
    one must never import code it names.
    """
    module_name, _, qualname = error.type.rpartition(".")
    obj: Any = sys.modules.get(module_name or "builtins")
    for part in qualname.split("."):
        obj = getattr(obj, part, None)
    if isinstance(obj, type) and issubclass(obj, Exception):
        try:
            return obj(error.message)
        except Exception:  # the type needs more than a message to construct
            pass
    return ReplayedToolError(error.type, error.message)


def _unrecorded(spec: ToolSpec, ctx: CaptureContext, args: dict[str, Any]) -> UnrecordedToolCall:
    recorded = ctx.recorded_tool_names(spec.name)
    shown = "\n".join(f"      {a}" for a in recorded[:5]) or "      (none)"
    redacted = ctx.redactor.fork().value(to_jsonable(args)[0])
    if ctx.mode == "record":
        # A drift run: the agent now does something the recording never did.
        advice = (
            f"tool {spec.name!r} was called with {redacted}, which {ctx.cassette_label} has no "
            f"call for, and it is not marked safe=True, so RepleX did not run it.\n"
            f"    recorded calls of {spec.name!r}:\n{shown}\n"
            f"  The agent's behaviour changed. Review it with `replex console`; if the new "
            f"behaviour is intended, re-record this scenario with --replex-mode=rewrite."
        )
    else:
        advice = (
            f"tool {spec.name!r} was called with {redacted} but {ctx.cassette_label} has no "
            f"unused recording with these arguments (tools mode: {ctx.tools_mode}).\n"
            f"    recorded calls of {spec.name!r}:\n{shown}\n"
            f"  In replay, tools never run live unless allowed. Re-record the cassette, mark the "
            f"tool @replex.tool(safe=True) and use tools='auto', or use tools='live'."
        )
    return UnrecordedToolCall(advice, tool=spec.name, arguments=redacted)


def _lookup(spec: ToolSpec, ctx: CaptureContext, args: dict[str, Any]) -> ToolCallEvent | None:
    """The recorded call to replay, ``None`` to execute live, or raise UnrecordedToolCall."""
    if ctx.tools_mode == "live":
        return None
    data, _ = to_jsonable(args)
    recorded = ctx.find_tool(spec.name, data)
    if recorded is not None:
        return recorded
    if ctx.tools_mode == "auto" and spec.safe:
        return None
    raise _unrecorded(spec, ctx, args)


def _replay(spec: ToolSpec, ctx: CaptureContext, recorded: ToolCallEvent) -> Any:
    ctx.append(lambda i: recorded.model_copy(update={"index": i}))
    if recorded.error is not None:
        raise _rebuild_exception(recorded.error)
    return spec.coerce(from_jsonable(recorded.result))


def _file(
    spec: ToolSpec,
    ctx: CaptureContext,
    args: dict[str, Any],
    start: float,
    result: Any = None,
    exc: BaseException | None = None,
) -> None:
    duration = int((time.perf_counter() - start) * 1000)
    data_args, lossy_args = to_jsonable(args)
    data_result, lossy_result = to_jsonable(result) if exc is None else (None, False)

    def make(index: int) -> ToolCallEvent:
        r = ctx.redactor
        return ToolCallEvent(
            index=index,
            name=spec.name,
            args=r.value(data_args),
            result=r.value(data_result),
            error=None
            if exc is None
            else ToolError(type=_type_name(exc), message=r.text(str(exc))),
            duration_ms=duration,
            lossy=lossy_args or lossy_result,
        )

    ctx.append(make)


def _begin(
    spec: ToolSpec, ctx: CaptureContext, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> tuple[dict[str, Any] | None, ToolCallEvent | None]:
    """Bind the call's arguments and find the recording to replay, if any.

    Returns ``(None, None)`` when the arguments do not fit the signature: the real call then
    raises the ``TypeError`` itself.
    """
    try:
        named = spec.bind(args, kwargs)
    except TypeError:
        return None, None
    return named, _lookup(spec, ctx, named)


def _call_sync(
    spec: ToolSpec, ctx: CaptureContext, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    named, recorded = _begin(spec, ctx, args, kwargs)
    if named is None:
        return spec.fn(*args, **kwargs)
    if recorded is not None:
        return _replay(spec, ctx, recorded)
    ctx.count_tool_execution()
    start = time.perf_counter()
    try:
        result = spec.fn(*args, **kwargs)
    except Exception as exc:
        _file(spec, ctx, named, start, exc=exc)
        raise
    _file(spec, ctx, named, start, result=result)
    return result


async def _call_async(
    spec: ToolSpec, ctx: CaptureContext, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    named, recorded = _begin(spec, ctx, args, kwargs)
    if named is None:
        return await spec.fn(*args, **kwargs)
    if recorded is not None:
        return _replay(spec, ctx, recorded)
    ctx.count_tool_execution()
    start = time.perf_counter()
    try:
        result = await spec.fn(*args, **kwargs)
    except Exception as exc:
        _file(spec, ctx, named, start, exc=exc)
        raise
    _file(spec, ctx, named, start, result=result)
    return result


# --------------------------------------------------------------------- public decorator


def _decorate(fn: F, name: str | None, safe: bool) -> F:
    spec = _make_spec(fn, name, safe)
    wrapper: Callable[..., Any]
    if spec.is_async:

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            ctx = _runtime._capture
            if ctx is None:
                return await fn(*args, **kwargs)
            return await _call_async(spec, ctx, args, kwargs)

        wrapper = async_wrapper
    else:

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            ctx = _runtime._capture
            if ctx is None:
                return fn(*args, **kwargs)
            return _call_sync(spec, ctx, args, kwargs)

        wrapper = sync_wrapper
    setattr(wrapper, "__replex_tool__", spec)  # noqa: B010
    return typing.cast(F, wrapper)


@overload
def tool(fn: F, /) -> F: ...
@overload
def tool(*, name: str | None = None, safe: bool = False) -> Callable[[F], F]: ...


def tool(
    fn: F | None = None, /, *, name: str | None = None, safe: bool = False
) -> F | Callable[[F], F]:
    """Record and replay a tool function (sync or async).

    Use bare (``@replex.tool``) or with options (``@replex.tool(safe=True)``).

    Args:
        name: Name recorded in the cassette. Defaults to the function's ``__name__``.
        safe: The tool has no side effects and may run live during ``drift`` runs when its
            arguments have no recording (``tools="auto"``).
    """
    if fn is not None:
        return _decorate(fn, name, safe)

    def decorator(f: F) -> F:
        return _decorate(f, name, safe)

    return decorator


def is_tool(fn: Any) -> bool:
    """True if ``fn`` was decorated with :func:`tool`."""
    return hasattr(fn, "__replex_tool__")


def wrap_tools(
    mapping: Mapping[str, Callable[..., Any]], *, safe: Iterable[str] = ()
) -> dict[str, Callable[..., Any]]:
    """Wrap a registry of tool functions, keyed by the name the model uses.

    Already-decorated functions are kept as they are. Names listed in ``safe`` are wrapped
    with ``safe=True``.
    """
    safe_names = set(safe)
    return {
        key: fn if is_tool(fn) else tool(name=key, safe=key in safe_names)(fn)
        for key, fn in mapping.items()
    }
