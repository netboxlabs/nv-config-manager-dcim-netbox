#!/usr/bin/env python3
"""Fail the build if NetBox will not load a plugin the chart enables.

`import netbox_bgp` succeeding proves nothing. NetBox applies each plugin's
min_version/max_version and, on failure, warns and drops it (settings.py:
`except IncompatiblePluginError ... warnings.warn(f'Unable to load plugin ...')`
then `continue`), so the plugin never reaches
`registry['plugins']['installed']`. Nothing else fails, and at runtime that
plugin's data is simply absent.

Run with NetBox's settings importable and PLUGINS already set. Arguments are the
plugin names to require.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, "/opt/netbox/netbox")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "netbox.settings")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from netbox.registry import registry  # noqa: E402


def main() -> None:
    required = sys.argv[1:]
    if not required:
        sys.exit("usage: assert-plugins-load.py <plugin> [<plugin> ...]")

    installed = set(registry["plugins"]["installed"])
    missing = [name for name in required if name not in installed]
    if missing:
        sys.exit(
            f"NetBox {settings.RELEASE.version} did not load: {', '.join(missing)}.\n"
            "Most often the plugin's max_version is below this NetBox; the build log has "
            "the 'Unable to load plugin' warning with the exact bound."
        )
    print(f"plugins loaded on NetBox {settings.RELEASE.version}: {', '.join(required)}")


if __name__ == "__main__":
    main()
