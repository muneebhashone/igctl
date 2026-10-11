"""Parsing of user-supplied media and user references (ids, shortcodes, URLs, @names)."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from igctl.backends.base import Backend

SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_SHORTCODE_RE = re.compile(r"[A-Za-z0-9_-]+")
_MEDIA_ID_RE = re.compile(r"\d+(?:_\d+)?")  # pk, or instagrapi's "<pk>_<owner id>"
_MEDIA_PATH_RE = re.compile(r"/(?:p|reels?|tv)/([A-Za-z0-9_-]+)")


def shortcode_to_pk(code: str) -> int:
    """Decode an Instagram shortcode (``B1LbfVPlwIA``) to the numeric media pk.

    Shortcodes are the pk in base64 with Instagram's URL-safe alphabet. Codes of private
    posts carry an extra suffix after the first 11 characters, which is ignored.
    """
    code = code.strip()[:11]
    if not code or not _SHORTCODE_RE.fullmatch(code):
        raise ValueError(f"Not a valid Instagram shortcode: {code!r}")
    pk = 0
    for ch in code:
        pk = pk * 64 + SHORTCODE_ALPHABET.index(ch)
    return pk


def pk_to_shortcode(pk: int | str) -> str:
    """Inverse of :func:`shortcode_to_pk`."""
    n = int(str(pk).split("_")[0])
    if n < 0:
        raise ValueError("pk must be non-negative")
    out = ""
    while True:
        n, r = divmod(n, 64)
        out = SHORTCODE_ALPHABET[r] + out
        if n == 0:
            return out


def is_url(ref: str) -> bool:
    return "://" in ref or ref.startswith(("instagram.com", "www.instagram.com", "instagr.am"))


def shortcode_from_url(url: str) -> str:
    """Extract the shortcode from a /p/, /reel/, /reels/ or /tv/ URL."""
    if "://" not in url:
        url = "https://" + url
    m = _MEDIA_PATH_RE.search(urlparse(url).path)
    if not m:
        raise ValueError(f"Not an Instagram post/reel URL: {url}")
    return m.group(1)


def media_ref_to_pk(ref: str) -> str:
    """Media id passthrough, or the private-API pk decoded from a shortcode / post URL."""
    ref = ref.strip()
    if _MEDIA_ID_RE.fullmatch(ref):
        return ref
    code = shortcode_from_url(ref) if "/" in ref else ref
    return str(shortcode_to_pk(code))


def resolve_media(backend: Backend, ref: str) -> str:
    """Turn a media id, shortcode or post URL into a media id ``backend`` understands."""
    ref = ref.strip()
    if _MEDIA_ID_RE.fullmatch(ref):
        return ref
    if is_url(ref):
        from_url = getattr(backend, "media_pk_from_url", None)
        if callable(from_url):
            return str(from_url(ref))
        code = shortcode_from_url(ref)
    else:
        code = ref
    if backend.name == "graph":
        # Graph media ids are unrelated to shortcodes; look the code up among own posts.
        for media in backend.list_media(limit=200):
            if media.code == code:
                return media.id
    return str(shortcode_to_pk(code))


def normalize_username(ref: str) -> str:
    """Accept ``name``, ``@name`` or ``instagram.com/name/``."""
    ref = ref.strip()
    if is_url(ref):
        ref = urlparse(ref if "://" in ref else "https://" + ref).path.strip("/").split("/")[0]
    return ref.lstrip("@")
