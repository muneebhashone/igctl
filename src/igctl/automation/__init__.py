"""YAML-driven automation: rules, trigger handlers and the scheduling engine."""

from igctl.automation.engine import Engine, RuleResult
from igctl.automation.registry import Handler, Params, RuleRun, all_handlers, get_handler, register
from igctl.automation.rules import Rule, RulesError, RuleSet, TextFilter, load_rules, parse_rules

__all__ = [
    "Engine",
    "Handler",
    "Params",
    "Rule",
    "RuleResult",
    "RuleRun",
    "RuleSet",
    "RulesError",
    "TextFilter",
    "all_handlers",
    "get_handler",
    "load_rules",
    "parse_rules",
    "register",
]
