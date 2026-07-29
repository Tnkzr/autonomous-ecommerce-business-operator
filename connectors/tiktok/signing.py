"""TikTok Shop request signing.

Every call carries an HMAC-SHA256 signature. Get any detail wrong and the API
returns a generic authentication failure that says nothing about which detail —
so this module is isolated, exhaustively commented, and tested against
hand-computed vectors.

The algorithm, in TikTok's required order:

  1. Take all query parameters EXCEPT `sign` and `access_token`.
  2. Sort them by key, ASCII ascending.
  3. Concatenate as `{key}{value}` with no separators.
  4. Prepend the request path (the part after the host, no query string).
  5. If there is a JSON body, append the raw body string.
  6. Wrap the whole thing in `app_secret` on BOTH ends.
  7. HMAC-SHA256 that string using `app_secret` as the key, hex digest.

Two exclusions people get wrong: `access_token` is excluded even though it is a
query parameter on some older endpoints, and `sign` obviously cannot sign
itself. Including either produces a valid-looking signature that always fails.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, Mapping

# Never contribute to the signature base string.
EXCLUDED_PARAMS = frozenset({"sign", "access_token"})


def canonical_string(
    *,
    path: str,
    params: Mapping[str, Any],
    app_secret: str,
    body: str | None = None,
) -> str:
    """Build the exact string that gets signed.

    Exposed separately from `sign_request` because when a signature is rejected,
    the only useful debugging step is comparing this string against what you
    expected — and you cannot do that if it is buried inside the HMAC call.
    """
    sorted_pairs = sorted(
        (k, v) for k, v in params.items() if k not in EXCLUDED_PARAMS and v is not None
    )
    joined = "".join(f"{k}{_stringify(v)}" for k, v in sorted_pairs)

    base = f"{path}{joined}"
    if body:
        base = f"{base}{body}"

    # The secret wraps the payload on both sides, then is also the HMAC key.
    # That is unusual but it is what TikTok specifies.
    return f"{app_secret}{base}{app_secret}"


def sign_request(
    *,
    path: str,
    params: Mapping[str, Any],
    app_secret: str,
    body: str | None = None,
) -> str:
    """Return the hex signature for a request."""
    if not app_secret:
        raise ValueError(
            "Cannot sign a TikTok Shop request without an app secret. "
            "Set TIKTOK_APP_SECRET."
        )
    base = canonical_string(path=path, params=params, app_secret=app_secret, body=body)
    return hmac.new(
        app_secret.encode("utf-8"),
        base.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _stringify(value: Any) -> str:
    """Match TikTok's expectation for non-string parameter values.

    Python's `str(True)` is "True"; every HTTP API expects "true". A boolean
    parameter serialised the Python way signs correctly against a string that
    the server never sees, which fails with no useful error.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
