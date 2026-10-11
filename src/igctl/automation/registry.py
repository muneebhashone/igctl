"""Trigger handler base class, per-run helpers and the handler registry.

Adding a trigger: subclass :class:`Handler`, give it ``name``/``description`` and pydantic
``Trigger``/``Action`` models, implement :meth:`Handler.run`, and decorate it with
:func:`register`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, ClassVar, TypeVar

from pydantic import BaseModel, ConfigDict

from igctl.automation.rules import Rule, TextFilter
from igctl.backends.base import Backend, IgctlError
from igctl.core.config import BackendName
from igctl.core.models import Profile
from igctl.core.ratelimit import Guard
from igctl.core.storage import Store

T = TypeVar("T")


class Params(BaseModel):
    """Base for handler Trigger/Action models: unknown keys are errors."""

    model_config = ConfigDict(extra="forbid")


class _SafeVars(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render(template: str, **values: Any) -> str:
    """``str.format_map`` that leaves unknown ``{placeholders}`` untouched."""
    return template.format_map(_SafeVars({k: "" if v is None else v for k, v in values.items()}))


@dataclass
class RunStats:
    scanned: int = 0
    matched: int = 0
    acted: int = 0
    skipped: int = 0
    errors: int = 0
    notes: list[str] = field(default_factory=list)

    def note(self, msg: str) -> None:
        self.notes.append(msg)


@dataclass
class RuleRun:
    """Everything a handler needs to execute one rule once."""

    rule: Rule
    trigger: Any  # instance of handler.Trigger
    action: Any  # instance of handler.Action
    backend: Backend
    store: Store
    guard: Guard
    account: str
    max_per_run: int
    stats: RunStats = field(default_factory=RunStats)

    @property
    def filter(self) -> TextFilter:
        return self.rule.filter

    @property
    def dry_run(self) -> bool:
        return self.guard.dry_run

    @property
    def budget(self) -> int:
        return max(0, self.max_per_run - self.stats.acted)

    @property
    def namespace(self) -> str:
        return f"{self.rule.type}:{self.rule.name}"

    @cached_property
    def me(self) -> Profile:
        return self.backend.me()

    def seen(self, key: str, namespace: str | None = None) -> bool:
        return self.store.is_seen(self.account, namespace or self.namespace, key)

    def mark(self, key: str, namespace: str | None = None) -> None:
        """Record ``key`` as handled (no-op in dry-run so a real run still acts)."""
        if not self.dry_run:
            self.store.mark_seen(self.account, namespace or self.namespace, key)

    def act(self, action: str, target: str | None, fn: Callable[[], T]) -> bool:
        """Perform one guarded write. True if it ran (or would have, in dry-run).

        :class:`~igctl.backends.base.RateLimited` propagates so the engine stops the rule;
        other errors are counted and swallowed so one bad item doesn't end the run.
        """
        from igctl.backends.base import NotSupported, RateLimited

        try:
            self.guard.act(action, target, fn)
        except (RateLimited, NotSupported):
            raise
        except Exception as e:  # noqa: BLE001 - recorded in the action log by Guard
            self.stats.errors += 1
            self.stats.note(f"{action} {target}: {e}")
            return False
        return True


class Handler(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    Trigger: ClassVar[type[BaseModel]] = Params
    Action: ClassVar[type[BaseModel]] = Params
    default_max_per_run: ClassVar[int] = 20
    # Backend to prefer when the rule doesn't set one (None = the account's default).
    prefer_backend: ClassVar[BackendName | None] = "private"

    @abstractmethod
    def run(self, run: RuleRun) -> None:
        """Find matching items and act on at most ``run.budget`` of them."""


_REGISTRY: dict[str, type[Handler]] = {}


def register[H: type[Handler]](cls: H) -> H:
    if cls.name in _REGISTRY:
        raise ValueError(f"Duplicate automation trigger '{cls.name}'")
    _REGISTRY[cls.name] = cls
    return cls


def _ensure_builtin() -> None:
    import igctl.automation.handlers  # noqa: F401 - registers built-ins


def get_handler(name: str, missing_ok: bool = False) -> type[Handler] | None:
    _ensure_builtin()
    handler = _REGISTRY.get(name)
    if handler is None and not missing_ok:
        raise IgctlError(f"Unknown automation trigger '{name}'.")
    return handler


def handler_names() -> list[str]:
    _ensure_builtin()
    return sorted(_REGISTRY)


def all_handlers() -> list[type[Handler]]:
    _ensure_builtin()
    return [_REGISTRY[n] for n in sorted(_REGISTRY)]
