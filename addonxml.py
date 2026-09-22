"""The installed addon.xml, for the few settings that live in it.

Kodi reads ``<reuselanguageinvoker>`` from ``xbmc.addon.metadata`` at addon
load. A settings toggle can only change the on-disk file; the next Kodi
restart is what makes ExtraInfo() move. The zip always ships true; after an
update overwrites the file, the service reconciles a false setting back onto
disk.

Kodi-free: the service and the settings handler pass in the path.
"""

import os
import re

REUSE_TAG = re.compile(r"(<reuselanguageinvoker>)(true|false)(</reuselanguageinvoker>)")


def with_reuse_invoker(text, enabled):
    """``text`` with the tag set to true/false, or None if the tag is absent."""
    wanted = "true" if enabled else "false"
    new, count = REUSE_TAG.subn(r"\g<1>%s\g<3>" % wanted, text, count=1)
    return new if count else None


def apply(enabled, path):
    """Align the installed addon.xml with ``enabled``.

    Returns True if a write happened, False if the file already matched,
    None if the file could not be read or written or had no tag.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return None
    updated = with_reuse_invoker(text, enabled)
    if updated is None:
        return None
    if updated == text:
        return False
    temporary = path + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(updated)
        os.replace(temporary, path)
    except OSError:
        try:
            os.remove(temporary)
        except OSError:
            pass
        return None
    return True
