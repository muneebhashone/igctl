"""Helpers shared by every CLI sub-command module."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import Any

import typer
from pydantic import BaseModel
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from igctl.core.context import Context

console = Console()
err_console = Console(stderr=True)


def get_ctx(ctx: typer.Context) -> Context:
    """The igctl Context created by the root callback."""
    obj = ctx.find_root().obj
    if not isinstance(obj, Context):  # invoked without the root callback (tests)
        obj = Context()
        ctx.find_root().obj = obj
    return obj


def _plain(item: Any) -> Any:
    if isinstance(item, BaseModel):
        return item.model_dump(mode="json")
    if isinstance(item, dict):
        return {k: _plain(v) for k, v in item.items()}
    if isinstance(item, list | tuple):
        return [_plain(v) for v in item]
    return item


def emit(
    ctx: Context,
    data: Any,
    columns: Sequence[str] | None = None,
    title: str | None = None,
) -> None:
    """Print ``data`` as JSON (``--json``) or as a rich table / key-value list.

    ``data`` may be a model, dict, or a list of either. ``columns`` picks and orders the
    fields shown in table mode (JSON mode always prints everything).
    """
    plain = _plain(data)
    if ctx.json_output:
        console.print_json(json.dumps(plain, default=str))
        return
    if isinstance(plain, list):
        rows: Iterable[dict] = plain
        cols = list(columns or (plain[0].keys() if plain else []))
        table = Table(title=title, show_lines=False)
        for c in cols:
            table.add_column(c)
        for r in rows:
            table.add_row(*(_cell(r.get(c)) for c in cols))
        console.print(table)
        return
    if isinstance(plain, dict):
        table = Table(title=title, show_header=False, box=None)
        table.add_column(style="bold cyan")
        table.add_column()
        for k in columns or plain.keys():
            table.add_row(k, _cell(plain.get(k)))
        console.print(table)
        return
    console.print(plain)


def _cell(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:,.2f}"
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, dict | list):
        return json.dumps(v, default=str)
    s = str(v)
    return s if len(s) <= 80 else s[:77] + "..."


def ok(msg: str) -> None:
    err_console.print(f"[green]✓[/green] {escape(msg)}")


def warn(msg: str) -> None:
    err_console.print(f"[yellow]![/yellow] {escape(msg)}")


def confirm_or_abort(ctx: Context, prompt: str, yes: bool) -> None:
    if yes or ctx.dry_run:
        return
    if not typer.confirm(prompt):
        raise typer.Abort()
