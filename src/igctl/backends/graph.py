"""Backend over the official Instagram Graph API (Business/Creator accounts).

Works with both token flavours:

* ``graph.facebook.com``  - Instagram API with Facebook Login (Page-linked IG account).
* ``graph.instagram.com`` - Instagram API with Instagram Login.

Publishing is the documented two-step flow: create a media container from public URLs
(``POST /{ig-user-id}/media``), wait for video containers to reach ``FINISHED``, then
``POST /{ig-user-id}/media_publish``. Local files are made public through
:mod:`igctl.core.media_host`, or - for videos on the Facebook host when no media host is
configured - pushed directly with the resumable upload protocol (``rupload.facebook.com``).
"""

from __future__ import annotations

import json
import re
import tempfile
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from igctl.backends.base import (
    AuthError,
    Backend,
    IgctlError,
    NotSupported,
    RateLimited,
    TransientError,
)
from igctl.core.config import Account, Config, get_secret, secret_env_var
from igctl.core.media_host import (
    IMAGE_SUFFIXES,
    JPEG_SUFFIXES,
    VIDEO_SUFFIXES,
    graph_direct_upload,
    is_video,
    to_jpeg,
    upload_file,
    url_suffix,
)
from igctl.core.models import (
    Comment,
    Insight,
    Media,
    MediaType,
    Profile,
    PublishRequest,
    PublishResult,
)

AUTH_CODES = frozenset({102, 190})
RATE_LIMIT_CODES = frozenset({4, 17, 32, 613, 80002})
PUBLISH_LIMIT_SUBCODE = 2207042  # "reached the maximum number of posts allowed"
TRANSIENT_CODES = frozenset({1, 2})

USER_FIELDS = (
    "id,username,name,biography,followers_count,follows_count,media_count,"
    "profile_picture_url,website"
)
BASE_MEDIA_FIELDS = (
    "id,caption,media_type,media_url,permalink,thumbnail_url,timestamp,username,"
    "like_count,comments_count"
)
COMMENT_FIELDS = "id,text,username,timestamp,like_count,from"
HASHTAG_MEDIA_FIELDS = (
    "id,caption,media_type,comments_count,like_count,permalink,timestamp,media_url"
)

MAX_CAROUSEL_ITEMS = 10
MAX_INSIGHT_WINDOW = timedelta(days=30)
# Account metrics that only exist as time series (no total_value form). follower_count is a
# daily series; online_followers only accepts period=lifetime (hourly values per day).
TIME_SERIES_METRICS = frozenset({"follower_count", "online_followers"})
LIFETIME_SERIES_METRICS = frozenset({"online_followers"})
# Since v20 the interaction metrics (total_value) and follower_count only accept period=day;
# the since/until window sets the range.
ACCOUNT_PERIOD = "day"
DEMOGRAPHIC_BREAKDOWNS = ("age", "gender", "country", "city")

FEED_METRICS = ["reach", "views", "likes", "comments", "shares", "saved", "total_interactions"]
REEL_METRICS = [*FEED_METRICS, "ig_reels_avg_watch_time", "ig_reels_video_view_total_time"]
STORY_METRICS = ["reach", "views", "replies", "shares", "total_interactions"]
# Valid for every media product type (feed, reel, story); the fallback on the Instagram-Login
# host, where media_product_type is unavailable and stories/reels can't be told apart.
COMMON_METRICS = ["reach", "views", "shares", "total_interactions"]

_HASHTAG_RE = re.compile(r"#(\w+)", re.UNICODE)


