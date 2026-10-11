"""Pre-flight checks of a :class:`PublishRequest` against Instagram's publishing limits."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image, UnidentifiedImageError

from igctl.backends.base import IgctlError
from igctl.core.models import MediaType, PublishRequest

IMAGE_EXT = frozenset({".jpg", ".jpeg", ".png", ".webp"})
VIDEO_EXT = frozenset({".mp4", ".mov"})
MAX_CAPTION = 2200
MAX_HASHTAGS = 30
MAX_USER_TAGS = 20
CAROUSEL_MIN, CAROUSEL_MAX = 2, 10
FEED_MIN_RATIO, FEED_MAX_RATIO = 4 / 5, 1.91
VERTICAL_RATIO = 9 / 16
_RATIO_TOLERANCE = 0.01
_HASHTAG_RE = re.compile(r"(?<![\w&])#\w+")
_EXIF_ORIENTATION = 274


def _ext(source: Path | str) -> str:
    path = source if isinstance(source, Path) else Path(urlparse(source).path)
    return path.suffix.lower()


def caption_problems(caption: str) -> str | None:
    """Why ``caption`` would be rejected by Instagram, or None if it is fine."""
    if len(caption) > MAX_CAPTION:
        return f"Caption is {len(caption)} chars; the limit is {MAX_CAPTION}."
    if (n := len(_HASHTAG_RE.findall(caption))) > MAX_HASHTAGS:
        return f"Caption has {n} hashtags; the limit is {MAX_HASHTAGS}."
    return None


def image_ratio(path: Path) -> float:
    """Width / height as displayed (EXIF rotation applied)."""
    try:
        with Image.open(path) as im:
            w, h = im.size
            if im.getexif().get(_EXIF_ORIENTATION) in (5, 6, 7, 8):
                w, h = h, w
    except (UnidentifiedImageError, OSError) as e:
        raise IgctlError(f"{path}: not a readable image ({e}).") from e
    return w / h


def _allowed(media_type: MediaType) -> frozenset[str]:
    if media_type is MediaType.IMAGE:
        return IMAGE_EXT
    if media_type in (MediaType.VIDEO, MediaType.REEL):
        return VIDEO_EXT
    return IMAGE_EXT | VIDEO_EXT


def validate_request(req: PublishRequest) -> list[str]:
    """Pre-flight checks against Instagram's limits.

    Raises :class:`IgctlError` on hard failures; returns non-fatal warnings.
    """
    warnings: list[str] = []
    sources: list[Path | str] = [*req.paths, *req.urls]
    if not sources:
        raise IgctlError("Nothing to publish: give a FILE or --url.")
    if req.paths and req.urls:
        raise IgctlError("Use either local files or --url, not both.")
    if req.media_type is MediaType.CAROUSEL:
        if not CAROUSEL_MIN <= len(sources) <= CAROUSEL_MAX:
            raise IgctlError(
                f"A carousel needs {CAROUSEL_MIN}-{CAROUSEL_MAX} items, got {len(sources)}."
            )
    elif len(sources) != 1:
        raise IgctlError(f"A {req.media_type} post takes exactly one item, got {len(sources)}.")

    allowed = _allowed(req.media_type)
    for src in sources:
        ext = _ext(src)
        if isinstance(src, Path):
            if not src.is_file():
                raise IgctlError(f"File not found: {src}")
        elif not src.startswith(("http://", "https://")):
            raise IgctlError(f"--url must be an http(s) URL: {src}")
        elif not ext:
            continue  # extension-less URLs (CDNs) can't be checked locally
        if ext not in allowed:
            raise IgctlError(
                f"{src}: '{ext or 'no extension'}' is not allowed for {req.media_type} "
                f"(allowed: {', '.join(sorted(allowed))})."
            )
        if isinstance(src, Path) and ext in IMAGE_EXT:
            ratio = image_ratio(src)
            if req.media_type is MediaType.STORY:
                if abs(ratio - VERTICAL_RATIO) > _RATIO_TOLERANCE:
                    warnings.append(
                        f"{src.name}: aspect {ratio:.2f} is not 9:16; it will be letterboxed."
                    )
            elif not FEED_MIN_RATIO - _RATIO_TOLERANCE <= ratio <= FEED_MAX_RATIO + 0.005:
                raise IgctlError(
                    f"{src.name}: aspect ratio {ratio:.2f} is outside Instagram's feed range "
                    "4:5 (0.80) to 1.91:1; crop it first."
                )

    if req.cover is not None:
        if req.media_type is not MediaType.REEL:
            raise IgctlError("--cover only applies to reels.")
        if not req.cover.is_file():
            raise IgctlError(f"Cover not found: {req.cover}")
        if _ext(req.cover) not in IMAGE_EXT:
            raise IgctlError("--cover must be an image (jpg/png/webp).")
        if abs(image_ratio(req.cover) - VERTICAL_RATIO) > _RATIO_TOLERANCE:
            warnings.append("Reel cover is not 9:16.")

    if problem := caption_problems(req.caption):
        raise IgctlError(problem)
    if len(req.user_tags) > MAX_USER_TAGS:
        raise IgctlError(f"{len(req.user_tags)} user tags; the limit is {MAX_USER_TAGS}.")
    if req.user_tags and req.media_type is MediaType.STORY:
        warnings.append("User tags are ignored for stories by some backends.")
    return warnings
