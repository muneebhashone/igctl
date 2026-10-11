"""Built-in automation triggers."""

from __future__ import annotations

import random
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Literal

from pydantic import Field, field_validator, model_validator

from igctl.automation.registry import Handler, Params, RuleRun, register, render
from igctl.automation.state import last_action_at
from igctl.backends.base import NotSupported
from igctl.core.models import UserSummary
from igctl.core.storage import utcnow


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _user_text(u: UserSummary) -> str:
    return f"{u.username} {u.full_name or ''}".strip()


class _NoParams(Params):
    pass


# --- comment_autoreply -------------------------------------------------------


class CommentTrigger(Params):
    posts: int = Field(5, ge=1, le=100, description="how many of my recent posts to scan")
    comments_per_post: int = Field(50, ge=1, le=500)
    max_age_hours: float | None = Field(72, gt=0, description="ignore older comments")
    include_replies: bool = False


class CommentAction(Params):
    reply: str | None = Field(None, description="template: {username} {text} {permalink}")
    hide_blocklist: list[str] = Field(
        default_factory=list, description="hide comments containing any of these words"
    )

    @model_validator(mode="after")
    def _something(self) -> CommentAction:
        if not self.reply and not self.hide_blocklist:
            raise ValueError("set 'reply' and/or 'hide_blocklist'")
        return self


@register
class CommentAutoreply(Handler):
    name = "comment_autoreply"
    description = "Reply to new comments on my recent posts; optionally hide spam."
    Trigger = CommentTrigger
    Action = CommentAction
    prefer_backend = None  # works on the Graph API too

    def run(self, run: RuleRun) -> None:
        t: CommentTrigger = run.trigger
        a: CommentAction = run.action
        me = run.me.username.lower()
        oldest = utcnow() - timedelta(hours=t.max_age_hours) if t.max_age_hours else None
        blocklist = [w.lower() for w in a.hide_blocklist]

        for media in run.backend.list_media(None, limit=t.posts):
            for c in run.backend.list_comments(media.id, limit=t.comments_per_post):
                if run.budget == 0:
                    return
                run.stats.scanned += 1
                if c.username.lower() == me or (c.replied_to_id and not t.include_replies):
                    continue
                if oldest and c.created_at and _aware(c.created_at) < oldest:
                    continue
                if run.seen(c.id):
                    continue
                low = c.text.lower()
                if blocklist and any(w in low for w in blocklist):
                    run.stats.matched += 1
                    if run.act("hide_comment", c.id, partial(run.backend.hide_comment, c.id)):
                        run.mark(c.id)
                        run.stats.acted += 1
                    continue
                if not a.reply or not run.filter.matches(c.text):
                    continue
                run.stats.matched += 1
                text = render(a.reply, username=c.username, text=c.text, permalink=media.permalink)
                if run.act(
                    "comment", c.id, partial(run.backend.comment, media.id, text, reply_to=c.id)
                ):
                    run.mark(c.id)
                    run.stats.acted += 1


# --- dm_autoreply ------------------------------------------------------------


class DmTrigger(Params):
    threads: int = Field(20, ge=1, le=200)
    unread_only: bool = True
    once_per_thread: bool = Field(True, description="reply at most once per thread, ever")


class DmAction(Params):
    reply: str = Field(description="template: {username} {text}")
    mark_seen: bool = True


@register
class DmAutoreply(Handler):
    name = "dm_autoreply"
    description = "Reply to DM threads whose last message matches the filter."
    Trigger = DmTrigger
    Action = DmAction

    def run(self, run: RuleRun) -> None:
        t: DmTrigger = run.trigger
        a: DmAction = run.action
        for thread in run.backend.list_threads(limit=t.threads, unread_only=t.unread_only):
            if run.budget == 0:
                return
            run.stats.scanned += 1
            if not run.filter.matches(thread.last_message):
                continue
            stamp = thread.last_activity_at.isoformat() if thread.last_activity_at else ""
            key = thread.id if t.once_per_thread else f"{thread.id}@{stamp or thread.last_message}"
            if run.seen(key):
                continue
            run.stats.matched += 1
            username = thread.users[0].username if thread.users else ""
            text = render(a.reply, username=username, text=thread.last_message)
            if not run.act("dm", thread.id, partial(run.backend.reply_dm, thread.id, text)):
                continue
            run.mark(key)
            run.stats.acted += 1
            if a.mark_seen and not run.dry_run:
                try:
                    run.backend.mark_thread_seen(thread.id)
                except NotSupported:
                    pass
                except Exception as e:  # noqa: BLE001 - cosmetic, never fail the rule
                    run.stats.note(f"mark seen {thread.id}: {e}")


# --- welcome_dm --------------------------------------------------------------

