# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""NetBox v2 API token generation.

One token is generated per deployment and used by both sides: NetBox installs it
(the plugin's ``nvcm_init`` reads ``SUPERUSER_API_TOKEN`` and splits it into the
stored key + secret) and NVCM authenticates with it. Generating rather than
reading it back is what lets NetBox and NVCM be deployed in either order.
"""

from __future__ import annotations

import re
import secrets
import string

# Mirrors netbox/users/constants.py. A v2 token is `nbt_<key>.<secret>`; the
# plugin rejects anything else, and NetBox stores the key verbatim while HMACing
# the secret with a configured pepper.
TOKEN_PREFIX = "nbt_"
TOKEN_KEY_LENGTH = 12
TOKEN_SECRET_LENGTH = 40
TOKEN_CHARSET = string.ascii_letters + string.digits

_TOKEN_RE = re.compile(
    rf"^{re.escape(TOKEN_PREFIX)}[A-Za-z0-9]{{{TOKEN_KEY_LENGTH}}}"
    rf"\.[A-Za-z0-9]{{{TOKEN_SECRET_LENGTH}}}$"
)


def _random(length: int) -> str:
    return "".join(secrets.choice(TOKEN_CHARSET) for _ in range(length))


def generate_api_token() -> str:
    """Return a fresh NetBox v2 API token."""
    return f"{TOKEN_PREFIX}{_random(TOKEN_KEY_LENGTH)}.{_random(TOKEN_SECRET_LENGTH)}"


def is_valid_api_token(value: str) -> bool:
    """Return whether *value* is a well-formed v2 token.

    Shape only — this cannot tell whether NetBox actually holds it. Used to fail
    fast on a hand-supplied ``--token``, because the plugin silently skips
    installing a malformed one and the failure would otherwise surface much later
    as NVCM authentication errors.
    """
    return bool(_TOKEN_RE.match(value or ""))
