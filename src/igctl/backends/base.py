"""The Backend contract every Instagram transport implements.

Two implementations exist:

* ``graph``   - the official Instagram Graph API (Business/Creator accounts). Safe, but
  limited to publishing, insights, comments, and Business Discovery.
* ``private`` - instagrapi (the private mobile API). Can do nearly everything the app
  can, at the cost of violating Instagram's Terms and risking account restrictions.

A backend raises :class:`NotSupported` for operations its transport cannot perform, so
the CLI can fail with a clear message (or fall back to another backend).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from igctl.core.models import (
    Comment,
    DirectMessage,
    DirectThread,
    Insight,
    Media,
    Profile,
    PublishRequest,
    PublishResult,
    UserSummary,
)


class IgctlError(Exception):
    """Base error for user-facing failures."""


class NotSupported(IgctlError):
    def __init__(self, backend: str, operation: str):
        super().__init__(f"The '{backend}' backend does not support '{operation}'.")
        self.backend = backend
        self.operation = operation


class AuthError(IgctlError):
    pass


class RateLimited(IgctlError):
    pass


class TransientError(IgctlError):
    """A failure worth retrying soon: network errors, timeouts, temporary 5xx responses."""


class ActionBlocked(IgctlError):
    """Instagram blocked an action (feedback_required, sentry block).

    Not transient: retrying within minutes during an active block is what escalates it to a
    checkpoint or suspension. Automation must stop for the account (24h+).
    """


class Backend(ABC):
    name: str

    def supports(self, op: str) -> bool:
        """Whether this backend implements ``op`` (a :class:`Backend` method name).

        Lets ``--dry-run`` report unsupported actions instead of pretending they worked.
        """
        impl = getattr(type(self), op, None)
        return impl is not None and impl is not getattr(Backend, op, None)

    # --- session -------------------------------------------------------------
    @abstractmethod
    def me(self) -> Profile: ...

    # --- publishing ----------------------------------------------------------
    @abstractmethod
    def publish(self, req: PublishRequest) -> PublishResult: ...

    def delete_media(self, media_id: str) -> None:
        raise NotSupported(self.name, "delete_media")

    def edit_caption(self, media_id: str, caption: str) -> None:
        raise NotSupported(self.name, "edit_caption")

    # --- reading -------------------------------------------------------------
    @abstractmethod
    def get_profile(self, username: str) -> Profile: ...

    @abstractmethod
    def list_media(self, username: str | None = None, limit: int = 50) -> list[Media]:
        """Recent media for ``username`` (None means the logged-in account)."""

    def get_media(self, media_id: str) -> Media:
        raise NotSupported(self.name, "get_media")

    def list_comments(self, media_id: str, limit: int = 100) -> list[Comment]:
        raise NotSupported(self.name, "list_comments")

    def followers(self, username: str | None = None, limit: int = 0) -> Iterator[UserSummary]:
        """Followers of ``username`` (None = me). ``limit=0`` means all."""
        raise NotSupported(self.name, "followers")

    def following(self, username: str | None = None, limit: int = 0) -> Iterator[UserSummary]:
        raise NotSupported(self.name, "following")

    def hashtag_media(self, tag: str, limit: int = 50, recent: bool = False) -> list[Media]:
        raise NotSupported(self.name, "hashtag_media")

    def search_users(self, query: str, limit: int = 20) -> list[UserSummary]:
        raise NotSupported(self.name, "search_users")

    def media_likers(self, media_id: str, limit: int = 0) -> list[UserSummary]:
        """Users who liked ``media_id``. ``limit=0`` means all the API returns."""
        raise NotSupported(self.name, "media_likers")

    # --- insights ------------------------------------------------------------
    def account_insights(
        self, metrics: list[str], period: str = "day", days: int = 30
    ) -> list[Insight]:
        raise NotSupported(self.name, "account_insights")

    def media_insights(self, media_id: str, metrics: list[str] | None = None) -> list[Insight]:
        raise NotSupported(self.name, "media_insights")

    # --- engagement ----------------------------------------------------------
    def comment(self, media_id: str, text: str, reply_to: str | None = None) -> Comment:
        raise NotSupported(self.name, "comment")

    def delete_comment(self, media_id: str, comment_id: str) -> None:
        raise NotSupported(self.name, "delete_comment")

    def hide_comment(self, comment_id: str, hide: bool = True) -> None:
        raise NotSupported(self.name, "hide_comment")

    def like(self, media_id: str) -> None:
        raise NotSupported(self.name, "like")

    def unlike(self, media_id: str) -> None:
        raise NotSupported(self.name, "unlike")

    def follow(self, username: str) -> None:
        raise NotSupported(self.name, "follow")

    def unfollow(self, username: str) -> None:
        raise NotSupported(self.name, "unfollow")

    # --- direct messages -----------------------------------------------------
    def list_threads(self, limit: int = 20, unread_only: bool = False) -> list[DirectThread]:
        raise NotSupported(self.name, "list_threads")

    def thread_messages(self, thread_id: str, limit: int = 20) -> list[DirectMessage]:
        raise NotSupported(self.name, "thread_messages")

    def send_dm(self, usernames: list[str], text: str) -> DirectMessage:
        raise NotSupported(self.name, "send_dm")

    def reply_dm(self, thread_id: str, text: str) -> DirectMessage:
        raise NotSupported(self.name, "reply_dm")

    def mark_thread_seen(self, thread_id: str) -> None:
        raise NotSupported(self.name, "mark_thread_seen")

    # --- downloads -----------------------------------------------------------
    def download_media(self, media_id: str, dest: str) -> list[str]:
        raise NotSupported(self.name, "download_media")

    def download_stories(self, username: str | None, dest: str) -> list[str]:
        """Download ``username``'s active stories into ``dest``; returns the file paths."""
        raise NotSupported(self.name, "download_stories")
