"""Backend-neutral data models shared by every igctl module."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field


class MediaType(StrEnum):
    IMAGE = "image"
    VIDEO = "video"
    REEL = "reel"
    CAROUSEL = "carousel"
    STORY = "story"


class Profile(BaseModel):
    id: str
    username: str
    full_name: str | None = None
    biography: str | None = None
    followers_count: int | None = None
    following_count: int | None = None
    media_count: int | None = None
    is_private: bool | None = None
    is_verified: bool | None = None
    is_business: bool | None = None
    category: str | None = None
    external_url: str | None = None
    profile_pic_url: str | None = None


class Media(BaseModel):
    id: str
    code: str | None = None  # shortcode used in instagram.com/p/<code>
    media_type: MediaType
    caption: str | None = None
    taken_at: datetime | None = None
    like_count: int | None = None
    comment_count: int | None = None
    view_count: int | None = None
    permalink: str | None = None
    media_url: str | None = None
    thumbnail_url: str | None = None
    owner_username: str | None = None
    hashtags: list[str] = Field(default_factory=list)


class Comment(BaseModel):
    id: str
    media_id: str
    text: str
    username: str
    user_id: str | None = None
    created_at: datetime | None = None
    like_count: int | None = None
    replied_to_id: str | None = None


class UserSummary(BaseModel):
    """A lightweight user reference (follower, liker, search hit...)."""

    id: str
    username: str
    full_name: str | None = None
    is_private: bool | None = None
    is_verified: bool | None = None


class DirectThread(BaseModel):
    id: str
    title: str | None = None
    users: list[UserSummary] = Field(default_factory=list)
    last_message: str | None = None
    last_activity_at: datetime | None = None
    unread: bool = False


class DirectMessage(BaseModel):
    id: str
    thread_id: str
    user_id: str | None = None
    text: str | None = None
    created_at: datetime | None = None


class Insight(BaseModel):
    """One metric value, e.g. reach=1234 for a period or a media item."""

    name: str
    value: float | int | dict | None
    period: str | None = None
    end_time: datetime | None = None


class PublishRequest(BaseModel):
    media_type: MediaType
    paths: list[Path] = Field(default_factory=list)  # local files (private backend / upload)
    urls: list[str] = Field(default_factory=list)  # public URLs (Graph API requires these)
    caption: str = ""
    cover: Path | None = None
    location: str | None = None
    user_tags: list[str] = Field(default_factory=list)
    share_to_feed: bool = True  # for reels


class PublishResult(BaseModel):
    media_id: str
    permalink: str | None = None
    backend: str
