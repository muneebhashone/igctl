"""Private-API backend built on instagrapi (the Instagram mobile API).

It can do nearly everything the app can, but it violates Instagram's Terms of Use:
keep automated volume low (see :mod:`igctl.core.ratelimit`) and prefer the ``graph``
backend where it suffices.

Media ids: :class:`igctl.core.models.Media` ids produced here are instagrapi's full
media ids (``<pk>_<owner_pk>``). Every method taking a ``media_id`` also accepts a bare
pk, a shortcode or an instagram.com post/reel URL.
"""

from __future__ import annotations

import functools
import json
import re
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import pydantic
import requests
from instagrapi import Client
from instagrapi import exceptions as ig
from instagrapi import types as igt

from igctl.backends.base import (
    ActionBlocked,
    AuthError,
    Backend,
    IgctlError,
    RateLimited,
    TransientError,
)
from igctl.core.config import Account, get_secret, sessions_dir
from igctl.core.models import (
    Comment,
    DirectMessage,
    DirectThread,
    Insight,
    Media,
    MediaType,
    Profile,
    PublishRequest,
    PublishResult,
    UserSummary,
)
from igctl.core.refs import shortcode_to_pk

#: Random pause (seconds) instagrapi inserts before each private request.
DEFAULT_DELAY_RANGE = [1, 3]
FOLLOW_PAGE_SIZE = 100
#: Upper bound for hashtag scans: instagrapi treats amount=0 as "page forever".
MAX_HASHTAG_SCAN = 500
MAX_USER_SEARCH = 50
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}
_HASHTAG_RE = re.compile(r"#(\w+)", re.UNICODE)
_SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]{5,}$")


# --------------------------------------------------------------------------- errors
def map_error(exc: Exception) -> IgctlError:
    """Translate an instagrapi exception into an igctl error with an actionable message."""
    if isinstance(exc, IgctlError):
        return exc
    detail = str(getattr(exc, "message", "") or exc) or type(exc).__name__
    match exc:
        case ig.TwoFactorRequired():
            return AuthError(
                "Two-factor authentication required: re-run login with the 2FA code "
                "(e.g. `igctl auth login --code 123456`)."
            )
        case ig.ChallengeError() | ig.CaptchaChallengeRequired():
            return AuthError(
                f"Instagram requires a security challenge ({detail}). Approve the login in "
                "the Instagram app, then log in again interactively."
            )
        case ig.BadPassword() | ig.BadCredentials():
            return AuthError(f"Instagram rejected the credentials: {detail}")
        case ig.ReloginAttemptExceeded():
            return AuthError("Re-login attempts exceeded; wait a while and log in again.")
        case ig.LoginRequired() | ig.ClientLoginRequired() | ig.ClientUnauthorizedError():
            return AuthError("Instagram session expired or invalid; run `igctl auth login` again.")
        case ig.AccountSuspended():
            return AuthError(f"The Instagram account is suspended: {detail}")
        case ig.FeedbackRequired() | ig.SentryBlock():
            return ActionBlocked(
                f"Instagram blocked this action ({detail}). Stop automating for 24-48h and "
                "lower the daily limits."
            )
        case ig.PleaseWaitFewMinutes() | ig.RateLimitError() | ig.ClientThrottledError():
            return RateLimited(f"Instagram is rate limiting requests ({detail}); retry later.")
        case ig.ProxyAddressIsBlocked():
            return IgctlError("Instagram blocked the proxy IP; configure a different proxy.")
        case ig.PrivateAccount():
            return IgctlError(f"That account is private: {detail}")
        case ig.NotFoundError() | ig.ClientNotFoundError():
            return IgctlError(f"Not found: {detail}")
        case (
            ig.ClientConnectionError()
            | ig.ClientRequestTimeout()
            | ig.ClientIncompleteReadError()
            | requests.ConnectionError()
            | requests.Timeout()
            | httpx.TransportError()
        ):
            return TransientError(f"Network error talking to Instagram: {detail}")
        case ModuleNotFoundError() if "moviepy" in detail:
            return IgctlError(
                "Video uploads need MoviePy and ffmpeg: `uv add moviepy` and install ffmpeg."
            )
        case AssertionError() if "login required" in detail.lower():
            return AuthError("Instagram session expired or invalid; run `igctl auth login` again.")
        case httpx.HTTPStatusError() | requests.HTTPError():
            return IgctlError(f"Download failed: {detail}")
        case pydantic.ValidationError() | KeyError():
            return IgctlError(
                f"Instagram returned an unexpected response ({type(exc).__name__}: {detail})."
            )
        case _:
            return IgctlError(f"Instagram error ({type(exc).__name__}): {detail}")