FOLLOWER_SNAPSHOT = "automation:followers"


class WelcomeTrigger(Params):
    scan_limit: int = Field(1000, ge=0, description="newest followers to fetch; 0 = all")


class WelcomeAction(Params):
    message: str = Field(description="template: {username} {full_name}")


@register
class WelcomeDm(Handler):
    name = "welcome_dm"
    description = "DM new followers since the last run (first run only records a baseline)."
    Trigger = WelcomeTrigger
    Action = WelcomeAction
    default_max_per_run = 10

    def run(self, run: RuleRun) -> None:
        t: WelcomeTrigger = run.trigger
        a: WelcomeAction = run.action
        subject = f"{run.me.username}#{run.rule.name}"
        current = list(run.backend.followers(None, limit=t.scan_limit))
        current_ids = {u.id for u in current}
        snaps = run.store.snapshots(run.account, FOLLOWER_SNAPSHOT, subject, limit=1)

        def save(pending: Iterable[UserSummary]) -> None:
            if run.dry_run:
                return
            run.store.save_snapshot(
                run.account,
                FOLLOWER_SNAPSHOT,
                subject,
                {
                    "followers": [u.id for u in current],
                    "pending": [u.model_dump(mode="json") for u in pending],
                },
            )

        if not snaps:
            save([])
            run.stats.note(f"baseline recorded ({len(current)} followers); new ones get DMs")
            return

        prev = snaps[0]["data"]
        prev_ids = set(prev.get("followers", []))
        queue: dict[str, UserSummary] = {}
        for raw in prev.get("pending", []):
            u = UserSummary.model_validate(raw)
            if u.id in current_ids:  # still following us
                queue[u.id] = u
        for u in current:
            if u.id not in prev_ids:
                queue.setdefault(u.id, u)

        leftover: list[UserSummary] = []
        for u in queue.values():
            run.stats.scanned += 1
            if run.seen(u.id) or not run.filter.matches(_user_text(u)):
                continue
            run.stats.matched += 1
            if run.budget == 0:
                leftover.append(u)  # welcome on a later run
                continue
            text = render(a.message, username=u.username, full_name=u.full_name or u.username)
            if run.act("dm", u.username, partial(run.backend.send_dm, [u.username], text)):
                run.mark(u.id)
                run.stats.acted += 1
        if leftover:
            run.stats.note(f"{len(leftover)} new followers queued for the next run")
        save(leftover)


# --- hashtag_engage ----------------------------------------------------------

ENGAGED_NS = "engage:media"  # shared by all rules so a post is never engaged twice


class HashtagTrigger(Params):
    tags: list[str] = Field(min_length=1)
    recent: bool = Field(False, description="recent instead of top posts")
    per_tag: int = Field(30, ge=1, le=200)

    @field_validator("tags")
    @classmethod
    def _clean(cls, v: list[str]) -> list[str]:
        tags = [t.strip().lstrip("#") for t in v if t.strip().lstrip("#")]
        if not tags:
            raise ValueError("at least one non-empty tag required")
        return tags


class HashtagAction(Params):
    like: bool = True
    comments: list[str] = Field(default_factory=list, description="templates, one picked at random")
    comment_probability: float = Field(1.0, ge=0, le=1)

    @model_validator(mode="after")
    def _something(self) -> HashtagAction:
        if not self.like and not self.comments:
            raise ValueError("enable 'like' and/or provide 'comments'")
        return self


@register
class HashtagEngage(Handler):
    name = "hashtag_engage"
    description = "Like (and optionally comment on) top/recent posts of hashtags."
    Trigger = HashtagTrigger
    Action = HashtagAction
    default_max_per_run = 15

    def run(self, run: RuleRun) -> None:
        t: HashtagTrigger = run.trigger
        a: HashtagAction = run.action
        me = run.me.username.lower()
        done: set[str] = set()
        for tag in t.tags:
            for m in run.backend.hashtag_media(tag, limit=t.per_tag, recent=t.recent):
                if run.budget == 0:
                    return
                run.stats.scanned += 1
                if (m.owner_username or "").lower() == me or m.id in done:
                    continue
                if run.seen(m.id, ENGAGED_NS) or not run.filter.matches(m.caption):
                    continue
                run.stats.matched += 1
                done.add(m.id)
                did = a.like and run.act("like", m.id, partial(run.backend.like, m.id))
                if a.comments and random.random() < a.comment_probability:
                    text = render(
                        random.choice(a.comments),
                        username=m.owner_username,
                        text=m.caption,
                        tag=tag,
                    )
                    did = run.act("comment", m.id, partial(run.backend.comment, m.id, text)) or did
                if did:
                    run.mark(m.id, ENGAGED_NS)
                    run.stats.acted += 1


