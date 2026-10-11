"""`igctl auth`: log in, list, switch and tune accounts."""

from __future__ import annotations

from typing import Any

import httpx
import typer

from igctl.backends.base import AuthError, IgctlError
from igctl.cli._common import emit, get_ctx, ok, warn
from igctl.core.config import Account, Config, delete_secret, get_secret, sessions_dir, set_secret
from igctl.core.context import make_backend

app = typer.Typer(no_args_is_help=True)

# Keyring "kind" names shared with the backends.
TOKEN = "token"
PASSWORD = "password"
GRAPH_HOSTS = ("graph.facebook.com", "graph.instagram.com")


def _validate_name(name: str) -> str:
    if not name or not all(c.isalnum() or c in "-_" for c in name):
        raise typer.BadParameter("account name may only contain letters, digits, '-' and '_'")
    return name


@app.command()
def login(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Local alias for this account, e.g. 'brand'."),
    backend: str = typer.Option("graph", "--backend", help="graph | private"),
    token: str | None = typer.Option(None, "--token", help="Graph API access token."),
    ig_user_id: str | None = typer.Option(None, "--ig-user-id", help="Instagram user id."),
    host: str = typer.Option("graph.facebook.com", "--host", help=" | ".join(GRAPH_HOSTS)),
    username: str | None = typer.Option(None, "--username", "-u", help="Instagram username."),
    password: str | None = typer.Option(None, "--password", help="Prompted if omitted."),
    two_factor: str | None = typer.Option(None, "--2fa", help="Two-factor / verification code."),
    proxy: str | None = typer.Option(None, "--proxy", help="Proxy URL for the private backend."),
    default: bool = typer.Option(False, "--default", help="Make this the default account."),
) -> None:
    """Add (or re-authenticate) an account and verify the credentials work."""
    c = get_ctx(ctx)
    _validate_name(name)
    if backend not in ("graph", "private"):
        raise typer.BadParameter("--backend must be 'graph' or 'private'")
    cfg = Config.load()
    existing = cfg.accounts.get(name)
    acct = existing.model_copy() if existing else Account(name=name)
    acct.backend = backend  # type: ignore[assignment]

    if backend == "graph":
        if host not in GRAPH_HOSTS:
            raise typer.BadParameter(f"--host must be one of {', '.join(GRAPH_HOSTS)}")
        token = token or typer.prompt("Access token", hide_input=True)
        acct.graph_host = host
        if ig_user_id:
            acct.ig_user_id = ig_user_id
        previous = get_secret(name, TOKEN)
        set_secret(name, TOKEN, token.strip())
        try:
            profile = make_backend("graph", acct).me()
        except Exception as e:
            # Roll back so a typo doesn't clobber a working token.
            if previous:
                set_secret(name, TOKEN, previous)
            else:
                delete_secret(name, TOKEN)
            raise AuthError(f"Graph login failed: {e}") from e
        acct.ig_user_id = acct.ig_user_id or profile.id
    else:
        username = username or typer.prompt("Instagram username")
        password = password or typer.prompt("Password", hide_input=True)
        acct.username = username
        if proxy is not None:
            acct.proxy = proxy or None
        be = make_backend("private", acct)
        login_fn = getattr(be, "login", None)
        if login_fn is None:
            raise IgctlError("The private backend does not implement login().")
        try:
            login_fn(username, password, verification_code=two_factor)
        except IgctlError:
            raise
        except Exception as e:
            raise AuthError(f"Instagram login failed: {e}") from e
        set_secret(name, PASSWORD, password)
        profile = be.me()

    acct.username = profile.username or acct.username
    cfg.accounts[name] = acct
    if default or not cfg.default_account or cfg.default_account not in cfg.accounts:
        cfg.default_account = name
    cfg.save()
    ok(f"Logged in as @{acct.username} ({backend}) as account '{name}'.")
    if cfg.default_account == name:
        ok(f"'{name}' is the default account.")
    if c.json_output:
        emit(c, {"account": name, "backend": backend, "profile": profile})


@app.command("list")
def list_accounts(ctx: typer.Context) -> None:
    """List configured accounts."""
    c = get_ctx(ctx)
    cfg = Config.load()
    rows = [
        {
            "default": "*" if n == cfg.default_account else "",
            "name": n,
            "backend": a.backend,
            "username": a.username,
            "ig_user_id": a.ig_user_id,
            "host": a.graph_host if a.backend == "graph" else None,
            "proxy": a.proxy,
        }
        for n, a in sorted(cfg.accounts.items())
    ]
    if not rows and not c.json_output:
        warn("No accounts yet. Run `igctl auth login NAME`.")
        return
    emit(c, rows, title="Accounts")


@app.command()
def use(ctx: typer.Context, name: str = typer.Argument(...)) -> None:
    """Set the default account."""
    cfg = Config.load()
    if name not in cfg.accounts:
        raise IgctlError(f"Unknown account '{name}'. See `igctl auth list`.")
    cfg.default_account = name
    cfg.save()
    ok(f"Default account is now '{name}'.")


