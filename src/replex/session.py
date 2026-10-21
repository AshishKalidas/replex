"""The session API: ``with replex.session("name", mode=...) as s: ...``."""

from __future__ import annotations

import contextlib
import datetime as _dt
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, Literal

from replex._version import __version__
from replex.capture import capturing
from replex.capture.context import CaptureContext, HttpSettings
from replex.capture.matching import BodyNormaliser
from replex.cassette.model import Cassette, CassetteMeta, CassetteUsage, Event
from replex.cassette.storage import cassette_path, load, save
from replex.config import MODES, Config, check_ci_guard, load_config
from replex.consistency.runs import run_path
from replex.errors import (
    CassetteExists,
    CassetteNotFound,
    ConfigError,
    NoActiveSession,
    UnrecordedToolCall,
)
from replex.pricing import Pricing
from replex.providers import build_trace, detect_provider
from replex.redaction import RedactionRules, Redactor
from replex.trace.model import Trace

Outcome = Literal[
    "replayed", "recorded", "drift_passed", "drift_failed", "sampled", "live", "error"
]


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_sha(root: str) -> str | None:
    env = os.environ.get("REPLEX_GIT_SHA")
    if env is not None:
        return env or None
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


class Session:
    """One recording, replay or drift run of a named cassette.

    Create it with :func:`replex.session`. Use it as a (sync or async) context manager. After
    the block, :attr:`trace` holds the provider-neutral trace of what ran, and in ``drift``
    mode :attr:`drift_report` holds the comparison with the baseline.
    """

    def __init__(
        self,
        name: str,
        mode: str | None = None,
        *,
        config: Config | None = None,
        cassette_dir: str | os.PathLike[str] | None = None,
        candidate_dir: str | os.PathLike[str] | None = None,
        match: str | None = None,
        tools: str | None = None,
        policy: str | os.PathLike[str] | None = None,
        overwrite: bool = False,
        patch_httpx: bool = True,
        normalisers: Sequence[BodyNormaliser] = (),
        intercept_hosts: Sequence[str] | None = None,
        passthrough_hosts: Sequence[str] | None = None,
        strict_unused: bool | None = None,
        tags: Sequence[str] = (),
        run_number: int | None = None,
    ) -> None:
        base = config if config is not None else load_config()
        if mode is not None and mode not in MODES:
            raise ConfigError(f"unknown mode {mode!r}; use one of {', '.join(MODES)}")
        self.config: Config = base.merged(
            mode=mode,
            cassette_dir=str(cassette_dir) if cassette_dir is not None else None,
            candidate_dir=str(candidate_dir) if candidate_dir is not None else None,
            match=match,
            tools=tools,
            policy=str(policy) if policy is not None else None,
            intercept_hosts=list(intercept_hosts) if intercept_hosts is not None else None,
            passthrough_hosts=list(passthrough_hosts) if passthrough_hosts is not None else None,
            strict_unused=strict_unused,
        )
        if run_number is not None and (self.config.mode != "drift" or run_number < 1):
            raise ConfigError(
                f"run_number={run_number!r} needs mode='drift' and a number from 1 "
                f"(it numbers one of several repeated drift runs)"
            )
        self.name = name
        self.run_number = run_number
        self.overwrite = overwrite
        self.patch_httpx = patch_httpx
        self.normalisers = list(normalisers)
        self.tags = list(tags)
        self._pricing = Pricing.from_config(self.config)
        self.baseline: Cassette | None = None
        self.cassette: Cassette | None = None
        self.drift_report: Any = None
        self.outcome: Outcome | None = None
        self.written_path: Path | None = None
        self._ctx: CaptureContext | None = None
        self._stack: contextlib.ExitStack | None = None
        self._trace: Trace | None = None
        self._finished = False

    # ------------------------------------------------------------------ paths
    @property
    def mode(self) -> str:
        """The resolved mode: replay, record, drift, rewrite or live."""
        return self.config.mode

    @property
    def baseline_path(self) -> Path:
        """Where the baseline cassette lives."""
        return cassette_path(self.config.cassette_path, self.name)

    @property
    def candidate_path(self) -> Path:
        """Where a drift run writes its candidate (a numbered run file when repeating)."""
        if self.run_number is not None:
            return run_path(self.config.candidate_path, self.name, self.run_number)
        return cassette_path(self.config.candidate_path, self.name)

    @property
    def capture(self) -> CaptureContext | None:
        """The capture context while the session is active (``None`` in live mode)."""
        return self._ctx

    # ------------------------------------------------------------------ lifecycle
    def _http_settings(self) -> HttpSettings:
        c = self.config
        return HttpSettings(
            intercept_hosts=c.intercept_hosts,
            passthrough_hosts=c.passthrough_hosts,
            match=c.match,
            ignore_request_fields=c.ignore_request_fields,
            normalisers=self.normalisers,
            strict_unused=c.strict_unused,
        )

    def _load_baseline(self) -> Cassette:
        try:
            return load(self.baseline_path)
        except CassetteNotFound:
            raise CassetteNotFound(
                f"no cassette named {self.name!r} at {self.baseline_path}.\n"
                f"  Record it first: pytest --replex-mode=record (or replex.session(..., "
                f"mode='record')). Replay never touches the network, so a cassette is required."
            ) from None

    def start(self) -> Session:
        """Begin recording or replaying. Prefer ``with`` / ``async with``."""
        from replex import _runtime

        mode = self.mode
        check_ci_guard(mode)
        stack = contextlib.ExitStack()
        if mode == "live":
            if not _runtime.activate(self, None):
                from replex.errors import SessionActive

                raise SessionActive("a RepleX session is already active; sessions do not nest.")
            stack.callback(_runtime.deactivate, self)
            self._stack = stack
            return self
        if mode in ("replay", "drift"):
            self.baseline = self._load_baseline()
        if mode == "record" and self.baseline_path.exists() and not self.overwrite:
            raise CassetteExists(
                f"cassette {self.baseline_path} already exists; record mode will not "
                f"overwrite it.\n  Use --replex-overwrite (overwrite=True), or mode='rewrite' "
                f"to replace the baseline."
            )
        tools = self.config.tools
        if mode in ("record", "rewrite"):
            tools = "live"
        rules = RedactionRules.from_config(self.config.redact_patterns, self.config.redact_paths)
        self._ctx = CaptureContext(
            mode="replay" if mode == "replay" else "record",
            http=self._http_settings(),
            tools_mode=tools,
            redactor=Redactor(rules),
            baseline=self.baseline,
            cassette_label=str(self.baseline_path),
        )
        stack.enter_context(capturing(self._ctx, owner=self, patch=self.patch_httpx))
        self._stack = stack
        return self

    def finish(self, exc: BaseException | None = None) -> None:
        """End the session: write cassettes, compute drift, check unused recordings."""
        if self._finished:
            return
        self._finished = True
        assert self._stack is not None, "session was never started"
        self._stack.close()
        mode, ctx = self.mode, self._ctx
        if mode == "live" or ctx is None:
            self.outcome = "live"
            return
        events = ctx.finished_events()
        if mode == "replay":
            assert self.baseline is not None
            run = self._build(events, meta=self.baseline.meta.model_copy(deep=True))
            self._trace = build_trace(run, pricing=self._pricing)
            self.outcome = "replayed" if exc is None else "error"
            if exc is None:
                ctx.check_unused()
            return
        cassette = self._build(events, error=exc)
        self.cassette = cassette
        self._trace = build_trace(cassette, pricing=self._pricing)
        if mode == "drift":
            from replex.drift.compare import compare
            from replex.drift.policy import Policy, load_policy

            self.written_path = save(cassette, self.candidate_path)
            if self.run_number is not None and exc is None:
                # One of several repeated runs: they are measured together, not judged alone.
                self.outcome = "sampled"
                return
            blocked = None
            if isinstance(exc, UnrecordedToolCall) and self.run_number is None:
                blocked = exc
            if exc is not None and blocked is None:
                # A crashed run is not a behavioural comparison; keep the candidate to debug.
                self.outcome = "error"
                return
            policy_file = self.config.policy_path
            policy = load_policy(policy_file) if policy_file.is_file() else Policy()
            assert self.baseline is not None
            self.drift_report = compare(
                build_trace(self.baseline, pricing=self._pricing),
                self._trace,
                policy,
                name=self.name,
                blocked=blocked,
            )
            self.outcome = "drift_passed" if self.drift_report.passed else "drift_failed"
            from replex.history import record_check

            record_check(self.config, self.drift_report, git_sha=cassette.meta.git_sha)
            return
        if mode == "rewrite" and exc is not None:
            self.outcome = "error"
            return
        self.written_path = save(cassette, self.baseline_path)
        self.outcome = "recorded"

    def _build(
        self,
        events: list[Event],
        *,
        meta: CassetteMeta | None = None,
        error: BaseException | None = None,
    ) -> Cassette:
        cassette = Cassette(
            name=self.name,
            created_at=_now(),
            replex_version=__version__,
            meta=meta or CassetteMeta(),
            events=events,
        )
        if meta is None:
            provider, _ = detect_provider(cassette)
            trace = build_trace(cassette)
            cassette.meta = CassetteMeta(
                provider=provider,
                model=trace.model,
                git_sha=_git_sha(self.config.root),
                tags=self.tags,
            )
            if error is not None and self._ctx is not None:
                setattr(  # noqa: B010 - extra field on an extra="allow" model
                    cassette.meta,
                    "error",
                    self._ctx.redactor.text(f"{type(error).__name__}: {error}"),
                )
            cassette.usage = CassetteUsage(
                input_tokens=trace.usage.input_tokens, output_tokens=trace.usage.output_tokens
            )
        return cassette

    # ------------------------------------------------------------------ results
    @property
    def trace(self) -> Trace:
        """The trace of this run (available during the session and after it)."""
        if self._trace is not None:
            return self._trace
        if self._ctx is None:
            raise NoActiveSession(
                "no trace: the session has not started, or runs in live mode where RepleX "
                "does not capture anything"
            )
        meta = (
            self.baseline.meta.model_copy(deep=True)
            if self.mode == "replay" and self.baseline
            else None
        )
        return build_trace(
            self._build(self._ctx.finished_events(), meta=meta), pricing=self._pricing
        )

    @property
    def tool_executions(self) -> int:
        """How many tool calls actually executed (as opposed to being replayed)."""
        return self._ctx.stats["tool_executions"] if self._ctx else 0

    # ------------------------------------------------------------------ context managers
    def __enter__(self) -> Session:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.finish(exc)

    async def __aenter__(self) -> Session:
        return self.start()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.finish(exc)

    def __repr__(self) -> str:
        return f"<replex.Session {self.name!r} mode={self.mode} outcome={self.outcome}>"