# --- unfollow_nonfollowers ---------------------------------------------------


class UnfollowTrigger(Params):
    whitelist: list[str] = Field(default_factory=list, description="never unfollow these")
    min_days_followed: float = Field(
        0, ge=0, description="only unfollow if we followed them at least this long ago"
    )
    only_followed_by_igctl: bool = Field(
        False, description="skip accounts igctl has no record of following"
    )
    scan_limit: int = Field(0, ge=0, description="how many of my followings to check; 0 = all")


@register
class UnfollowNonfollowers(Handler):
    name = "unfollow_nonfollowers"
    description = "Unfollow accounts that don't follow back (whitelist, min days followed)."
    Trigger = UnfollowTrigger
    Action = _NoParams

    def run(self, run: RuleRun) -> None:
        t: UnfollowTrigger = run.trigger
        followers = list(run.backend.followers(None, limit=0))
        follower_ids = {u.id for u in followers}
        follower_names = {u.username.lower() for u in followers}
        whitelist = {w.lower().lstrip("@") for w in t.whitelist}
        cutoff = utcnow() - timedelta(days=t.min_days_followed)

        for u in run.backend.following(None, limit=t.scan_limit):
            if run.budget == 0:
                return
            run.stats.scanned += 1
            name = u.username.lower()
            if u.id in follower_ids or name in follower_names or name in whitelist:
                continue
            if not run.filter.matches(_user_text(u)):
                continue
            followed_at = last_action_at(run.store, run.account, "follow", u.username)
            if followed_at is None:
                if t.only_followed_by_igctl:
                    continue
            elif t.min_days_followed and followed_at > cutoff:
                continue
            run.stats.matched += 1
            if run.act("unfollow", u.username, partial(run.backend.unfollow, u.username)):
                run.stats.acted += 1


# --- follow_from -------------------------------------------------------------


class FollowFromTrigger(Params):
    user: str | None = Field(None, description="source account username")
    post: str | None = Field(None, description="source media id")
    audience: Literal["followers", "following", "likers", "commenters"] = "followers"
    posts: int = Field(3, ge=1, le=50, description="recent posts of `user` for likers/commenters")
    scan_limit: int = Field(200, ge=1)
    skip_private: bool = False
    skip_verified: bool = False
    skip_following: bool = Field(True, description="skip accounts I already follow")

    @field_validator("user")
    @classmethod
    def _strip_at(cls, v: str | None) -> str | None:
        return v.strip().lstrip("@") if v else v

    @model_validator(mode="after")
    def _source(self) -> FollowFromTrigger:
        if bool(self.user) == bool(self.post):
            raise ValueError("set exactly one of 'user' or 'post'")
        if self.post and self.audience in ("followers", "following"):
            raise ValueError("a 'post' source supports audience 'likers' or 'commenters'")
        return self


@register
class FollowFrom(Handler):
    name = "follow_from"
    description = "Follow followers/following/likers/commenters of an account or post."
    Trigger = FollowFromTrigger
    Action = _NoParams

    def _candidates(self, run: RuleRun, t: FollowFromTrigger) -> Iterator[UserSummary]:
        b = run.backend
        if t.audience == "followers":
            yield from b.followers(t.user, limit=t.scan_limit)
            return
        if t.audience == "following":
            yield from b.following(t.user, limit=t.scan_limit)
            return
        media_ids = [t.post] if t.post else [m.id for m in b.list_media(t.user, limit=t.posts)]
        for mid in media_ids:
            if t.audience == "likers":
                yield from b.media_likers(mid, limit=t.scan_limit)
            else:
                for c in b.list_comments(mid, limit=t.scan_limit):
                    yield UserSummary(id=c.user_id or c.username, username=c.username)

    def run(self, run: RuleRun) -> None:
        t: FollowFromTrigger = run.trigger
        me = run.me.username.lower()
        following = (
            {u.username.lower() for u in run.backend.following(None, limit=0)}
            if t.skip_following
            else set()
        )
        considered: set[str] = set()
        for u in self._candidates(run, t):
            if run.budget == 0:
                return
            name = u.username.lower()
            if name in considered:
                continue
            considered.add(name)
            run.stats.scanned += 1
            if name in (me, (t.user or "").lower()) or name in following:
                continue
            if (t.skip_private and u.is_private) or (t.skip_verified and u.is_verified):
                continue
            if not run.filter.matches(_user_text(u)):
                continue
            # Never re-follow someone igctl followed before (they may have been unfollowed).
            if last_action_at(run.store, run.account, "follow", u.username) is not None:
                continue
            run.stats.matched += 1
            if run.act("follow", u.username, partial(run.backend.follow, u.username)):
                run.stats.acted += 1
