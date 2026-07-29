"""TikTok Shop's credential spec.

All the machinery lives in `connectors.credentials`. What is TikTok-specific is
the field list, the environment variable names, and the remedy text — plus one
behaviour no other provider here needs: TikTok rotates the refresh token on
every refresh, so the source has to be able to write one back.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..credentials import (
    ChainedCredentials,
    CredentialSource,
    CredentialSpec,
    CredentialStatus,
    CredentialsUnavailable,
    build_default_chain,
)
from ..credentials import EnvCredentials as _EnvCredentials
from ..credentials import FileCredentials as _FileCredentials
from ..credentials import StaticCredentials as _StaticCredentials
from ..credentials import UnavailableCredentials as _UnavailableCredentials
from .auth import TikTokCredentials

REQUIRED_FIELDS = ("app_key", "app_secret", "refresh_token", "shop_id")

ENV_KEYS = {
    "app_key": "TIKTOK_APP_KEY",
    "app_secret": "TIKTOK_APP_SECRET",
    "refresh_token": "TIKTOK_REFRESH_TOKEN",
    "shop_id": "TIKTOK_SHOP_ID",
}

REGISTRATION_REMEDY = (
    "TikTok Shop Open API access requires an eligible seller account. If "
    "registration is blocked, eligibility usually depends on shop age, order "
    "volume, and account standing rather than anything you can configure — it "
    "commonly clears within weeks of consistent selling.\n"
    "  Meanwhile the operator runs on Seller Center CSV exports, which need no "
    "API at all: Seller Center > Orders > Export, and Finance > Statements > "
    "Export. Import them with `tiktok-import`."
)

SPEC = CredentialSpec(
    provider="tiktok",
    required=REQUIRED_FIELDS,
    env_keys=ENV_KEYS,
    build=lambda v: TikTokCredentials(
        app_key=v["app_key"], app_secret=v["app_secret"],
        refresh_token=v["refresh_token"], shop_id=v["shop_id"],
    ),
    rotating_field="refresh_token",
    remedy=REGISTRATION_REMEDY,
)


class EnvCredentials(_EnvCredentials):
    def __init__(self) -> None:
        super().__init__(SPEC)


class FileCredentials(_FileCredentials):
    def __init__(self, path: str | Path) -> None:
        super().__init__(SPEC, path)

    def persist_refresh_token(self, token: str) -> None:
        """Write back a rotated refresh token.

        Passed to `TokenProvider(on_refresh_token_rotated=...)`. Without it a
        rotation is lost and the integration keeps working until the old token
        expires, then fails months later with no deploy to correlate against.
        """
        self.persist_rotated("refresh_token", token)


class StaticCredentials(_StaticCredentials):
    def __init__(self, credentials: TikTokCredentials) -> None:
        super().__init__(SPEC, credentials)


class UnavailableCredentials(_UnavailableCredentials):
    def __init__(self, reason: str = "TikTok Open API access is not provisioned.",
                 *, remedy: str = REGISTRATION_REMEDY) -> None:
        super().__init__(SPEC, reason, remedy=remedy)


def default_source(*, credential_file: str | Path | None = None) -> CredentialSource:
    """The standard chain: file, then environment, then explicit unavailable."""
    sources: list[CredentialSource] = []
    path = credential_file or os.environ.get("TIKTOK_CREDENTIAL_FILE")
    if path:
        sources.append(FileCredentials(path))
    sources.append(EnvCredentials())
    sources.append(UnavailableCredentials())
    return ChainedCredentials(*sources)


__all__ = [
    "SPEC", "REQUIRED_FIELDS", "ENV_KEYS", "REGISTRATION_REMEDY",
    "CredentialSource", "CredentialSpec", "CredentialStatus",
    "CredentialsUnavailable", "EnvCredentials", "FileCredentials",
    "StaticCredentials", "UnavailableCredentials", "ChainedCredentials",
    "default_source", "build_default_chain",
]
