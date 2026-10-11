"""Turn local files into public URLs so the Graph API can fetch them.

The Graph API never accepts file bytes for images: Instagram downloads every image (and
usually video) from a public URL. ``Config.media_host`` picks how local files are hosted:

* ``s3://bucket/prefix``   upload with boto3 and hand out a presigned GET URL (1 hour)
* ``cmd:<command>``        run a shell command; ``{path}`` is replaced by the (quoted) file
                           path and the last non-empty stdout line must be the URL
* ``imgbb:<ENV_VAR>``      upload to imgbb.com with the API key read from ``ENV_VAR``
                           (images only)
"""

from __future__ import annotations

import base64
import mimetypes
import os
import shlex
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from igctl.backends.base import IgctlError
from igctl.core.config import Config
from igctl.core.models import PublishRequest

IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic"})
JPEG_SUFFIXES = frozenset({".jpg", ".jpeg"})
VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"})

PRESIGN_SECONDS = 3600
IMGBB_ENDPOINT = "https://api.imgbb.com/1/upload"

_HELP = (
    "The Graph API needs public URLs for media. Either pass --url, or set `media_host` in "
    "config.yaml to one of:\n"
    "  s3://bucket/prefix          (needs the s3 extra: uv tool install '.[s3]')\n"
    "  cmd:<command with {path}>   (command prints the public URL on its last line)\n"
    "  imgbb:<ENV_VAR>             (imgbb.com API key in that env var; images only)"
)


def is_video(name: str | Path) -> bool:
    """Whether a path or URL looks like a video file (by extension, ignoring query)."""
    return url_suffix(name) in VIDEO_SUFFIXES


def url_suffix(name: str | Path) -> str:
    """Lower-case file suffix of a path or URL, ignoring any query string or fragment."""
    text = str(name).split("?", 1)[0].split("#", 1)[0]
    return Path(urlparse(text).path if "://" in text else text).suffix.lower()


def to_jpeg(path: Path, workdir: Path) -> Path:
    """``path`` itself if it is a JPEG, else a JPEG copy written into ``workdir``.

    The Graph API only accepts JPEG images. EXIF rotation is applied and transparency is
    flattened onto white.
    """
    path = Path(path)
    if path.suffix.lower() in JPEG_SUFFIXES:
        return path
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                rgba = im.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.getchannel("A"))
                im = flat
            else:
                im = im.convert("RGB")
            out = workdir / f"{path.stem}-{uuid.uuid4().hex[:8]}.jpg"
            im.save(out, "JPEG", quality=95)
    except (UnidentifiedImageError, OSError) as exc:
        raise IgctlError(f"{path.name}: can't convert to JPEG ({exc}).") from exc
    return out


def graph_direct_upload(req: PublishRequest, graph_host: str, config: Config) -> bool:
    """Whether the Graph backend can push ``req``'s local files itself (resumable upload).

    Only videos on graph.facebook.com qualify, and only without a media host configured.
    """
    return (
        graph_host.endswith("facebook.com")
        and not config.media_host
        and bool(req.paths)
        and all(is_video(p) for p in req.paths)
    )


def check_graph_hostable(req: PublishRequest, graph_host: str, config: Config) -> None:
    """Fail early when the Graph API could never fetch ``req``'s local files.

    Shared by ``post`` (now and ``--at``), ``schedule add`` and ``schedule import`` so a
    queued post doesn't fail hours later at publish time. Also rejects non-JPEG image URLs,
    which the Graph API refuses (local files are converted automatically).
    """
    for url in req.urls:
        suffix = url_suffix(url)
        if suffix in IMAGE_SUFFIXES and suffix not in JPEG_SUFFIXES:
            raise IgctlError(
                f"{url}: the Graph API only accepts JPEG images, not '{suffix}'. Convert it, "
                "or pass the local file so igctl converts and hosts it."
            )
    if config.media_host:
        return
    needs_host = bool(req.paths) and not graph_direct_upload(req, graph_host, config)
    if needs_host or req.cover is not None:
        raise IgctlError(
            "The Graph API fetches media from public URLs. Pass --url, set `media_host` in "
            "config.yaml, or use --backend private."
        )


def resolve_urls(req: PublishRequest, config: Config) -> PublishRequest:
    """Return ``req`` with ``urls`` filled in, uploading ``req.paths`` if needed."""
    if req.urls:
        return req
    if not req.paths:
        raise IgctlError("Nothing to publish: no file paths or URLs given.")
    urls = [upload_file(p, config) for p in req.paths]
    return req.model_copy(update={"urls": urls})


def upload_file(path: Path, config: Config) -> str:
    """Upload one local file via ``config.media_host`` and return its public URL."""
    path = Path(path)
    if not path.is_file():
        raise IgctlError(f"File not found: {path}")
    spec = (config.media_host or "").strip()
    if not spec:
        raise IgctlError(_HELP)
    if spec.startswith("s3://"):
        return _upload_s3(path, spec)
    if spec.startswith("cmd:"):
        return _upload_cmd(path, spec[4:].strip())
    if spec.startswith("imgbb:"):
        return _upload_imgbb(path, spec[6:].strip())
    raise IgctlError(f"Unrecognised media_host '{spec}'.\n{_HELP}")


def _upload_s3(path: Path, spec: str) -> str:
    try:
        import boto3  # type: ignore[import-not-found]
    except ImportError as exc:
        raise IgctlError(
            "media_host is s3:// but boto3 is not installed. "
            "Install the s3 extra: `uv tool install '.[s3]'`."
        ) from exc
    bucket, _, prefix = spec[len("s3://") :].partition("/")
    if not bucket:
        raise IgctlError(f"Invalid S3 media_host '{spec}': expected s3://bucket/prefix.")
    key = "/".join(p for p in (prefix.strip("/"), f"{uuid.uuid4().hex}{path.suffix.lower()}") if p)
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    try:
        s3 = boto3.client("s3")
        s3.upload_file(str(path), bucket, key, ExtraArgs={"ContentType": content_type})
        return s3.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=PRESIGN_SECONDS
        )
    except Exception as exc:  # botocore raises a zoo of exception types
        raise IgctlError(f"S3 upload of {path.name} failed: {exc}") from exc


def _quote(path: Path) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline([str(path)])
    return shlex.quote(str(path))


def _upload_cmd(path: Path, template: str) -> str:
    if "{path}" not in template:
        raise IgctlError("media_host cmd: template must contain a {path} placeholder.")
    command = template.replace("{path}", _quote(path))
    try:
        proc = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired as exc:
        raise IgctlError(f"Upload command timed out: {command}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise IgctlError(f"Upload command failed ({proc.returncode}): {detail}")
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    if not lines or not lines[-1].startswith(("http://", "https://")):
        raise IgctlError(f"Upload command did not print a URL on its last line: {command}")
    return lines[-1]


def _upload_imgbb(path: Path, env_var: str) -> str:
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        raise IgctlError(f"imgbb only hosts images; cannot upload {path.name}.")
    key = os.environ.get(env_var or "IMGBB_API_KEY")
    if not key:
        raise IgctlError(f"media_host is imgbb but env var {env_var!r} is not set.")
    payload = {"key": key, "image": base64.b64encode(path.read_bytes()).decode("ascii")}
    try:
        resp = httpx.post(IMGBB_ENDPOINT, data=payload, timeout=120)
        resp.raise_for_status()
        return resp.json()["data"]["url"]
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise IgctlError(f"imgbb upload of {path.name} failed: {exc}") from exc
