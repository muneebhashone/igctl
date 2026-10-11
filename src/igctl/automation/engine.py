"""Evaluate which rules are due, run them through their handlers, record the outcome."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from igctl.automation.registry import Handler, RuleRun, get_handler
from igctl.automation.rules import Rule, RuleSet, next_run
from igctl.automation.state import RuleState
from igctl.backends.base import ActionBlocked, AuthError, IgctlError, NotSupported, RateLimited
from igctl.core.context import Context
from igctl.core.ratelimit import Guard
from igctl.core.storage import utcnow

ContextFactory = Callable[[str | None], Context]

#: How long automation stops for an account after Instagram blocks an action, and after an
#: authentication failure (expired session, challenge): retrying sooner is what escalates
#: blocks to checkpoints or suspensions.
BLOCK_COOLDOWN = timedelta(hours=24)
AUTH_COOLDOWN = timedelta(hours=1)


@dataclass
class RuleResult:
    rule: str
    account: str
    trigger: str
    status: str  # ok | dry-run | capped | unsupported | blocked | halted | error
    scanned: int = 0
    matched: int = 0
    acted: int = 0
    errors: int = 0
    message: str = ""
    seconds: float = 0.0
    next_run: datetime | None = None

    def as_row(self) -> dict:
        return asdict(self)


class Engine:
    def __init__(
        self,
        ruleset: RuleSet,
        context_for: ContextFactory,
        *,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], datetime] = utcnow,
    ):
        """``context_for(account_name)`` builds a Context; ``sleep`` overrides Guard pacing."""
        self.ruleset = ruleset
        self._context_for = context_for
        self._sleep = sleep
        self._clock = clock
        self._contexts: dict[str | None, Context] = {}
        self._guards: dict[str, Guard] = {}
        self._states: dict[str, RuleState] = {}
        # In-memory last runs, so dry-run loops still respect schedules.
        self._ran: dict[tuple[str, str], datetime] = {}
        # In-memory account halts (dry-run never persists them).
        self._halts: dict[str, tuple[datetime, str]] = {}

    # --- plumbing ------------------------------------------------------------
    def context(self, rule: Rule) -> Context:
        if rule.account not in self._contexts:
            self._contexts[rule.account] = self._context_for(rule.account)
        return self._contexts[rule.account]

    def _state(self, ctx: Context) -> RuleState:
        key = str(ctx.store.path)
        if key not in self._states:
            self._states[key] = RuleState(ctx.store)
        return self._states[key]

    def _guard(self, ctx: Context) -> Guard:
        # One Guard per account so pacing carries across rules within a cycle.
        name = ctx.account.name
        if name not in self._guards:
            self._guards[name] = (
                ctx.guard
                if self._sleep is None
                else Guard(name, ctx.account.limits, ctx.store, ctx.dry_run, sleep=self._sleep)
            )
        return self._guards[name]

    # --- scheduling ----------------------------------------------------------
    def _halted(self, ctx: Context, account: str) -> tuple[datetime, str] | None:
        now = self._clock()
        mem = self._halts.get(account)
        if mem and mem[0] > now:
            return mem
        return None if ctx.dry_run else self._state(ctx).halted(account, now)

    def _halt(self, ctx: Context, account: str, cooldown: timedelta, reason: str) -> None:
        until = self._clock() + cooldown
        self._halts[account] = (until, reason)
        if not ctx.dry_run:
            self._state(ctx).halt(account, until, reason)

    def selected(self, only: str | None = None) -> list[Rule]:
        if only:
            return [self.ruleset.get(only)]
        return [r for r in self.ruleset.rules if r.enabled]

    def next_due(self, rule: Rule) -> datetime:
        ctx = self.context(rule)
        account = ctx.account.name
        last = self._state(ctx).last(account, rule.name)
        times = [t for t in (last.at if last else None, self._ran.get((account, rule.name))) if t]
        return next_run(rule.every, max(times)) if times else self._clock()

    def is_due(self, rule: Rule, now: datetime | None = None) -> bool:
        return self.next_due(rule) <= (now or self._clock())

    # --- execution -----------------------------------------------------------
    def run_once(self, only: str | None = None, force: bool = False) -> list[RuleResult]:
        """Run every selected rule that is due (all of them with ``force``)."""
        results = []
        for rule in self.selected(only):
            try:
                due = force or self.is_due(rule)
            except IgctlError as e:
                results.append(
                    RuleResult(rule.name, rule.account or "?", rule.type, "error", message=str(e))
                )
                continue
            if due:
                results.append(self.run_rule(rule))
        return results

    def run_rule(self, rule: Rule) -> RuleResult:
        started = time.monotonic()
        result = RuleResult(rule.name, rule.account or "?", rule.type, "ok")
        try:
            ctx = self.context(rule)
            result.account = ctx.account.name
            handler_cls = get_handler(rule.type)
            assert handler_cls is not None
            run = self._prepare(rule, handler_cls, ctx)
        except Exception as e:  # noqa: BLE001 - bad account/backend: report, don't crash
            result.status, result.message = "error", str(e) or type(e).__name__
            return result
        if halt := self._halted(ctx, result.account):
            until, reason = halt
            result.status = "halted"
            result.message = f"account paused until {until.isoformat(timespec='minutes')}: {reason}"
            return result

        guard = run.guard
        base_limits = guard.limits
        overrides = {
            k: v
            for k, v in (("min_delay", rule.limits.min_delay), ("max_delay", rule.limits.max_delay))
            if v is not None
        }
        if overrides:
            guard.limits = base_limits.model_copy(update=overrides)
        try:
            handler_cls().run(run)
            result.status = "dry-run" if run.dry_run else "ok"
        except RateLimited as e:
            result.status, result.message = "capped", str(e)
        except NotSupported as e:
            result.status, result.message = "unsupported", str(e)
        except ActionBlocked as e:
            # Stop every rule of this account, not just this one, for a long cooldown.
            result.status, result.message = "blocked", str(e)
            self._halt(ctx, result.account, BLOCK_COOLDOWN, str(e))
        except AuthError as e:
            result.status, result.message = "error", f"{type(e).__name__}: {e}"
            self._halt(ctx, result.account, AUTH_COOLDOWN, str(e))
        except Exception as e:  # noqa: BLE001 - one broken rule must not kill the loop
            result.status, result.message = "error", f"{type(e).__name__}: {e}"
        finally:
            guard.limits = base_limits

        stats = run.stats
        result.scanned, result.matched, result.acted = stats.scanned, stats.matched, stats.acted
        result.errors = stats.errors
        result.message = "; ".join(filter(None, [result.message, *stats.notes]))
        result.seconds = round(time.monotonic() - started, 2)
        now = self._clock()
        self._ran[(result.account, rule.name)] = now
        if not run.dry_run:
            self._state(ctx).record(
                result.account, rule.name, now, result.status, result.message[:500]
            )
        result.next_run = next_run(rule.every, now)
        return result

    def _prepare(self, rule: Rule, handler_cls: type[Handler], ctx: Context) -> RuleRun:
        backend = ctx.backend(prefer=rule.backend or handler_cls.prefer_backend)
        return RuleRun(
            rule=rule,
            trigger=handler_cls.Trigger.model_validate(rule.trigger_params()),
            action=handler_cls.Action.model_validate(rule.action),
            backend=backend,
            store=ctx.store,
            guard=self._guard(ctx),
            account=ctx.account.name,
            max_per_run=(
                rule.limits.max_per_run
                if rule.limits.max_per_run is not None
                else handler_cls.default_max_per_run
            ),
        )

    def loop(
        self,
        only: str | None = None,
        on_results: Callable[[list[RuleResult]], None] | None = None,
        *,
        max_sleep: float = 60.0,
        max_cycles: int | None = None,
        wait: Callable[[float], None] = time.sleep,
    ) -> None:
        """Run due rules forever (or ``max_cycles`` times), sleeping until the next is due."""
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            cycles += 1
            results = self.run_once(only)
            if results and on_results:
                on_results(results)
            rules = self.selected(only)
            if not rules:
                return
            try:
                soonest = min(self.next_due(r) for r in rules)
            except IgctlError:
                soonest = self._clock() + timedelta(seconds=max_sleep)
            delay = (soonest - self._clock()).total_seconds()
            if max_cycles is None or cycles < max_cycles:
                wait(min(max(delay, 1.0), max_sleep))