class GraphBackend(Backend):
    name = "graph"

    def __init__(
        self,
        account: Account,
        client: httpx.Client | None = None,
        config: Config | None = None,
        *,
        poll_interval: float = 3.0,
        poll_timeout: float = 600.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.account = account
        self.base_url = f"https://{account.graph_host}/{account.graph_version}"
        self._client = client or httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0))
        self._config = config
        self._token: str | None = None
        self._ig_user_id = account.ig_user_id
        self._username = account.username
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        self._sleep = sleep

    # --- plumbing ------------------------------------------------------------
    @property
    def config(self) -> Config:
        if self._config is None:
            self._config = Config.load()
        return self._config

    @property
    def token(self) -> str:
        if self._token is None:
            token = get_secret(self.account.name, "token")
            if not token:
                raise AuthError(
                    f"No Graph API token for account '{self.account.name}'. Run "
                    f"`igctl auth login` or set {secret_env_var(self.account.name, 'token')}."
                )
            self._token = token
        return self._token

    @property
    def is_facebook_host(self) -> bool:
        return self.account.graph_host.endswith("facebook.com")

    @property
    def ig_user_id(self) -> str:
        if self._ig_user_id is None:
            self._ig_user_id = self._resolve_ig_user_id()
        return self._ig_user_id

    def _resolve_ig_user_id(self) -> str:
        if not self.is_facebook_host:
            data = self._get("me", {"fields": "user_id,username"})
            self._username = self._username or data.get("username")
            return str(data.get("user_id") or data["id"])
        # Facebook Login: the IG account hangs off one of the user's Pages (any page of them).
        pages = self._paginate(
            "me/accounts", {"fields": "name,instagram_business_account{id,username}"}, 0
        )
        found = [
            p["instagram_business_account"] for p in pages if p.get("instagram_business_account")
        ]
        wanted = (self.account.username or "").lower()
        if wanted:
            found = [ig for ig in found if ig.get("username", "").lower() == wanted]
        if len({str(ig["id"]) for ig in found}) > 1:
            names = ", ".join(sorted(f"@{ig.get('username', ig['id'])}" for ig in found))
            raise IgctlError(
                f"This token manages several Instagram accounts ({names}). Set `username` or "
                "`ig_user_id` for the account in config.yaml (or re-run `igctl auth login` "
                "with --ig-user-id)."
            )
        if found:
            self._username = self._username or found[0].get("username")
            return str(found[0]["id"])
        raise IgctlError(
            "Could not find an Instagram Business/Creator account linked to this token's "
            "Facebook Pages. Set `ig_user_id` for the account in config.yaml."
        )

    def _url(self, path: str) -> str:
        return path if path.startswith("https://") else f"{self.base_url}/{path.lstrip('/')}"

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        query = dict(params or {})
        if auth:
            query["access_token"] = self.token
        for attempt in range(3):
            try:
                resp = self._client.request(
                    method,
                    self._url(path),
                    params=query or None,  # an empty dict would wipe a URL query
                    data=data,
                    headers=headers,
                    content=content,
                )
            except httpx.TransportError as exc:
                if attempt < 2:
                    self._sleep(2**attempt)
                    continue
                raise TransientError(
                    f"Network error talking to {self.account.graph_host}: {exc}"
                ) from exc
            try:
                body = resp.json()
            except ValueError:
                body = None
            if resp.is_success and isinstance(body, dict) and "error" not in body:
                return body
            if isinstance(body, dict) and isinstance(body.get("error"), dict):
                err = body["error"]
                if attempt < 2 and (err.get("is_transient") or err.get("code") in TRANSIENT_CODES):
                    self._sleep(2**attempt)
                    continue
                raise _map_error(err, resp.status_code)
            if resp.status_code >= 500 and attempt < 2:
                self._sleep(2**attempt)
                continue
            if resp.status_code == 429:
                raise RateLimited("Instagram rate limit hit (HTTP 429). Try again later.")
            if resp.status_code >= 500:
                raise TransientError(f"Graph API HTTP {resp.status_code}: {resp.text[:300]}")
            raise IgctlError(f"Graph API HTTP {resp.status_code}: {resp.text[:300]}")
        raise AssertionError("unreachable")

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("GET", path, params)

    def _post(self, path: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("POST", path, data=data)

    def _paginate(
        self, path: str, params: dict[str, Any], limit: int, page_size: int = 50
    ) -> Iterator[dict[str, Any]]:
        """Yield items across cursor pages; ``limit <= 0`` means everything."""
        params = {**params, "limit": min(page_size, limit) if limit > 0 else page_size}
        count = 0
        url: str | None = path
        while url:
            # `paging.next` already carries every query param, including the token.
            body = self._get(url, params) if params else self._request("GET", url, auth=False)
            for item in body.get("data", []):
                yield item
                count += 1
                if 0 < limit <= count:
                    return
            url, params = body.get("paging", {}).get("next"), {}

    # --- session -------------------------------------------------------------
    def me(self) -> Profile:
        data = self._get(self.ig_user_id, {"fields": USER_FIELDS})
        self._username = data.get("username") or self._username
        profile = _profile(data)
        profile.is_business = True
        return profile

    def _my_username(self) -> str:
        if not self._username:
            self.me()
        return self._username or ""

    def _is_me(self, username: str | None) -> bool:
        if username is None:
            return True
        name = username.lstrip("@").lower()
        known = (self._username or self.account.username or "").lower()
        return bool(known) and name == known

    # --- publishing ----------------------------------------------------------
    def publishing_limit(self) -> dict[str, Any]:
        """Current quota usage, e.g. ``{"quota_usage": 3, "config": {"quota_total": 100, ...}}``."""
        body = self._get(
            f"{self.ig_user_id}/content_publishing_limit", {"fields": "quota_usage,config"}
        )
        data = body.get("data") or [{}]
        return data[0]

    def publish(self, req: PublishRequest) -> PublishResult:
        with tempfile.TemporaryDirectory(prefix="igctl-jpeg-") as tmp:
            sources = self._sources(req, Path(tmp))
            cover_url = None
            if req.cover and req.media_type is not MediaType.CAROUSEL:
                cover_url = upload_file(to_jpeg(req.cover, Path(tmp)), self.config)
        if not sources:
            raise IgctlError("Nothing to publish: no file paths or URLs given.")

        common = self._common_params(req)
        if req.media_type is MediaType.CAROUSEL:
            if not 2 <= len(sources) <= MAX_CAROUSEL_ITEMS:
                raise IgctlError(f"A carousel needs 2-{MAX_CAROUSEL_ITEMS} items.")
            children = [
                self._create_container(src, {"is_carousel_item": "true"}, video=video)
                for src, video in sources
            ]
            # Every child (images too) must be FINISHED before the parent can use it.
            for child in children:
                self._wait_finished(child)
            parent = self._post(
                f"{self.ig_user_id}/media",
                {"media_type": "CAROUSEL", "children": ",".join(children), **common},
            )["id"]
            self._wait_finished(parent)
            container = parent
        else:
            if len(sources) != 1:
                raise IgctlError(
                    f"A {req.media_type.value} takes exactly one file; use carousel for several."
                )
            src, src_video = sources[0]
            params = dict(common)
            video = src_video or req.media_type in (MediaType.REEL, MediaType.VIDEO)
            if req.media_type is MediaType.STORY:
                params.pop("caption", None)
                params.pop("location_id", None)
                params["media_type"] = "STORIES"
            elif video:
                # Single-video feed posts are published as Reels (VIDEO is retired).
                params["media_type"] = "REELS"
                params["share_to_feed"] = str(req.share_to_feed).lower()
                if cover_url:
                    params["cover_url"] = cover_url
            elif req.media_type is not MediaType.IMAGE:
                raise IgctlError(f"{src} does not look like a video for a {req.media_type.value}.")
            if req.media_type is not MediaType.STORY and (tags := self._user_tags(req, video)):
                params["user_tags"] = tags
            container = self._create_container(src, params, video=video)
            # Image containers can still be IN_PROGRESS (slow fetch from the media host);
            # publishing them then fails with "Media ID is not available".
            self._wait_finished(container)

        media_id = self._post(f"{self.ig_user_id}/media_publish", {"creation_id": container})["id"]
        permalink: str | None = None
        try:
            permalink = self._get(media_id, {"fields": "permalink"}).get("permalink")
        except IgctlError:
            pass  # published fine; the permalink is a nicety
        return PublishResult(media_id=str(media_id), permalink=permalink, backend=self.name)

    def _sources(self, req: PublishRequest, workdir: Path) -> list[tuple[str | Path, bool]]:
        """``(url or local path, is_video)`` per item, uploading local files if needed.

        Video-ness comes from the local file when there is one: hosted URLs (``cmd:`` hosts,
        CDNs) often have no extension. Local images are converted to JPEG first, the only
        image format the Graph API accepts.
        """
        if req.urls:
            probe = req.media_type in (MediaType.CAROUSEL, MediaType.STORY)
            return [(url, self._url_is_video(url, probe)) for url in req.urls]
        if self._use_resumable(req):
            return [(p, True) for p in req.paths]
        kinds = [is_video(p) for p in req.paths]
        hosted = [
            upload_file(p if video else to_jpeg(p, workdir), self.config)
            for p, video in zip(req.paths, kinds, strict=True)
        ]
        return list(zip(hosted, kinds, strict=True))

    def _url_is_video(self, url: str, probe: bool) -> bool:
        """Classify a public URL; rejects non-JPEG images, which the Graph API refuses."""
        suffix = url_suffix(url)
        if suffix in VIDEO_SUFFIXES:
            return True
        if suffix in IMAGE_SUFFIXES and suffix not in JPEG_SUFFIXES:
            raise IgctlError(
                f"{url}: the Graph API only accepts JPEG images, not '{suffix}'. Convert it, "
                "or pass the local file so igctl converts and hosts it."
            )
        if suffix or not probe:
            return False
        # Extension-less URL in a carousel/story: ask the server what it is.
        try:
            resp = self._client.head(url, follow_redirects=True)
        except httpx.HTTPError:
            return False
        ctype = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if ctype.startswith("image/") and ctype not in ("image/jpeg", "image/jpg"):
            raise IgctlError(f"{url} is {ctype}; the Graph API only accepts JPEG images.")
        return ctype.startswith("video/")

    def _use_resumable(self, req: PublishRequest) -> bool:
        return graph_direct_upload(req, self.account.graph_host, self.config)

    def _common_params(self, req: PublishRequest) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if req.caption:
            params["caption"] = req.caption
        if req.location:
            if not req.location.isdigit():
                raise IgctlError(
                    "The Graph API needs a numeric location id (a Facebook Page id with a "
                    f"location), got '{req.location}'."
                )
            params["location_id"] = req.location
        return params

    def _user_tags(self, req: PublishRequest, video: bool) -> str | None:
        if not req.user_tags or req.media_type is MediaType.CAROUSEL:
            return None
        tags = [
            {"username": u.lstrip("@")}
            if video
            else {"username": u.lstrip("@"), "x": 0.5, "y": 0.5}
            for u in req.user_tags
        ]
        return json.dumps(tags)

    def _create_container(self, src: str | Path, params: dict[str, Any], *, video: bool) -> str:
        params = dict(params)
        if isinstance(src, Path):
            params["upload_type"] = "resumable"
            params.setdefault("media_type", "VIDEO")
            container = self._post(f"{self.ig_user_id}/media", params)["id"]
            self._rupload(container, src)
            return str(container)
        params["video_url" if video else "image_url"] = src
        if video and params.get("is_carousel_item"):
            params["media_type"] = "VIDEO"
        return str(self._post(f"{self.ig_user_id}/media", params)["id"])

    def _rupload(self, container_id: str, path: Path) -> None:
        payload = path.read_bytes()
        self._request(
            "POST",
            f"https://rupload.facebook.com/ig-api-upload/{self.account.graph_version}/{container_id}",
            headers={
                "Authorization": f"OAuth {self.token}",
                "offset": "0",
                "file_size": str(len(payload)),
            },
            content=payload,
            auth=False,
        )

    def _wait_finished(self, container_id: str) -> None:
        deadline = time.monotonic() + self.poll_timeout
        interval = self.poll_interval
        while True:
            data = self._get(container_id, {"fields": "status_code,status"})
            code = data.get("status_code")
            if code in ("FINISHED", "PUBLISHED"):
                return
            if code in ("ERROR", "EXPIRED"):
                raise IgctlError(
                    f"Media container {container_id} {code.lower()}: {data.get('status', '')}"
                )
            if time.monotonic() + interval > deadline:
                raise IgctlError(
                    f"Timed out after {self.poll_timeout:.0f}s waiting for container "
                    f"{container_id} to finish processing (last status: {code})."
                )
            self._sleep(interval)
            interval = min(interval * 1.5, 30.0)

    def delete_media(self, media_id: str) -> None:
        if not self.is_facebook_host:
            raise NotSupported(self.name, "delete_media (needs a Facebook Login token)")
        self._request("DELETE", media_id)

    def set_comments_enabled(self, media_id: str, enabled: bool) -> None:
        self._post(media_id, {"comment_enabled": str(enabled).lower()})

    # edit_caption: the Graph API cannot edit captions -> base NotSupported.

    # --- reading -------------------------------------------------------------
    def _media_fields(self) -> str:
        extra = ",media_product_type" if self.is_facebook_host else ""
        return f"{BASE_MEDIA_FIELDS},shortcode{extra}"

    def get_profile(self, username: str) -> Profile:
        if self._is_me(username):
            return self.me()
        data = self._business_discovery(username, USER_FIELDS)
        profile = _profile(data)
        profile.is_business = True
        profile.is_private = False
        return profile

    def _require_facebook_host(self, feature: str) -> None:
        if not self.is_facebook_host:
            raise NotSupported(self.name, f"{feature} (needs a Facebook Login token)")

    def supports(self, op: str) -> bool:
        if op in ("delete_media", "hashtag_media") and not self.is_facebook_host:
            return False
        return super().supports(op)

    def _business_discovery(self, username: str, fields: str) -> dict[str, Any]:
        self._require_facebook_host("business discovery")
        name = username.lstrip("@")
        body = self._get(
            self.ig_user_id, {"fields": f"business_discovery.username({name}){{{fields}}}"}
        )
        bd = body.get("business_discovery")
        if not bd:
            raise IgctlError(f"@{name} not found via Business Discovery.")
        return bd

    def list_media(self, username: str | None = None, limit: int = 50) -> list[Media]:
        if self._is_me(username):
            items = self._paginate(
                f"{self.ig_user_id}/media", {"fields": self._media_fields()}, limit
            )
            return [_media(m) for m in items]
        return self._discovery_media(username or "", limit)

    def _discovery_media(self, username: str, limit: int) -> list[Media]:
        out: list[Media] = []
        after: str | None = None
        while limit <= 0 or len(out) < limit:
            page = min(50, limit - len(out)) if limit > 0 else 50
            edge = f"media.limit({page})" + (f".after({after})" if after else "")
            bd = self._business_discovery(username, f"username,{edge}{{{BASE_MEDIA_FIELDS}}}")
            media = bd.get("media", {})
            out.extend(_media(m, owner=bd.get("username")) for m in media.get("data", []))
            after = media.get("paging", {}).get("cursors", {}).get("after")
            if not media.get("data") or not media.get("paging", {}).get("next") or not after:
                break
        return out[:limit] if limit > 0 else out

    def get_media(self, media_id: str) -> Media:
        return _media(self._get(media_id, {"fields": self._media_fields()}))

    def list_comments(self, media_id: str, limit: int = 100) -> list[Comment]:
        fields = f"{COMMENT_FIELDS},replies{{{COMMENT_FIELDS}}}"
        out: list[Comment] = []
        for raw in self._paginate(f"{media_id}/comments", {"fields": fields}, limit):
            out.append(_comment(raw, media_id))
            for reply in raw.get("replies", {}).get("data", []):
                out.append(_comment(reply, media_id, replied_to=raw["id"]))
        return out

    def hashtag_media(self, tag: str, limit: int = 50, recent: bool = False) -> list[Media]:
        self._require_facebook_host("hashtag search")
        tag = tag.lstrip("#")
        found = self._get("ig_hashtag_search", {"user_id": self.ig_user_id, "q": tag})
        if not found.get("data"):
            raise IgctlError(f"Hashtag #{tag} not found.")
        edge = "recent_media" if recent else "top_media"
        items = self._paginate(
            f"{found['data'][0]['id']}/{edge}",
            {"user_id": self.ig_user_id, "fields": HASHTAG_MEDIA_FIELDS},
            limit,
        )
        return [_media(m) for m in items]

    # --- insights ------------------------------------------------------------
    def account_insights(
        self, metrics: list[str], period: str = "day", days: int = 30
    ) -> list[Insight]:
        """Account metrics over the last ``days``.

        ``period`` is kept for interface compatibility and ignored: the Graph API takes
        ``period=day`` for the interaction metrics and follower_count, and ``lifetime`` for
        online_followers and demographics; the range comes from since/until windows.
        """
        out: list[Insight] = []
        until = datetime.now(UTC).replace(microsecond=0)
        start = until - timedelta(days=max(days, 1))
        lifetime = [m for m in metrics if m in LIFETIME_SERIES_METRICS]
        series = [m for m in metrics if m in TIME_SERIES_METRICS and m not in lifetime]
        demographic = [m for m in metrics if m.endswith("_demographics")]
        totals = [m for m in metrics if m not in TIME_SERIES_METRICS and m not in demographic]
        path = f"{self.ig_user_id}/insights"

        for since, end in _windows(start, until):
            window = {"since": int(since.timestamp()), "until": int(end.timestamp())}
            # follower_count/online_followers only cover the last 30 days.
            recent = until - since <= MAX_INSIGHT_WINDOW
            # Separate requests: one invalid metric/period combination fails the whole call.
            if series and recent:
                body = self._get(
                    path, {"metric": ",".join(series), "period": ACCOUNT_PERIOD, **window}
                )
                out.extend(_parse_insights(body))
            if lifetime and recent:
                body = self._get(
                    path, {"metric": ",".join(lifetime), "period": "lifetime", **window}
                )
                out.extend(_parse_insights(body))
            if totals:
                body = self._get(
                    path,
                    {
                        "metric": ",".join(totals),
                        "period": ACCOUNT_PERIOD,
                        "metric_type": "total_value",
                        **window,
                    },
                )
                out.extend(_parse_insights(body, end_time=end))
        for metric in demographic:
            for breakdown in DEMOGRAPHIC_BREAKDOWNS:
                body = self._get(
                    path,
                    {
                        "metric": metric,
                        "period": "lifetime",
                        "metric_type": "total_value",
                        "timeframe": "this_month",
                        "breakdown": breakdown,
                    },
                )
                for ins in _parse_insights(body):
                    ins.name = f"{metric}:{breakdown}"
                    out.append(ins)
        return out

    def media_insights(self, media_id: str, metrics: list[str] | None = None) -> list[Insight]:
        if metrics:
            return self._media_insights(media_id, metrics)
        kind = self.get_media(media_id).media_type
        if self.is_facebook_host:
            defaults = {MediaType.REEL: REEL_METRICS, MediaType.STORY: STORY_METRICS}.get(
                kind, FEED_METRICS
            )
            return self._media_insights(media_id, defaults)
        # Instagram-Login host: media_product_type isn't available, so a story looks like an
        # IMAGE/VIDEO and a reel like a VIDEO. Try the likely set, then the always-valid one.
        guess = REEL_METRICS if kind is MediaType.VIDEO else FEED_METRICS
        try:
            return self._media_insights(media_id, guess)
        except (AuthError, RateLimited, TransientError):
            raise
        except IgctlError:
            return self._media_insights(media_id, COMMON_METRICS)

    def _media_insights(self, media_id: str, metrics: list[str]) -> list[Insight]:
        body = self._get(f"{media_id}/insights", {"metric": ",".join(metrics)})
        return _parse_insights(body)

    # --- engagement ----------------------------------------------------------
    def comment(self, media_id: str, text: str, reply_to: str | None = None) -> Comment:
        if reply_to:
            data = self._post(f"{reply_to}/replies", {"message": text})
        else:
            data = self._post(f"{media_id}/comments", {"message": text})
        return Comment(
            id=str(data["id"]),
            media_id=media_id,
            text=text,
            username=self._my_username(),
            user_id=self._ig_user_id,
            created_at=datetime.now(UTC),
            replied_to_id=reply_to,
        )

    def delete_comment(self, media_id: str, comment_id: str) -> None:
        self._request("DELETE", comment_id)

    def hide_comment(self, comment_id: str, hide: bool = True) -> None:
        self._post(comment_id, {"hide": str(hide).lower()})


# --- helpers -------------------------------------------------------------------


def _map_error(err: dict[str, Any], status: int) -> IgctlError:
    code = err.get("code")
    subcode = err.get("error_subcode")
    message = err.get("error_user_msg") or err.get("message") or "Unknown Graph API error"
    detail = f"{message} (code {code}" + (f", subcode {subcode}" if subcode else "") + ")"
    if code in AUTH_CODES:
        return AuthError(f"Graph API token rejected: {detail}. Re-run `igctl auth login`.")
    if code in RATE_LIMIT_CODES or subcode == PUBLISH_LIMIT_SUBCODE or status == 429:
        return RateLimited(f"Graph API rate limit: {detail}")
    return IgctlError(f"Graph API error: {detail}")


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None


def _profile(d: dict[str, Any]) -> Profile:
    return Profile(
        id=str(d.get("id", "")),
        username=d.get("username", ""),
        full_name=d.get("name"),
        biography=d.get("biography"),
        followers_count=d.get("followers_count"),
        following_count=d.get("follows_count"),
        media_count=d.get("media_count"),
        external_url=d.get("website"),
        profile_pic_url=d.get("profile_picture_url"),
    )


def _media_type(d: dict[str, Any]) -> MediaType:
    product = d.get("media_product_type")
    if product == "REELS":
        return MediaType.REEL
    if product == "STORY":
        return MediaType.STORY
    return {
        "CAROUSEL_ALBUM": MediaType.CAROUSEL,
        "VIDEO": MediaType.VIDEO,
    }.get(d.get("media_type", ""), MediaType.IMAGE)


def _media(d: dict[str, Any], owner: str | None = None) -> Media:
    caption = d.get("caption")
    return Media(
        id=str(d["id"]),
        code=d.get("shortcode") or _shortcode(d.get("permalink")),
        media_type=_media_type(d),
        caption=caption,
        taken_at=_parse_time(d.get("timestamp")),
        like_count=d.get("like_count"),
        comment_count=d.get("comments_count"),
        view_count=d.get("view_count"),
        permalink=d.get("permalink"),
        media_url=d.get("media_url"),
        thumbnail_url=d.get("thumbnail_url"),
        owner_username=d.get("username") or owner,
        hashtags=[t.lower() for t in _HASHTAG_RE.findall(caption or "")],
    )


def _shortcode(permalink: str | None) -> str | None:
    if not permalink:
        return None
    m = re.search(r"instagram\.com/(?:p|reel|tv)/([^/?#]+)", permalink)
    return m.group(1) if m else None


def _comment(d: dict[str, Any], media_id: str, replied_to: str | None = None) -> Comment:
    sender = d.get("from") or {}
    return Comment(
        id=str(d["id"]),
        media_id=media_id,
        text=d.get("text", ""),
        username=d.get("username") or sender.get("username", ""),
        user_id=sender.get("id"),
        created_at=_parse_time(d.get("timestamp")),
        like_count=d.get("like_count"),
        replied_to_id=replied_to,
    )


def _windows(start: datetime, end: datetime) -> Iterator[tuple[datetime, datetime]]:
    """Split [start, end] into chunks the insights API accepts (max 30 days each)."""
    cursor = start
    while cursor < end:
        nxt = min(cursor + MAX_INSIGHT_WINDOW, end)
        yield cursor, nxt
        cursor = nxt


def _parse_insights(body: dict[str, Any], end_time: datetime | None = None) -> list[Insight]:
    out: list[Insight] = []
    for item in body.get("data", []):
        name, period = item.get("name", ""), item.get("period")
        total = item.get("total_value")
        if total is not None:
            value: Any = total.get("value")
            breakdowns = total.get("breakdowns") or []
            if breakdowns:
                value = {
                    "|".join(r.get("dimension_values", [])): r.get("value")
                    for b in breakdowns
                    for r in b.get("results", [])
                }
            out.append(Insight(name=name, value=value, period=period, end_time=end_time))
            continue
        for v in item.get("values", []):
            out.append(
                Insight(
                    name=name,
                    value=v.get("value"),
                    period=period,
                    end_time=_parse_time(v.get("end_time")),
                )
            )
    return out
