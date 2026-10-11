"""Publish due scheduled posts: atomic claim, guarded publish, retries with backoff."""

from __future__ import annotations

import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from types import FrameType
from typing import Literal, Protocol

from igctl.backends.base import AuthError, Backend, IgctlError, NotSupported, RateLimited
from igctl.core.models import PublishRequest, PublishResult
from igctl.core.ratelimit import Guard
from igctl.core.storage import Store, utcnow
from igctl.scheduler import queue

MAX_ATTEMPTS = 3
BACKOFF_BASE = timedelta(minutes=5)
STALE_AFTER = timedelta(hours=1)
CAP_WAIT = timedelta(hours=1)

# Exceptions (by class name, anywhere in the MRO) worth retrying. Names keep instagrapi/httpx
# optional here: the backends usually map them to RateLimited, but not always.
_TRANSIENT_NAMES = frozenset(
    {
        "TransportError",  # httpx network errors
        "TimeoutException",
        "ClientConnectionError",  # instagrapi
        "ClientRequestTimeout",
        "ClientIncompleteReadError",
        "ClientThrottledError",
        "PleaseWaitFewMinutes",
        "RateLimitError",
        "SentryBlock",
    }
)

Status = Literal["done", "retry", "failed", "skipped", "dry-run"]


class AccountContext(Protocol):
    """What the runner needs from :class:`igctl.core.context.Context`."""

    @property
    def guard(self) -> Guard: ...

    def backend(self, prefer: str | None = None) -> Backend: ...


ContextFactory = Callable[[str], AccountContext]


@dataclass
class RunOutcome:
    post_id: int
    account: str
    status: Status
    detail: str = ""
    media_id: str | None = None
    permalink: str | None = None
    next_run_at: datetime | None = None


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, RateLimited | ConnectionError | TimeoutError):
        return True
    if isinstance(exc, NotSupported | AuthError):
        return False
    return any(cls.__name__ in _TRANSIENT_NAMES for cls in type(exc).__mro__)


def backoff_delay(attempt: int, base: timedelta = BACKOFF_BASE) -> timedelta:
    """Delay before retry number ``attempt`` (1-based): base, 2*base, 4*base, ..."""
    return base * (2 ** max(0, attempt - 1))


def _check_files(req: PublishRequest) -> None:
    missing = [str(p) for p in [*req.paths, *([req.cover] if req.cover else [])] if not p.is_file()]
    if missing:
        raise IgctlError(f"Media file(s) no longer exist: {', '.join(missing)}")


def _target(req: PublishRequest) -> str:
    first = req.paths[0].name if req.paths else (req.urls[0] if req.urls else "")
    return f"{req.media_type.value}:{first}"


def run_due(
    store: Store,
    context_factory: ContextFactory,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    max_attempts: int = MAX_ATTEMPTS,
    backoff: timedelta = BACKOFF_BASE,
    should_stop: Callable[[], bool] = lambda: False,
    on_outcome: Callable[[RunOutcome], None] | None = None,
) -> list[RunOutcome]:
    """Publish every post due at ``now`` (default: current time) and return the outcomes.

    Each post is claimed atomically (safe with several runners), published through the
    account's backend via ``Guard.act("publish", ...)``, then marked ``done``/``failed``.
    Transient failures (rate limits, network) go back to ``pending`` with exponential
    backoff until ``max_attempts`` is used up. With ``dry_run`` nothing is claimed.
    """
    outcomes: list[RunOutcome] = []
    contexts: dict[str, AccountContext] = {}

    def report(outcome: RunOutcome) -> None:
        outcomes.append(outcome)
        if on_outcome:
            on_outcome(outcome)

    for row in store.due_posts(now):
        if should_stop():
            break
        post_id, account = int(row["id"]), str(row["account"])
        if dry_run:
            report(RunOutcome(post_id, account, "dry-run", "would publish"))
            continue
        if not store.claim_post(post_id):
            report(RunOutcome(post_id, account, "skipped", "claimed by another runner"))
            continue
        queue.record_claim(store, post_id)
        attempts = int(row["attempts"]) + 1
        try:
            req = PublishRequest.model_validate_json(row["request"])
            _check_files(req)
            if account not in contexts:
                contexts[account] = context_factory(account)
            ctx = contexts[account]
            if ctx.guard.remaining("publish") == 0:
                # Daily cap hit: wait without burning an attempt.
                at = (now or utcnow()) + CAP_WAIT
                queue.reschedule(
                    store, post_id, at, "Daily publish limit reached.", refund_attempt=True
                )
                report(RunOutcome(post_id, account, "retry", "daily publish limit", next_run_at=at))
                continue
            backend = ctx.backend()
            result: PublishResult | None = ctx.guard.act(
                "publish", _target(req), partial(backend.publish, req)
            )
        except Exception as e:  # every failure is recorded on the post
            error = f"{type(e).__name__}: {e}"
            if is_transient(e) and attempts < max_attempts:
                at = (now or utcnow()) + backoff_delay(attempts, backoff)
                queue.reschedule(store, post_id, at, error)
                report(RunOutcome(post_id, account, "retry", error, next_run_at=at))
            else:
                store.finish_post(post_id, "failed", error=error)
                report(RunOutcome(post_id, account, "failed", error))
            continue
        if result is None:  # the account's guard is in dry-run mode
            queue.reschedule(store, post_id, (now or utcnow()), refund_attempt=True)
            report(RunOutcome(post_id, account, "dry-run", "guard dry-run; left pending"))
            continue
        store.finish_post(post_id, "done", result=result.model_dump_json())
        report(
            RunOutcome(
                post_id,
                account,
                "done",
                f"published via {result.backend}",
                media_id=result.media_id,
                permalink=result.permalink,
            )
        )
    return outcomes


class StopFlag:
    """Ctrl+C handling for the daemon: first press finishes the current post and stops,
    the second press aborts immediately."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._previous: Callable | int | None = None

    def is_set(self) -> bool:
        return self._event.is_set()

    def set(self) -> None:
        self._event.set()

    def _handler(self, signum: int, frame: FrameType | None) -> None:
        if self._event.is_set():
            raise KeyboardInterrupt
        self._event.set()

    def __enter__(self) -> StopFlag:
        if threading.current_thread() is threading.main_thread():
            self._previous = signal.signal(signal.SIGINT, self._handler)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._previous is not None:
            signal.signal(signal.SIGINT, self._previous)
            self._previous = None

    def sleep(self, seconds: float, step: float = 0.5) -> None:
        # Short slices so the SIGINT handler runs promptly on every platform (incl. Windows).
        end = time.monotonic() + seconds
        while not self.is_set() and (left := end - time.monotonic()) > 0:
            time.sleep(min(step, left))


def run_loop(
    store: Store,
    context_factory: ContextFactory,
    *,
    interval: float = 60.0,
    stop: StopFlag | None = None,
    on_outcome: Callable[[RunOutcome], None] | None = None,
    on_error: Callable[[Exception], None] | None = None,
    max_attempts: int = MAX_ATTEMPTS,
    backoff: timedelta = BACKOFF_BASE,
) -> None:
    """Poll for due posts every ``interval`` seconds until ``stop`` is set."""
    stop = stop or StopFlag()
    while not stop.is_set():
        try:
            queue.recover_stale(store, STALE_AFTER, max_attempts)
            run_due(
                store,
                context_factory,
                max_attempts=max_attempts,
                backoff=backoff,
                should_stop=stop.is_set,
                on_outcome=on_outcome,
            )
        except Exception as e:  # keep the daemon alive (e.g. sqlite busy)
            if on_error:
                on_error(e)
        stop.sleep(interval)
