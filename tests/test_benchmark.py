"""Release gate: a decorated tool outside a session adds under 5 microseconds per call."""

from __future__ import annotations

import asyncio
import timeit

from replex.capture.tools import tool

N = 200_000


def plain(order_id: str) -> str:
    return order_id


decorated = tool(plain)


async def plain_async(order_id: str) -> str:
    return order_id


decorated_async = tool(plain_async)


def overhead_ns(fn_plain: object, fn_tool: object) -> float:
    """Median-of-5 per-call overhead in nanoseconds."""
    runs = []
    for _ in range(5):
        base = timeit.timeit(lambda: fn_plain("1042"), number=N)  # type: ignore[operator]
        wrapped = timeit.timeit(lambda: fn_tool("1042"), number=N)  # type: ignore[operator]
        runs.append((wrapped - base) / N * 1e9)
    return sorted(runs)[2]


def test_sync_tool_overhead_outside_session() -> None:
    ns = overhead_ns(plain, decorated)
    print(f"\n@replex.tool overhead outside a session (sync): {ns:.0f} ns/call")
    assert ns < 5_000


def test_async_tool_overhead_outside_session() -> None:
    async def loop(fn: object, n: int) -> float:
        start = asyncio.get_running_loop().time()
        for _ in range(n):
            await fn("1042")  # type: ignore[operator]
        return asyncio.get_running_loop().time() - start

    n = 50_000
    runs = []
    for _ in range(5):
        base = asyncio.run(loop(plain_async, n))
        wrapped = asyncio.run(loop(decorated_async, n))
        runs.append((wrapped - base) / n * 1e9)
    ns = sorted(runs)[2]
    print(f"@replex.tool overhead outside a session (async): {ns:.0f} ns/call")
    assert ns < 5_000
