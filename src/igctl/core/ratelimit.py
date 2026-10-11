"""Human-paced execution of automated actions with per-account daily caps.

Every write action that automation performs (like, comment, follow, dm, publish...) should go
through :meth:`Guard.act` so it is delayed, capped, logged and dry-run aware.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from datetime import timedelta
from typing import TypeVar

from igctl.backends.base import RateLimited
from igctl.core.config import Limits
from igctl.core.storage import Store, utcnow

T = TypeVar("T")


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

    def used(self, action: str) -> int:
        return self.store.count_actions_since(self.account, action, utcnow() - timedelta(days=1))

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
        action is not executed and ``None`` is returned.
        """
        left = self.remaining(action)
        if left == 0:
            raise RateLimited(
                f"Daily limit for '{action}' reached ({self.limits.daily[action]}/24h)."
            )
        if self.dry_run:
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
