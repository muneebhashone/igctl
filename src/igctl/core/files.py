"""Reading user-supplied text files (captions, user lists, rules, calendars)."""

from __future__ import annotations

import codecs
from pathlib import Path

from igctl.backends.base import IgctlError

_UTF16_BOMS = (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)


def read_text_file(path: str | Path, what: str = "file") -> str:
    """Decode ``path`` as UTF-8 (BOM stripped) or BOM-marked UTF-16.

    Windows tools often write UTF-16 (PowerShell 5.1 ``>``/``Out-File``) or add a UTF-8 BOM
    (Notepad, Excel's "CSV UTF-8"). Unreadable or undecodable files raise
    :class:`IgctlError` with a clear message instead of a traceback.
    """
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise IgctlError(f"Can't read {what} {path}: {e.strerror or e}") from e
    try:
        if raw.startswith(_UTF16_BOMS):
            return raw.decode("utf-16")
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise IgctlError(
            f"{what.capitalize()} {path} is not UTF-8 text (byte {e.start}: "
            f"{raw[e.start : e.start + 1]!r}). Re-save it as UTF-8 (e.g. Excel: "
            "'CSV UTF-8'; PowerShell: `Out-File -Encoding utf8`)."
        ) from e
