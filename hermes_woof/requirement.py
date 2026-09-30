"""The WOOF release line this plugin is built and tested against.

The plugin drives WOOF only through its command line and JSON doors
(run-plan, the catalogs, render), whose shape can change between minor
releases, so the plugin states the line it was checked on: recast-woof 1.0.x.
`hermes woof install` installs within it and `woof_doctor` says when the
installed WOOF is outside it.
"""
from __future__ import annotations

import re
from typing import Optional

PACKAGE = "recast-woof"
REQUIREMENT = ">=1.0.0,<1.1"
_LOW = (1, 0, 0)
_HIGH = (1, 1, 0)
_VERSION_RE = re.compile(r"^\s*v?(\d+)\.(\d+)(?:\.(\d+))?")


def pip_spec(extra: str = "") -> str:
    """The pip requirement, for example recast-woof[all-cu13]>=1.0.0,<1.1."""
    return f"{PACKAGE}[{extra}]{REQUIREMENT}" if extra else f"{PACKAGE}{REQUIREMENT}"


def parse(version: Optional[str]) -> Optional[tuple]:
    m = _VERSION_RE.match(version or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def supported(version: Optional[str]) -> Optional[bool]:
    """True inside the tested line, False outside it, None when the version cannot be read."""
    v = parse(version)
    if v is None:
        return None
    return _LOW <= v < _HIGH