def session(name: str, mode: str | None = None, **options: Any) -> Session:
    """Start a RepleX session for the cassette ``name``.

    Args:
        name: Cassette name (file ``<cassette_dir>/<name>.replex.json``).
        mode: ``replay`` (default), ``record``, ``drift``, ``rewrite`` or ``live``. Defaults to
            the configured mode (``[tool.replex]``, then ``REPLEX_MODE``).
        **options: Any of ``cassette_dir``, ``candidate_dir``, ``match``, ``tools``, ``policy``,
            ``overwrite``, ``patch_httpx``, ``normalisers``, ``intercept_hosts``,
            ``passthrough_hosts``, ``strict_unused``, ``tags``, ``run_number`` or a ready
            ``config``. In drift mode, ``run_number=n`` writes run ``n`` of a repeated run
            (see ``replex.consistency``) instead of the candidate, and skips the comparison.

    Example::

        with replex.session("refund_request", mode="record") as s:
            answer = run_agent("I want a refund for order 1042")
        s.trace.assert_tool_sequence(["get_order", "check_refund_policy", "create_refund_request"])
    """
    return Session(name, mode, **options)


def current_session() -> Session:
    """The active session. Raises :class:`~replex.NoActiveSession` outside one."""
    from replex import _runtime

    owner = _runtime.active_owner()
    if not isinstance(owner, Session):
        raise NoActiveSession("no RepleX session is active")
    return owner