# instagrapi also raises bare AssertionErrors ("Login required"; its own ValidationError
# subclasses AssertionError), KeyErrors and pydantic errors on unexpected payloads, and
# requests errors from downloads (and igctl fetches --url media with httpx); none of these
# may reach the CLI as a traceback.
_HANDLED: tuple[type[Exception], ...] = (
    ig.ClientError,
    ModuleNotFoundError,
    AssertionError,
    KeyError,
    pydantic.ValidationError,
    requests.RequestException,
    httpx.HTTPError,
)


@contextmanager
def _translated() -> Iterator[None]:
    try:
        yield
    except IgctlError:
        raise
    except _HANDLED as exc:
        raise map_error(exc) from exc


def _wrap[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    @functools.wraps(fn)
    def inner(*args: P.args, **kwargs: P.kwargs) -> R:
        with _translated():
            return fn(*args, **kwargs)

    return inner


def _write[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """Like :func:`_wrap`, and persist the session after a successful write.

    instagrapi rotates cookies, ``mid`` and authorization data as requests go out; long
    automation/scheduler runs would otherwise leave a stale session file behind.
    """
    wrapped = _wrap(fn)

    @functools.wraps(fn)
    def inner(*args: P.args, **kwargs: P.kwargs) -> R:
        result = wrapped(*args, **kwargs)
        self = args[0]
        if isinstance(self, PrivateBackend):
            self._persist_session()
        return result

    return inner


# -------------------------------------------------------------------------- mappers
def _s(value: Any) -> str | None:
    return None if value is None else str(value)


def to_user_summary(user: igt.UserShort | igt.User) -> UserSummary:
    return UserSummary(
        id=str(user.pk),
        username=user.username or "",
        full_name=user.full_name or None,
        is_private=user.is_private,
        is_verified=user.is_verified,
    )


def to_profile(user: igt.User) -> Profile:
    return Profile(
        id=str(user.pk),
        username=user.username,
        full_name=user.full_name or None,
        biography=user.biography or None,
        followers_count=user.follower_count,
        following_count=user.following_count,
        media_count=user.media_count,
        is_private=user.is_private,
        is_verified=user.is_verified,
        is_business=user.is_business,
        category=user.category_name or user.category or user.business_category_name,
        external_url=user.external_url,
        profile_pic_url=_s(user.profile_pic_url_hd or user.profile_pic_url),
    )


def _media_type(media_type: int, product_type: str | None) -> MediaType:
    if media_type == 8:
        return MediaType.CAROUSEL
    if media_type == 2:
        return MediaType.REEL if product_type == "clips" else MediaType.VIDEO
    return MediaType.IMAGE


def permalink(code: str | None, kind: MediaType | None = None) -> str | None:
    if not code:
        return None
    segment = "reel" if kind == MediaType.REEL else "p"
    return f"https://www.instagram.com/{segment}/{code}/"


def to_media(media: igt.Media) -> Media:
    kind = _media_type(media.media_type, media.product_type)
    media_url = media.video_url or media.thumbnail_url
    if kind == MediaType.CAROUSEL and media.resources:
        first = media.resources[0]
        media_url = first.video_url or first.thumbnail_url
    caption = media.caption_text or None
    return Media(
        id=str(media.id or media.pk),
        code=media.code,
        media_type=kind,
        caption=caption,
        taken_at=media.taken_at,
        like_count=media.like_count,
        comment_count=media.comment_count,
        view_count=media.play_count or media.view_count or None,
        permalink=permalink(media.code, kind),
        media_url=_s(media_url),
        thumbnail_url=_s(media.thumbnail_url),
        owner_username=media.user.username if media.user else None,
        hashtags=_HASHTAG_RE.findall(caption or ""),
    )


def story_to_media(story: igt.Story) -> Media:
    return Media(
        id=str(story.id or story.pk),
        code=story.code,
        media_type=MediaType.STORY,
        taken_at=story.taken_at,
        media_url=_s(story.video_url or story.thumbnail_url),
        thumbnail_url=_s(story.thumbnail_url),
        owner_username=story.user.username if story.user else None,
        permalink=(
            f"https://www.instagram.com/stories/{story.user.username}/{story.pk}/"
            if story.user and story.user.username
            else None
        ),
        hashtags=[h.hashtag.name for h in story.hashtags],
    )


def to_comment(comment: igt.Comment, media_id: str) -> Comment:
    return Comment(
        id=str(comment.pk),
        media_id=media_id,
        text=comment.text,
        username=comment.user.username or "",
        user_id=str(comment.user.pk),
        created_at=comment.created_at_utc,
        like_count=comment.like_count,
        replied_to_id=_s(comment.replied_to_comment_id),
    )


def to_message(msg: igt.DirectMessage, thread_id: str | None = None) -> DirectMessage:
    return DirectMessage(
        id=str(msg.id),
        thread_id=thread_id or _s(msg.thread_id) or "",
        user_id=_s(msg.user_id),
        text=msg.text,
        created_at=msg.timestamp,
    )


def _from_me(msg: igt.DirectMessage, my_user_id: str | None) -> bool | None:
    if msg.is_sent_by_viewer is not None:
        return bool(msg.is_sent_by_viewer)
    if my_user_id and msg.user_id is not None:
        return str(msg.user_id) == str(my_user_id)
    return None


def _thread_unread(
    thread: igt.DirectThread, last: igt.DirectMessage | None, from_me: bool | None, me: str | None
) -> bool:
    """Newest message is from someone else and newer than my last-seen marker."""
    if last is None or from_me is not False:
        return False
    seen = (thread.last_seen_at or {}).get(str(me)) if me else None
    seen_at = getattr(seen, "timestamp", None)
    if seen_at is None or last.timestamp is None:
        return True
    a, b = seen_at, last.timestamp
    if (a.tzinfo is None) != (b.tzinfo is None):  # compare naive with aware as UTC
        a, b = a.replace(tzinfo=None), b.replace(tzinfo=None)
    return a < b


def to_thread(thread: igt.DirectThread, my_user_id: str | None = None) -> DirectThread:
    last = thread.messages[0] if thread.messages else None
    from_me = _from_me(last, my_user_id) if last else None
    return DirectThread(
        id=str(thread.id),
        title=thread.thread_title or None,
        users=[to_user_summary(u) for u in thread.users],
        last_message=(last.text or (f"[{last.item_type}]" if last.item_type else None))
        if last
        else None,
        last_message_id=_s(last.id) if last else None,
        last_sender_id=_s(last.user_id) if last else None,
        last_from_me=from_me,
        last_activity_at=thread.last_activity_at,
        unread=_thread_unread(thread, last, from_me, my_user_id),
    )


def flatten_insights(data: Any, prefix: str = "") -> list[Insight]:
    """Flatten instagrapi's nested insights payload into numeric ``Insight`` rows.

    Names are dotted paths (``inline_insights_node.metrics.reach_count``); lists of
    dicts are kept whole as dict values since their keys are data (e.g. per-day rows).
    """
    out: list[Insight] = []
    if isinstance(data, dict):
        for key, value in data.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, int | float):
                out.append(Insight(name=name, value=value))
            elif isinstance(value, dict):
                out.extend(flatten_insights(value, name))
            elif isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
                out.append(Insight(name=name, value={"items": value}))
    return out


def _filter_insights(rows: list[Insight], metrics: list[str] | None) -> list[Insight]:
    if not metrics:
        return rows
    wanted = {m.lower() for m in metrics}
    return [
        r
        for r in rows
        if r.name.lower() in wanted
        or r.name.rsplit(".", 1)[-1].lower() in wanted
        or r.name.rsplit(".", 1)[-1].lower().removesuffix("_count") in wanted
    ]


# ------------------------------------------------------------------------ helpers
def prompt_challenge_code(username: str, choice: Any = None) -> str:
    """instagrapi ``challenge_code_handler``: ask for the emailed/SMS code on stdin."""
    if not sys.stdin or not sys.stdin.isatty():
        raise AuthError(
            f"Instagram sent a security code to {username} ({choice}); log in interactively "
            "to enter it."
        )
    while True:
        code = input(f"Security code sent to {username} via {choice}: ").strip()
        if code.isdigit():
            return code


def _refuse_password_change(username: str) -> str:
    raise AuthError(
        f"Instagram demands a password change for {username}; change it in the app, store the "
        "new password, and log in again."
    )


def parse_usertag(spec: str, index: int = 0) -> tuple[str, float, float]:
    """``"name"`` or ``"name:x,y"`` (0..1 coordinates) -> (username, x, y)."""
    name, _, pos = spec.lstrip("@").partition(":")
    if pos:
        x_s, _, y_s = pos.partition(",")
        x, y = float(x_s), float(y_s)
        if not (0 <= x <= 1 and 0 <= y <= 1):
            raise IgctlError(f"User tag position must be within 0..1: {spec!r}")
        return name, x, y
    offset = min(0.1 * index, 0.4)  # stagger untagged positions so tags don't overlap
    return name, 0.5 + offset, 0.5 + offset


#: Sticker sizes as fractions of the story frame (width, height).
STORY_MENTION_SIZE = (0.5, 0.08)
STORY_LOCATION_SIZE = (0.5, 0.1)


def story_sticker_position(spec: str, index: int = 0) -> tuple[str, float, float]:
    """``"name"`` or ``"name:x,y"`` -> (username, x, y) centre of a story mention sticker.

    Untagged positions stack vertically so several mentions don't overlap.
    """
    name, x, y = parse_usertag(spec, index)
    if ":" not in spec:
        x, y = 0.5, min(0.3 + 0.1 * index, 0.9)
    return name, x, y


_POST_SEGMENTS = frozenset({"p", "reel", "reels", "tv"})


def media_pk_from_path(parts: list[str]) -> str:
    """Media pk from URL path segments, e.g. ``["p", "CODE", "embed"]``.

    Accepts ``/p|reel|reels|tv/<code>/...`` (optionally after a ``/<username>/`` prefix)
    and ``/stories/<user>/<pk>/``; anything else is rejected rather than misdecoded.
    """
    if len(parts) >= 3 and parts[0] == "stories" and parts[2].isdigit():
        return parts[2]
    for i, seg in enumerate(parts[:2]):
        if seg in _POST_SEGMENTS and i + 1 < len(parts):
            return str(shortcode_to_pk(parts[i + 1]))
    raise ValueError(
        "Not an Instagram post, reel or story URL (expected /p/<code>/, /reel/<code>/ or "
        f"/stories/<user>/<id>/): /{'/'.join(parts)}"
    )


class PrivateBackend(Backend):
    name = "private"

    def __init__(self, account: Account, client: Client | None = None):
        self.account = account
        if client is None:
            client = Client(proxy=account.proxy, delay_range=list(DEFAULT_DELAY_RANGE))
        elif account.proxy:
            client.set_proxy(account.proxy)
        if not client.delay_range:
            client.delay_range = list(DEFAULT_DELAY_RANGE)
        client.challenge_code_handler = prompt_challenge_code
        client.change_password_handler = _refuse_password_change
        self.client = client
        self._authenticated = False
        self._user_ids: dict[str, str] = {}

    # ------------------------------------------------------------------ session
    @property
    def session_path(self) -> Path:
        return sessions_dir() / f"{self.account.name}.json"

    def save_session(self) -> None:
        self.client.dump_settings(self.session_path)

    def _persist_session(self) -> None:
        """Best-effort :meth:`save_session` after writes (never fails the action)."""
        if not self._authenticated:
            return
        try:
            self.save_session()
        except (OSError, TypeError, ValueError):
            pass

    def _load_session(self) -> bool:
        path = self.session_path
        if not path.exists():
            return False
        try:
            self.client.load_settings(path)
        except (OSError, json.JSONDecodeError, ValueError):
            return False
        return True

    @_wrap
    def login(
        self,
        username: str | None = None,
        password: str | None = None,
        verification_code: str | None = None,
    ) -> Profile:
        """Authenticate, reusing the saved session when it is still valid.

        With an explicit ``password`` (``igctl auth login``) the credentials are always
        checked by a real login, so a stale session of another account (or from before a
        password change) is never reused. Otherwise a valid saved session is reused and a
        password login (password from the keyring ``<account>:password``) is the fallback.
        ``verification_code`` answers a 2FA prompt. The saved device identity is kept.
        """
        explicit = password is not None
        username = username or self.account.username
        password = password or get_secret(self.account.name, "password")
        code = verification_code or ""

        loaded = self._load_session()
        self._authenticated = False
        if loaded and not explicit:
            try:
                self.client.get_timeline_feed()  # cheap probe that the session still works
                self._authenticated = True
            except (ig.LoginRequired, ig.ClientLoginRequired, ig.ClientUnauthorizedError):
                pass

        if not self._authenticated:
            if not username or not password:
                raise AuthError(
                    f"No valid session for account '{self.account.name}' and no stored "
                    "credentials; run `igctl auth login`."
                )
            # relogin=True clears only auth state and cookies of a loaded session and keeps
            # its device settings, so Instagram keeps seeing the same phone.
            self.client.login(username, password, relogin=loaded, verification_code=code)
            self._authenticated = True

        self.save_session()
        return self.me()

    def _ensure(self) -> Client:
        if not self._authenticated:
            if self.client.user_id:  # caller handed us an already-authenticated client
                self._authenticated = True
            else:
                self.login()
        return self.client

    @property
    def c(self) -> Client:
        """The authenticated instagrapi client (logs in lazily)."""
        return self._ensure()

    def _user_id(self, username: str | None) -> str:
        if username is None:
            return str(self.c.user_id)
        key = username.lstrip("@").lower()
        if key not in self._user_ids:
            self._user_ids[key] = str(self.c.user_info_by_username_v1(key).pk)
        return self._user_ids[key]

    def _media_id(self, ref: str) -> str:
        """Accept a full id, pk, shortcode or post URL; return something instagrapi takes."""
        ref = ref.strip()
        if ref.startswith(("http://", "https://")):
            return self._pk_from_url(ref)
        if "_" in ref and ref.split("_", 1)[0].isdigit():
            return ref
        if ref.isdigit():
            return ref
        if _SHORTCODE_RE.match(ref):
            return str(self.client.media_pk_from_code(ref))
        raise IgctlError(f"Not a media id, shortcode or URL: {ref!r}")

    def _pk(self, ref: str) -> str:
        return str(self.c.media_pk(self._media_id(ref)))

    # ------------------------------------------------------------------- reading
    @_wrap
    def me(self) -> Profile:
        c = self.c
        return to_profile(c.user_info_v1(str(c.user_id)))

    @_wrap
    def get_profile(self, username: str) -> Profile:
        profile = to_profile(self.c.user_info_by_username_v1(username.lstrip("@")))
        self._user_ids[profile.username.lower()] = profile.id
        return profile

    @_wrap
    def list_media(self, username: str | None = None, limit: int = 50) -> list[Media]:
        medias = self.c.user_medias_v1(self._user_id(username), amount=limit)
        if limit:
            medias = medias[:limit]
        return [to_media(m) for m in medias]

    @_wrap
    def get_media(self, media_id: str) -> Media:
        return to_media(self.c.media_info(self._pk(media_id)))

    @_wrap
    def list_comments(self, media_id: str, limit: int = 100) -> list[Comment]:
        mid = self._media_id(media_id)
        comments = self.c.media_comments(mid, amount=limit)
        return [to_comment(cm, mid) for cm in (comments[:limit] if limit else comments)]

    def _paginate(
        self,
        fetch: Callable[..., tuple[list[igt.UserShort], str]],
        username: str | None,
        limit: int,
    ) -> Iterator[UserSummary]:
        with _translated():
            user_id = self._user_id(username)
            cursor = ""
            seen: set[str] = set()
            yielded = 0
            while True:
                page = FOLLOW_PAGE_SIZE if not limit else min(FOLLOW_PAGE_SIZE, limit - yielded)
                users, cursor = fetch(user_id, max_amount=page, max_id=cursor)
                for user in users:
                    if user.pk in seen:
                        continue
                    seen.add(user.pk)
                    yield to_user_summary(user)
                    yielded += 1
                    if limit and yielded >= limit:
                        return
                if not cursor or not users:
                    return

    def followers(self, username: str | None = None, limit: int = 0) -> Iterator[UserSummary]:
        return self._paginate(self.client.user_followers_v1_chunk, username, limit)

    def following(self, username: str | None = None, limit: int = 0) -> Iterator[UserSummary]:
        return self._paginate(self.client.user_following_v1_chunk, username, limit)

    @_wrap
    def hashtag_media(self, tag: str, limit: int = 50, recent: bool = False) -> list[Media]:
        name = tag.lstrip("#")
        fetch = self.c.hashtag_medias_recent_v1 if recent else self.c.hashtag_medias_top_v1
        # Never pass amount=0: instagrapi would page through the whole hashtag.
        amount = min(limit, MAX_HASHTAG_SCAN) if limit > 0 else MAX_HASHTAG_SCAN
        return [to_media(m) for m in fetch(name, amount=amount)[:amount]]

    @_wrap
    def search_users(self, query: str, limit: int = 20) -> list[UserSummary]:
        count = min(limit, MAX_USER_SEARCH) if limit > 0 else MAX_USER_SEARCH
        return [to_user_summary(u) for u in self.c.search_users_v1(query, count)[:count]]

    # ------------------------------------------------------------------ insights
    @_wrap
    def account_insights(
        self, metrics: list[str], period: str = "day", days: int = 30
    ) -> list[Insight]:
        """Business/creator accounts only; ``period``/``days`` are fixed by the app API."""
        rows = flatten_insights(self.c.insights_account())
        return _filter_insights(rows, metrics)

    @_wrap
    def media_insights(self, media_id: str, metrics: list[str] | None = None) -> list[Insight]:
        rows = flatten_insights(self.c.insights_media(self._pk(media_id)))
        return _filter_insights(rows, metrics)

    # ---------------------------------------------------------------- publishing
    def _location(self, query: str | None) -> igt.Location | None:
        if not query:
            return None
        places = self.c.fbsearch_places(query)
        if not places:
            raise IgctlError(f"No Instagram location matches {query!r}.")
        return places[0]

    def _user_short(self, username: str) -> igt.UserShort:
        user = self.c.user_info_by_username_v1(username)
        return igt.UserShort(pk=str(user.pk), username=user.username)

    def _usertags(self, specs: list[str]) -> list[igt.Usertag]:
        tags = []
        for i, spec in enumerate(specs):
            name, x, y = parse_usertag(spec, i)
            tags.append(igt.Usertag(user=self._user_short(name), x=x, y=y))
        return tags

    def _local_paths(self, req: PublishRequest, workdir: Path) -> list[Path]:
        if req.paths:
            missing = [p for p in req.paths if not Path(p).is_file()]
            if missing:
                raise IgctlError(f"File(s) not found: {', '.join(map(str, missing))}")
            return [Path(p) for p in req.paths]
        if not req.urls:
            raise IgctlError("Nothing to publish: pass at least one file.")
        paths = []
        with httpx.Client(follow_redirects=True, timeout=120) as http:
            for i, url in enumerate(req.urls):
                resp = http.get(url)
                resp.raise_for_status()
                suffix = Path(urlparse(url).path).suffix or (
                    ".mp4" if "video" in resp.headers.get("content-type", "") else ".jpg"
                )
                dest = workdir / f"{i}{suffix}"
                dest.write_bytes(resp.content)
                paths.append(dest)
        return paths

    @_write
    def publish(self, req: PublishRequest) -> PublishResult:
        c = self.c
        with tempfile.TemporaryDirectory(prefix="igctl-") as tmp:
            paths = self._local_paths(req, Path(tmp))
            location = self._location(req.location)
            cover = Path(req.cover) if req.cover else None
            caption = req.caption
            match req.media_type:
                case MediaType.IMAGE:
                    media = c.photo_upload(
                        paths[0], caption, usertags=self._usertags(req.user_tags), location=location
                    )
                case MediaType.VIDEO:
                    media = c.video_upload(
                        paths[0],
                        caption,
                        thumbnail=cover,
                        usertags=self._usertags(req.user_tags),
                        location=location,
                    )
                case MediaType.REEL:
                    media = c.clip_upload(
                        paths[0],
                        caption,
                        thumbnail=cover,
                        usertags=self._usertags(req.user_tags),
                        location=location,
                        show_preview_in_feed=req.share_to_feed,
                    )
                case MediaType.CAROUSEL:
                    if len(paths) < 2:
                        raise IgctlError("A carousel needs at least 2 files.")
                    media = c.album_upload(
                        paths, caption, usertags=self._usertags(req.user_tags), location=location
                    )
                case MediaType.STORY:
                    return self._publish_story(req, paths, cover, location)
                case _:
                    raise IgctlError(f"Unsupported media type: {req.media_type}")
        result = to_media(media)
        return PublishResult(media_id=result.id, permalink=result.permalink, backend=self.name)

    def _publish_story(
        self,
        req: PublishRequest,
        paths: list[Path],
        cover: Path | None,
        location: igt.Location | None,
    ) -> PublishResult:
        """Each file becomes one story frame; the result refers to the last frame.

        Stickers need explicit geometry: instagrapi copies x/y/width/height verbatim, and
        nulls make Instagram drop the sticker or reject the upload. (instagrapi 3.0.21 keeps
        only the last mention in ``reel_mentions``; all of them still go out as tap models.)
        """
        mentions = []
        for i, spec in enumerate(req.user_tags):
            name, x, y = story_sticker_position(spec, i)
            mentions.append(
                igt.StoryMention(
                    user=self._user_short(name),
                    x=x,
                    y=y,
                    width=STORY_MENTION_SIZE[0],
                    height=STORY_MENTION_SIZE[1],
                )
            )
        locations = (
            [
                igt.StoryLocation(
                    location=location,
                    x=0.5,
                    y=0.8,
                    width=STORY_LOCATION_SIZE[0],
                    height=STORY_LOCATION_SIZE[1],
                )
            ]
            if location
            else []
        )
        story: igt.Story | None = None
        for path in paths:
            kwargs: dict[str, Any] = {
                "caption": req.caption,
                "mentions": mentions,
                "locations": locations,
            }
            if path.suffix.lower() in VIDEO_SUFFIXES:
                story = self.c.video_upload_to_story(path, thumbnail=cover, **kwargs)
            else:
                story = self.c.photo_upload_to_story(path, **kwargs)
        assert story is not None
        mapped = story_to_media(story)
        return PublishResult(media_id=mapped.id, permalink=mapped.permalink, backend=self.name)

    @_write
    def delete_media(self, media_id: str) -> None:
        if not self.c.media_delete(self._media_id(media_id)):
            raise IgctlError(f"Instagram refused to delete media {media_id}.")

    @_write
    def edit_caption(self, media_id: str, caption: str) -> None:
        self.c.media_edit(self._media_id(media_id), caption)

    # ---------------------------------------------------------------- engagement
    @_write
    def comment(self, media_id: str, text: str, reply_to: str | None = None) -> Comment:
        mid = self._media_id(media_id)
        result = self.c.media_comment(
            mid, text, replied_to_comment_id=int(reply_to) if reply_to else None
        )
        return to_comment(result, mid)

    @_write
    def delete_comment(self, media_id: str, comment_id: str) -> None:
        self.c.comment_bulk_delete(self._media_id(media_id), [int(comment_id)])

    @_write
    def like_comment(self, comment_id: str, unlike: bool = False) -> None:
        self.c.comment_like(int(comment_id), revert=unlike)

    @_write
    def like(self, media_id: str) -> None:
        self.c.media_like(self._media_id(media_id))

    @_write
    def unlike(self, media_id: str) -> None:
        self.c.media_unlike(self._media_id(media_id))

    @_write
    def follow(self, username: str) -> None:
        self.c.user_follow(self._user_id(username))

    @_write
    def unfollow(self, username: str) -> None:
        self.c.user_unfollow(self._user_id(username))

    @_wrap
    def media_likers(self, media_id: str, limit: int = 0) -> list[UserSummary]:
        likers = self.c.media_likers(self._media_id(media_id))
        return [to_user_summary(u) for u in (likers[:limit] if limit else likers)]

    # ------------------------------------------------------------------- stories
    @_wrap
    def user_stories(self, username: str | None = None) -> list[Media]:
        return [story_to_media(s) for s in self.c.user_stories_v1(self._user_id(username))]

    @_wrap
    def download_stories(self, username: str | None, dest: str) -> list[str]:
        folder = Path(dest)
        folder.mkdir(parents=True, exist_ok=True)
        stories = self.c.user_stories_v1(self._user_id(username))
        return [str(self.c.story_download(s.pk, folder=folder)) for s in stories]

    @_wrap
    def story_viewers(self, story_id: str, limit: int = 0) -> list[UserSummary]:
        pk = int(str(story_id).split("_", 1)[0])
        return [to_user_summary(v) for v in self.c.story_viewers(pk, amount=limit)]

    # ------------------------------------------------------------------- hashtags
    @_wrap
    def hashtag_info(self, tag: str) -> dict[str, Any]:
        info = self.c.hashtag_info_v1(tag.lstrip("#"))
        return {"id": info.id, "name": info.name, "media_count": info.media_count}

    @_wrap
    def media_pk_from_url(self, url: str) -> str:
        return self._pk_from_url(url)

    def _pk_from_url(self, url: str) -> str:
        """Post/reel/tv/story URL -> media pk. Only ``/share/`` links need a request."""
        parts = [p for p in urlparse(url).path.split("/") if p]
        if parts[:1] == ["share"]:
            # Short share links only redirect to the canonical URL: one public request.
            return str(self.client.media_pk_from_url(url))
        try:
            return media_pk_from_path(parts)
        except ValueError as e:
            raise IgctlError(str(e)) from e

    # -------------------------------------------------------------------- direct
    @_wrap
    def list_threads(self, limit: int = 20, unread_only: bool = False) -> list[DirectThread]:
        c = self.c
        threads = c.direct_threads(amount=limit, selected_filter="unread" if unread_only else None)
        me = _s(c.user_id)
        return [to_thread(t, me) for t in (threads[:limit] if limit else threads)]

    @_wrap
    def thread_messages(self, thread_id: str, limit: int = 20) -> list[DirectMessage]:
        msgs = self.c.direct_messages(int(thread_id), amount=limit)
        return [to_message(m, thread_id) for m in (msgs[:limit] if limit else msgs)]

    @_write
    def send_dm(self, usernames: list[str], text: str) -> DirectMessage:
        if not usernames:
            raise IgctlError("send_dm needs at least one recipient.")
        user_ids = [int(self._user_id(u)) for u in usernames]
        return to_message(self.c.direct_send(text, user_ids=user_ids))

    @_write
    def reply_dm(self, thread_id: str, text: str) -> DirectMessage:
        return to_message(self.c.direct_answer(int(thread_id), text), thread_id)

    @_write
    def mark_thread_seen(self, thread_id: str) -> None:
        self.c.direct_send_seen(int(thread_id))

    # ----------------------------------------------------------------- downloads
    @_wrap
    def download_media(self, media_id: str, dest: str) -> list[str]:
        folder = Path(dest)
        folder.mkdir(parents=True, exist_ok=True)
        c = self.c
        pk = self._pk(media_id)
        info = c.media_info(pk)
        match info.media_type, info.product_type:
            case 8, _:
                files = c.album_download(pk, folder=folder)
                return [str(p) for p in files]
            case 2, "clips":
                path = c.clip_download(pk, folder=folder)
            case 2, "igtv":
                path = c.igtv_download(pk, folder=folder)
            case 2, _:
                path = c.video_download(pk, folder=folder)
            case _:
                path = c.photo_download(pk, folder=folder)
        return [str(path)]


__all__ = [
    "PrivateBackend",
    "map_error",
    "to_profile",
    "to_media",
    "story_to_media",
    "to_comment",
    "to_user_summary",
    "to_thread",
    "to_message",
    "flatten_insights",
    "parse_usertag",
    "prompt_challenge_code",
    "permalink",
]
