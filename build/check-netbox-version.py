#!/usr/bin/env python3
"""Assert the NetBox we build is one the plugin will load on.

Run by `make version-check`. Otherwise a mismatched pin surfaces as the
Dockerfile's import assert failing minutes into the build.

Mirrors NetBox's gate (netbox/netbox/plugins/__init__.py, PluginConfig.validate):
`packaging.version.parse` against `RELEASE.version` -- the version without the
designation, so 4.7.0-beta1 satisfies a 4.7.0 floor.

Inputs, from the environment:

    NETBOX_VERSION  what build/versions.env's NETBOX_REF reports
    FLOOR           the plugin's min_version (empty if unset)
    CEIL            the plugin's max_version (empty if unset)
"""

from __future__ import annotations

import os
import sys

from packaging.version import InvalidVersion, Version


def _parse(label: str, raw: str) -> Version | None:
    if not raw:
        return None
    try:
        return Version(raw)
    except InvalidVersion:
        sys.exit(f"version-check: {label}={raw!r} is not a version packaging can parse")


def main() -> None:
    raw_built = os.environ.get("NETBOX_VERSION", "")
    if not raw_built:
        sys.exit("version-check: build/versions.env does not set NETBOX_VERSION")

    built = _parse("NETBOX_VERSION", raw_built)
    floor = _parse("min_version", os.environ.get("FLOOR", ""))
    ceil = _parse("max_version", os.environ.get("CEIL", ""))

    if floor is not None and built < floor:
        sys.exit(
            f"version-check: build/versions.env builds NetBox {built}, but the plugin's "
            f"min_version is {floor}. Move NETBOX_REF forward, or lower the floor."
        )
    if ceil is not None and built > ceil:
        sys.exit(
            f"version-check: build/versions.env builds NetBox {built}, but the plugin's "
            f"max_version is {ceil}. Raise the ceiling, or move NETBOX_REF back."
        )


if __name__ == "__main__":
    main()
