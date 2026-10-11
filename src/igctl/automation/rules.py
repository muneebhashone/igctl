"""Rule file schema (``rules.yaml``), schedule parsing and text filters."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import yaml
from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from igctl.backends.base import IgctlError
from igctl.core.config import BackendName

_INTERVAL_RE = re.compile(r"(\d+)\s*([smhd])")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


class RulesError(IgctlError):
    """The rules file is missing, malformed, or fails validation."""


def parse_interval(text: str) -> timedelta | None:
    """Parse ``"15m"``, ``"2h"``, ``"1h30m"``, ``"1d"``; None if ``text`` is not an interval."""
    compact = text.replace(" ", "").lower()
    if not compact or _INTERVAL_RE.sub("", compact):
        return None
    delta = timedelta()
    for amount, unit in _INTERVAL_RE.findall(compact):
        delta += timedelta(**{_UNITS[unit]: int(amount)})
    return delta if delta > timedelta() else None


def next_run(every: str, last: datetime) -> datetime:
    """When a rule scheduled ``every`` (interval or cron) is next due after ``last``."""
    interval = parse_interval(every)
    if interval is not None:
        return last + interval
    return croniter(every, last).get_next(datetime)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextFilter(Strict):
    """Case-insensitive keyword / regex matching applied to an item's text."""

    keywords: list[str] = Field(default_factory=list)
    regex: str | None = None
    match: Literal["any", "all"] = "any"
    exclude: list[str] = Field(default_factory=list, description="skip text containing any")

    @field_validator("regex")
    @classmethod
    def _compiles(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                re.compile(v)
            except re.error as e:
                raise ValueError(f"invalid regex: {e}") from e
        return v

    @property
    def empty(self) -> bool:
        return not (self.keywords or self.regex or self.exclude)

    def matches(self, text: str | None) -> bool:
        low = (text or "").lower()
        if any(w.lower() in low for w in self.exclude):
            return False
        checks: list[bool] = [w.lower() in low for w in self.keywords]
        if self.regex:
            checks.append(re.search(self.regex, text or "", re.IGNORECASE) is not None)
        if not checks:
            return True
        return any(checks) if self.match == "any" else all(checks)


class RuleLimits(Strict):
    max_per_run: int | None = Field(None, ge=0, description="defaults per trigger")
    min_delay: float | None = Field(None, ge=0)
    max_delay: float | None = Field(None, ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> RuleLimits:
        if (
            self.min_delay is not None
            and self.max_delay is not None
            and self.min_delay > self.max_delay
        ):
            raise ValueError("min_delay must be <= max_delay")
        return self


class Rule(Strict):
    """One validated automation rule. ``trigger``/``action`` are the handler's own models."""

    name: str = Field(min_length=1)
    account: str | None = None
    backend: BackendName | None = None
    enabled: bool = True
    every: str = "1h"
    trigger: dict[str, Any]
    filter: TextFilter = Field(default_factory=TextFilter)
    action: dict[str, Any] = Field(default_factory=dict)
    limits: RuleLimits = Field(default_factory=RuleLimits)

    @property
    def type(self) -> str:
        return str(self.trigger["type"])

    @field_validator("trigger", mode="before")
    @classmethod
    def _trigger_shape(cls, v: Any) -> Any:
        if isinstance(v, str):
            return {"type": v}
        if isinstance(v, dict) and not v.get("type"):
            raise ValueError("trigger needs a 'type' (see `igctl automate list-triggers`)")
        return v

    @field_validator("every")
    @classmethod
    def _schedule(cls, v: str) -> str:
        v = v.strip()
        if parse_interval(v) is None and not croniter.is_valid(v):
            raise ValueError(
                f"'{v}' is neither an interval like '15m' / '2h' / '1d' nor a cron expression"
            )
        return v

    def trigger_params(self) -> dict[str, Any]:
        return {k: v for k, v in self.trigger.items() if k != "type"}


class RuleSet(BaseModel):
    rules: list[Rule]
    path: Path | None = None

    def get(self, name: str) -> Rule:
        for r in self.rules:
            if r.name == name:
                return r
        raise RulesError(f"No rule named '{name}'. Known: {', '.join(self.names) or 'none'}.")

    @property
    def names(self) -> list[str]:
        return [r.name for r in self.rules]


def _fmt_errors(prefix: str, err: ValidationError) -> list[str]:
    out = []
    for e in err.errors():
        loc = ".".join(str(p) for p in e["loc"])
        msg = e["msg"].removeprefix("Value error, ")
        out.append(f"{prefix}{'.' + loc if loc else ''}: {msg}")
    return out


def _merge_defaults(defaults: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    merged = {**defaults, **raw}
    if isinstance(defaults.get("limits"), dict) and isinstance(raw.get("limits"), dict):
        merged["limits"] = {**defaults["limits"], **raw["limits"]}
    return merged


def parse_rules(data: Any, source: str = "rules") -> RuleSet:
    """Validate already-parsed YAML. Raises :class:`RulesError` listing every problem."""
    from igctl.automation.registry import get_handler, handler_names

    defaults: dict[str, Any] = {}
    if isinstance(data, dict):
        defaults = data.get("defaults") or {}
        unknown = set(data) - {"defaults", "rules"}
        if unknown:
            raise RulesError(f"{source}: unknown top-level keys: {', '.join(sorted(unknown))}")
        data = data.get("rules")
    if not isinstance(data, list):
        raise RulesError(f"{source}: expected a list of rules (or a mapping with 'rules:').")
    if not isinstance(defaults, dict):
        raise RulesError(f"{source}: 'defaults' must be a mapping.")

    errors: list[str] = []
    rules: list[Rule] = []
    for i, raw in enumerate(data):
        label = f"rules[{i}]"
        if not isinstance(raw, dict):
            errors.append(f"{label}: expected a mapping, got {type(raw).__name__}")
            continue
        if raw.get("name"):
            label += f" ({raw['name']})"
        try:
            rule = Rule.model_validate(_merge_defaults(defaults, raw))
        except ValidationError as e:
            errors.extend(_fmt_errors(label, e))
            continue
        handler = get_handler(rule.type, missing_ok=True)
        if handler is None:
            errors.append(
                f"{label}.trigger.type: unknown trigger '{rule.type}' "
                f"(known: {', '.join(handler_names())})"
            )
            continue
        for part, model, payload in (
            ("trigger", handler.Trigger, rule.trigger_params()),
            ("action", handler.Action, rule.action),
        ):
            try:
                model.model_validate(payload)
            except ValidationError as e:
                errors.extend(_fmt_errors(f"{label}.{part}", e))
        rules.append(rule)

    seen: set[str] = set()
    for r in rules:
        if r.name in seen:
            errors.append(f"duplicate rule name '{r.name}'")
        seen.add(r.name)
    if errors:
        raise RulesError(f"{source} is invalid:\n  " + "\n  ".join(errors))
    return RuleSet(rules=rules)


def load_rules(path: str | Path) -> RuleSet:
    p = Path(path).expanduser()
    if not p.is_file():
        raise RulesError(f"Rules file not found: {p}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise RulesError(f"{p}: invalid YAML: {e}") from e
    ruleset = parse_rules(data if data is not None else [], source=str(p))
    ruleset.path = p
    return ruleset
