"""`igctl automate`: run YAML-defined automation rules."""

from __future__ import annotations

from pathlib import Path

import typer

from igctl.automation import Engine, RuleResult, all_handlers, get_handler, load_rules
from igctl.automation.state import RuleState
from igctl.backends.base import IgctlError
from igctl.cli._common import emit, get_ctx, ok, warn
from igctl.core.context import Context

app = typer.Typer(no_args_is_help=True)

RULES_ARG = typer.Argument(..., help="Path to a rules YAML file.", show_default=False)
RESULT_COLUMNS = ["rule", "account", "trigger", "status", "scanned", "matched", "acted", "errors"]


def _factory(root: Context):
    def context_for(account: str | None) -> Context:
        if account is None or account == root.account_name:
            return root
        return Context(
            account_name=account,
            backend_override=root.backend_override,
            dry_run=root.dry_run,
            json_output=root.json_output,
        )

    return context_for


def _flag(c: Context, value: bool) -> bool | str:
    """Booleans stay booleans in JSON; tables show yes/no (rich tables print bools as 1/0)."""
    return value if c.json_output else ("yes" if value else "no")


def _show(c: Context, results: list[RuleResult]) -> None:
    rows = [r.as_row() for r in results]
    title = "Automation run" + (" (dry run)" if c.dry_run else "")
    emit(c, rows, columns=RESULT_COLUMNS + ["message"], title=title)


@app.command()
def run(
    ctx: typer.Context,
    rules: Path = RULES_ARG,
    rule: str | None = typer.Option(None, "--rule", "-r", help="Only run this rule."),
    loop: bool = typer.Option(False, "--loop/--once", help="Keep running due rules forever."),
    force: bool = typer.Option(False, "--force", help="Ignore schedules; run now (--once)."),
    max_sleep: float = typer.Option(60.0, "--max-sleep", help="Max seconds between checks."),
) -> None:
    """Run the rules that are due (honours --dry-run, daily caps and delays)."""
    c = get_ctx(ctx)
    engine = Engine(load_rules(rules), _factory(c))
    if not loop:
        results = engine.run_once(rule, force=force)
        if not results:
            ok("Nothing due. Use --force to run anyway, or `automate status` to see schedules.")
            if c.json_output:
                emit(c, [])
            return
        _show(c, results)
        if any(r.status in ("error", "blocked") for r in results):
            raise typer.Exit(1)
        return
    if force:
        warn("--force is ignored with --loop.")
    ok(f"Running {len(engine.selected(rule))} rule(s) from {rules}; Ctrl+C to stop.")
    try:
        engine.loop(rule, on_results=lambda res: _show(c, res), max_sleep=max_sleep)
    except KeyboardInterrupt:
        ok("Stopped.")


@app.command()
def validate(ctx: typer.Context, rules: Path = RULES_ARG) -> None:
    """Check a rules file and show what it defines."""
    c = get_ctx(ctx)
    ruleset = load_rules(rules)
    rows = []
    for r in ruleset.rules:
        handler = get_handler(r.type)
        assert handler is not None
        rows.append(
            {
                "name": r.name,
                "trigger": r.type,
                "every": r.every,
                "account": r.account or "(default)",
                "enabled": _flag(c, r.enabled),
                "max_per_run": r.limits.max_per_run
                if r.limits.max_per_run is not None
                else handler.default_max_per_run,
            }
        )
    emit(c, rows, title=f"{rules}: {len(rows)} rule(s)")
    if not c.json_output:
        ok("Rules file is valid.")


@app.command()
def status(ctx: typer.Context, rules: Path = RULES_ARG) -> None:
    """Show each rule's last run and when it is next due."""
    c = get_ctx(ctx)
    engine = Engine(load_rules(rules), _factory(c))
    rows = []
    for r in engine.ruleset.rules:
        row: dict = {"rule": r.name, "enabled": _flag(c, r.enabled), "every": r.every}
        try:
            rctx = engine.context(r)
            last = RuleState(rctx.store).last(rctx.account.name, r.name)
            row |= {
                "account": rctx.account.name,
                "last_run": last.at.isoformat(timespec="seconds") if last else None,
                "last_status": last.status if last else None,
                "next_due": engine.next_due(r).isoformat(timespec="seconds"),
            }
        except IgctlError as e:
            row |= {"account": r.account, "last_status": f"error: {e}"}
        rows.append(row)
    emit(
        c,
        rows,
        columns=["rule", "account", "enabled", "every", "last_run", "last_status", "next_due"],
        title="Automation status",
    )


@app.command("list-triggers")
def list_triggers(ctx: typer.Context) -> None:
    """List available triggers with their trigger/action options."""
    c = get_ctx(ctx)

    def fields(model) -> str:
        parts = []
        for name, f in model.model_fields.items():
            parts.append(name if not f.is_required() else f"{name}*")
        return ", ".join(parts) or "-"

    rows = [
        {
            "trigger": h.name,
            "description": h.description,
            "trigger_options": fields(h.Trigger),
            "action_options": fields(h.Action),
            "default_max": h.default_max_per_run,
            "backend": h.prefer_backend or "account default",
        }
        for h in all_handlers()
    ]
    emit(c, rows, title="Automation triggers (* = required)")


@app.command()
def log(
    ctx: typer.Context,
    limit: int = typer.Option(30, "--limit", "-n", min=1, help="How many actions to show."),
    all_accounts: bool = typer.Option(False, "--all", help="Show every account's actions."),
) -> None:
    """Show recent automated actions from the action log."""
    c = get_ctx(ctx)
    account = None
    if not all_accounts:
        try:
            account = c.account.name
        except IgctlError:
            account = None
    rows = [
        {
            "at": r["at"][:19].replace("T", " "),
            "account": r["account"],
            "action": r["action"],
            "target": r["target"],
            "ok": _flag(c, bool(r["ok"])),
            "detail": r["detail"] or "",
        }
        for r in c.store.recent_actions(account, limit)
    ]
    if not rows and not c.json_output:
        ok("No actions logged yet.")
        return
    emit(c, rows, title="Recent actions" + (f" ({account})" if account else ""))
