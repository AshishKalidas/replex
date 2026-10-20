"""The official SDKs, captured with no code changes, then replayed offline.

Current Anthropic and OpenAI SDKs send requests through ``httpx2``; older releases (and those
installed on Python 3.10) through ``httpx``. Whichever one pip resolved, recording through the
real SDK and replaying it with nothing listening must give the same result.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import replex
from replex.capture import patching
from replex.capture.libs import LIBS, lib_for
from replex.config import Config
from replex.testing import MockProvider, MockReply, MockToolCall

anthropic = pytest.importorskip("anthropic")
openai = pytest.importorskip("openai")

OFFLINE = "http://127.0.0.1:9"  # nothing listens here: replay must not touch the network
ANTHROPIC_KEY = "sk-ant-api03-test-key-that-must-never-be-written"
OPENAI_KEY = "sk-proj-test-key-that-must-never-be-written-0000"


def script() -> list[MockReply]:
    return [
        MockReply(tool_calls=[MockToolCall("get_order", {"order_id": "1042"})]),
        MockReply(text="Order 1042 has shipped."),
    ]


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return replex.load_config(
        tmp_path,
        cassette_dir="cassettes",
        intercept_hosts=["127.0.0.1"],
        passthrough_hosts=[],
    )


def conversation() -> list[dict[str, Any]]:
    return [{"role": "user", "content": "Where is order 1042?"}]


def anthropic_sync(url: str, stream: bool) -> list[Any]:
    client = anthropic.Anthropic(base_url=url, api_key=ANTHROPIC_KEY, max_retries=0)
    out = []
    for _ in range(2):
        kw: dict[str, Any] = {"model": "mock-sonnet", "max_tokens": 100, "messages": conversation()}
        if stream:
            with client.messages.stream(**kw) as s:
                message = s.get_final_message()
        else:
            message = client.messages.create(**kw)
        out.append([b.model_dump() for b in message.content])
    return out


def openai_sync(url: str, stream: bool) -> list[Any]:
    client = openai.OpenAI(base_url=f"{url}/v1", api_key=OPENAI_KEY, max_retries=0)
    out: list[Any] = []
    for _ in range(2):
        kw: dict[str, Any] = {"model": "mock-mimo", "messages": conversation()}
        if stream:
            out.append([c.model_dump() for c in client.chat.completions.create(stream=True, **kw)])
        else:
            out.append(client.chat.completions.create(**kw).choices[0].model_dump())
    return out


def anthropic_async(url: str) -> list[Any]:
    async def run() -> list[Any]:
        client = anthropic.AsyncAnthropic(base_url=url, api_key=ANTHROPIC_KEY, max_retries=0)
        out = []
        for _ in range(2):
            m = await client.messages.create(
                model="mock-sonnet", max_tokens=100, messages=conversation()
            )
            out.append([b.model_dump() for b in m.content])
        return out

    return asyncio.run(run())


def openai_async_stream(url: str) -> list[Any]:
    async def run() -> list[Any]:
        client = openai.AsyncOpenAI(base_url=f"{url}/v1", api_key=OPENAI_KEY, max_retries=0)
        out = []
        for _ in range(2):
            stream = await client.chat.completions.create(
                model="mock-mimo", messages=conversation(), stream=True
            )
            out.append([c.model_dump() async for c in stream])
        return out

    return asyncio.run(run())


CASES: dict[str, Callable[[str], list[Any]]] = {
    "anthropic": lambda url: anthropic_sync(url, stream=False),
    "anthropic_stream": lambda url: anthropic_sync(url, stream=True),
    "anthropic_async": anthropic_async,
    "openai": lambda url: openai_sync(url, stream=False),
    "openai_stream": lambda url: openai_sync(url, stream=True),
    "openai_async_stream": openai_async_stream,
}


@pytest.mark.parametrize("name", list(CASES))
def test_sdk_records_and_replays_offline(cfg: Config, name: str) -> None:
    run = CASES[name]
    with MockProvider(script()) as mock, replex.session(name, mode="record", config=cfg) as rec:
        live = run(mock.url)
    assert rec.written_path is not None
    text = rec.written_path.read_text()
    assert ANTHROPIC_KEY not in text and OPENAI_KEY not in text
    events = json.loads(text)["events"]
    assert len(events) == 2, f"the SDK's requests were not captured ({len(events)} events)"

    with replex.session(name, mode="replay", config=cfg) as replay:
        again = run(OFFLINE)
    assert again == live
    trace = replay.trace
    assert trace.steps_count == 2
    assert [r.name for r in trace.model_calls[0].tool_requests] == ["get_order"]
    assert trace.final_output == "Order 1042 has shipped."


def test_explicit_client_for_the_sdks_own_library(cfg: Config) -> None:
    """``replex.http_client(library=...)`` builds a client of the library the SDK expects."""
    library = lib_for(anthropic.Anthropic(api_key="x")._client).__name__

    def run(url: str) -> str:
        client = anthropic.Anthropic(
            base_url=url,
            api_key=ANTHROPIC_KEY,
            max_retries=0,
            http_client=replex.http_client(library=library),
        )
        message = client.messages.create(
            model="mock-sonnet", max_tokens=10, messages=conversation()
        )
        return str(message.content[0].text)

    with (
        MockProvider(["Hello."]) as mock,
        replex.session("explicit", mode="record", config=cfg, patch_httpx=False),
    ):
        assert run(mock.url) == "Hello."
    with replex.session("explicit", mode="replay", config=cfg, patch_httpx=False):
        assert run(OFFLINE) == "Hello."


def test_every_installed_library_is_patched_and_restored() -> None:
    originals = [
        (lib.HTTPTransport.handle_request, lib.AsyncHTTPTransport.handle_async_request)
        for lib in LIBS
    ]
    patching.install()
    try:
        for lib, (sync, async_) in zip(LIBS, originals, strict=True):
            assert lib.HTTPTransport.handle_request is not sync
            assert lib.AsyncHTTPTransport.handle_async_request is not async_
    finally:
        patching.uninstall()
    for lib, (sync, async_) in zip(LIBS, originals, strict=True):
        assert lib.HTTPTransport.handle_request is sync
        assert lib.AsyncHTTPTransport.handle_async_request is async_


def test_unknown_library_is_a_clear_error() -> None:
    with pytest.raises(ImportError, match="is not installed"):
        replex.http_client(library="requests")  # type: ignore[call-overload]