@app.command()
def whoami(ctx: typer.Context) -> None:
    """Show the active account's live profile."""
    c = get_ctx(ctx)
    profile = c.backend().me()
    if not c.json_output:
        ok(f"account '{c.account.name}' via {c.backend().name}")
    emit(
        c,
        profile,
        columns=[
            "id",
            "username",
            "full_name",
            "followers_count",
            "following_count",
            "media_count",
            "is_business",
            "is_verified",
        ],
    )


@app.command()
def logout(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    purge: bool = typer.Option(False, "--purge", help="Also remove the account from config."),
) -> None:
    """Forget stored credentials and session (and with --purge, the account itself)."""
    cfg = Config.load()
    if name not in cfg.accounts:
        raise IgctlError(f"Unknown account '{name}'.")
    for kind in (TOKEN, PASSWORD):
        delete_secret(name, kind)
    session = sessions_dir() / f"{name}.json"
    session.unlink(missing_ok=True)
    if purge:
        del cfg.accounts[name]
        if cfg.default_account == name:
            cfg.default_account = next(iter(cfg.accounts), None)
        cfg.save()
        ok(f"Removed account '{name}'.")
    else:
        ok(f"Logged out of '{name}' (credentials and session removed).")


@app.command("set-limit")
def set_limit(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Account alias."),
    action: str | None = typer.Argument(None, help="Action, e.g. like, follow, dm, publish."),
    count: int | None = typer.Argument(None, min=0, help="Max actions per rolling 24h."),
    min_delay: float | None = typer.Option(None, "--min-delay", min=0),
    max_delay: float | None = typer.Option(None, "--max-delay", min=0),
) -> None:
    """Set a daily cap for an action and/or the delay range between actions."""
    c = get_ctx(ctx)
    cfg = Config.load()
    if name not in cfg.accounts:
        raise IgctlError(f"Unknown account '{name}'.")
    if (action is None) != (count is None):
        raise typer.BadParameter("give both ACTION and N, or neither")
    if action is None and min_delay is None and max_delay is None:
        raise typer.BadParameter("nothing to change: pass ACTION N and/or --min-delay/--max-delay")
    limits = cfg.accounts[name].limits
    if action is not None and count is not None:
        limits.daily[action] = count
    if min_delay is not None:
        limits.min_delay = min_delay
    if max_delay is not None:
        limits.max_delay = max_delay
    if limits.min_delay > limits.max_delay:
        raise typer.BadParameter("--min-delay must not exceed --max-delay")
    cfg.save()
    ok(f"Updated limits for '{name}'.")
    emit(c, limits)


def refresh_ig_token(token: str, client: httpx.Client | None = None) -> dict[str, Any]:
    """Refresh a long-lived Instagram-Login token (valid >=24h, not expired)."""
    with client or httpx.Client(timeout=30) as http:
        r = http.get(
            "https://graph.instagram.com/refresh_access_token",
            params={"grant_type": "ig_refresh_token", "access_token": token},
        )
    return _graph_json(r)


def exchange_fb_token(
    token: str, app_id: str, app_secret: str, version: str, client: httpx.Client | None = None
) -> dict[str, Any]:
    """Exchange a (short- or long-lived) Facebook user token for a fresh long-lived one."""
    with client or httpx.Client(timeout=30) as http:
        r = http.get(
            f"https://graph.facebook.com/{version}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": token,
            },
        )
    return _graph_json(r)


def _graph_json(r: httpx.Response) -> dict[str, Any]:
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.is_error or "access_token" not in data:
        msg = (data.get("error") or {}).get("message") if isinstance(data, dict) else None
        raise AuthError(f"Token refresh failed ({r.status_code}): {msg or r.text[:200]}")
    return data


@app.command("token-refresh")
def token_refresh(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Graph account alias."),
    app_id: str | None = typer.Option(None, "--app-id", help="Meta app id (Facebook tokens)."),
    app_secret: str | None = typer.Option(
        None, "--app-secret", envvar="IGCTL_APP_SECRET", help="Meta app secret."
    ),
) -> None:
    """Extend a Graph API long-lived access token and store the new one."""
    c = get_ctx(ctx)
    cfg = Config.load()
    acct = cfg.accounts.get(name)
    if acct is None:
        raise IgctlError(f"Unknown account '{name}'.")
    if acct.backend != "graph":
        raise IgctlError(f"'{name}' uses the private backend; it has no Graph token.")
    token = get_secret(name, TOKEN)
    if not token:
        raise AuthError(f"No token stored for '{name}'. Run `igctl auth login {name}`.")

    if acct.graph_host == "graph.instagram.com":
        data = refresh_ig_token(token)
    elif app_id and app_secret:
        data = exchange_fb_token(token, app_id, app_secret, acct.graph_version)
    else:
        raise IgctlError(
            "Facebook-login tokens (graph.facebook.com) cannot be refreshed in place; they are "
            "re-issued by exchanging the current token with your app credentials "
            "(grant_type=fb_exchange_token). Re-run with --app-id and --app-secret "
            "(or IGCTL_APP_SECRET). Page tokens derived from a long-lived user token "
            "do not expire."
        )
    set_secret(name, TOKEN, data["access_token"])
    expires = data.get("expires_in")
    days = f" (expires in ~{int(expires) // 86400} days)" if expires else ""
    ok(f"Refreshed token for '{name}'{days}.")
    if c.json_output:
        emit(c, {"account": name, "expires_in": expires, "token_type": data.get("token_type")})
