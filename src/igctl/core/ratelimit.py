"""Human-paced execution of automated actions with per-account daily caps.

Every write action that automation performs (like, comment, follow, dm, publish...) should go
through :meth:`Guard.act` so it is delayed, capped, logged and dry-run aware.
"""

from __future__ import annotations

import functools
import random
import time
from collections import Counter
from collections.abc import Callable
from datetime import timedelta
from typing import Any, TypeVar

from igctl.backends.base import Backend, NotSupported, RateLimited
from igctl.core.config import Limits
from igctl.core.storage import Store, utcnow

T = TypeVar("T")

_REQUIRES_ATTR = "_igctl_requires"


def requires[R](backend: Backend, op: str, fn: Callable[[], R]) -> Callable[[], R]:
    """Tag an opaque callable with the backend operation it performs.

    :meth:`Guard.act` detects bound backend methods (and ``functools.partial`` of them) by
    itself; use this for lambdas/closures so ``--dry-run`` can still tell whether the backend
    supports the action.
    """

    @functools.wraps(fn)
    def call() -> R:
        return fn()

    setattr(call, _REQUIRES_ATTR, (backend, op))
    return call


def backend_op(fn: Callable[..., Any]) -> tuple[Backend, str] | None:
    """The ``(backend, method name)`` ``fn`` will call, when that can be determined."""
    tagged = getattr(fn, _REQUIRES_ATTR, None)
    if tagged is not None:
        return tagged
    while isinstance(fn, functools.partial):
        fn = fn.func
    owner = getattr(fn, "__self__", None)
    if isinstance(owner, Backend):
        return owner, fn.__name__
    return None


class Guard:
    def __init__(
        self,
        account: str,
        limits: Limits,
        store: Store,
        dry_run: bool = False,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.account = account
        self.limits = limits
        self.store = store
        self.dry_run = dry_run
        self._sleep = sleep
        self._last_at: float | None = None
        # Actions "performed" in dry-run, so previews hit the daily cap like a real run.
        self._simulated: Counter[str] = Counter()

    def used(self, action: str) -> int:
        logged = self.store.count_actions_since(self.account, action, utcnow() - timedelta(days=1))
        return logged + self._simulated[action]

    def remaining(self, action: str) -> int | None:
        """Actions left in the rolling 24h window, or None if the action is uncapped."""
        cap = self.limits.daily.get(action)
        if cap is None:
            return None
        return max(0, cap - self.used(action))

    def _pace(self) -> None:
        if self._last_at is None:
            return
        wait = random.uniform(self.limits.min_delay, self.limits.max_delay)
        elapsed = time.monotonic() - self._last_at
        if wait > elapsed:
            self._sleep(wait - elapsed)

    def act(self, action: str, target: str | None, fn: Callable[[], T]) -> T | None:
        """Run ``fn`` as one ``action`` on ``target``.

        Raises :class:`RateLimited` when the daily cap is reached. In dry-run mode the
        action is not executed and ``None`` is returned, but the cap is still counted and
        :class:`NotSupported` is raised when the backend can't perform the operation.
        """
        left = self.remaining(action)
        if left == 0:
            raise RateLimited(
                f"Daily limit for '{action}' reached ({self.limits.daily[action]}/24h)."
            )
        if self.dry_run:
            if (found := backend_op(fn)) is not None:
                backend, op = found
                if not backend.supports(op):
                    raise NotSupported(backend.name, op)
            self._simulated[action] += 1
            return None
        self._pace()
        try:
            result = fn()
        except Exception as e:
            self.store.log_action(self.account, action, target, ok=False, detail=str(e)[:500])
            raise
        finally:
            self._last_at = time.monotonic()
        self.store.log_action(self.account, action, target, ok=True)
        return result
