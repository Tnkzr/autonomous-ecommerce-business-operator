"""Credential resolution, separated from everything that uses credentials.

The point of this module is that no other file knows *where* credentials come
from. `Transport` takes a token provider; the token provider takes a
`CredentialSource`. Swapping environment variables for a file, a secrets
manager, or a database is one line at construction and touches nothing else.

That matters right now because TikTok Open API registration is gated on shop
eligibility. The rest of the integration must be completable, testable, and
reviewable without any credential existing — so `UnavailableCredentials` is a
first-class implementation rather than an error path bolted on later. It
answers every question the system asks except "give me a token", and for that
one it raises with an explanation of what is blocked and why.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .auth import TikTokCredentials


class CredentialsUnavailable(RuntimeError):
    """Credentials do not exist yet. Distinct from credentials being wrong.

    A wrong credential is a bug to fix; an absent one is a state to wait in.
    Conflating them produces alarming errors for an expected situation and
    hides real auth failures inside the noise.
    """

    def __init__(self, reason: str, *, remedy: str = "") -> None:
        message = reason
        if remedy:
            message += f"\n\nTo resolve: {remedy}"
        super().__init__(message)
        self.reason = reason
        self.remedy = remedy


@dataclass
class CredentialStatus:
    """What a source can tell us without actually producing a secret."""

    available: bool
    source_name: str
    detail: str
    present_fields: list[str]
    missing_fields: list[str]
    remedy: str = ""


class CredentialSource(ABC):
    """Where credentials come from. Implementations must never log secrets."""

    name: str = "base"

    @abstractmethod
    def status(self) -> CredentialStatus:
        """Describe availability without resolving anything sensitive."""

    @abstractmethod
    def resolve(self) -> TikTokCredentials:
        """Return credentials, or raise `CredentialsUnavailable`."""

    @property
    def available(self) -> bool:
        return self.status().available

    def __repr__(self) -> str:  # pragma: no cover - safety, not logic
        return f"<{type(self).__name__} name={self.name} available={self.available}>"


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


class EnvCredentials(CredentialSource):
    """Read from environment variables. The default in production."""

    name = "environment"

    def status(self) -> CredentialStatus:
        present = [f for f, k in ENV_KEYS.items() if os.environ.get(k)]
        missing = [f for f in REQUIRED_FIELDS if f not in present]
        return CredentialStatus(
            available=not missing,
            source_name=self.name,
            detail=(
                "All credentials present in the environment." if not missing
                else f"Missing environment variable(s): "
                     f"{', '.join(ENV_KEYS[f] for f in missing)}"
            ),
            present_fields=present,
            missing_fields=missing,
            remedy="" if not missing else REGISTRATION_REMEDY,
        )

    def resolve(self) -> TikTokCredentials:
        st = self.status()
        if not st.available:
            raise CredentialsUnavailable(st.detail, remedy=st.remedy)
        return TikTokCredentials(
            app_key=os.environ[ENV_KEYS["app_key"]],
            app_secret=os.environ[ENV_KEYS["app_secret"]],
            refresh_token=os.environ[ENV_KEYS["refresh_token"]],
            shop_id=os.environ[ENV_KEYS["shop_id"]],
        )


class FileCredentials(CredentialSource):
    """Read from a JSON file.

    Useful because TikTok rotates the refresh token on every refresh: a file
    can be written back, an environment variable cannot. `persist_refresh_token`
    is the callback the token provider uses, which is what stops a rotated
    token from being lost and stranding the integration weeks later.
    """

    name = "file"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CredentialsUnavailable(
                f"Credential file {self.path} is not valid JSON: {exc}",
                remedy="Fix or delete the file; a malformed one is not treated as "
                       "absent because that would silently fall back to no access.",
            ) from exc
        return data if isinstance(data, dict) else {}

    def status(self) -> CredentialStatus:
        if not self.path.exists():
            return CredentialStatus(
                available=False, source_name=self.name,
                detail=f"Credential file {self.path} does not exist.",
                present_fields=[], missing_fields=list(REQUIRED_FIELDS),
                remedy=REGISTRATION_REMEDY,
            )
        data = self._read()
        present = [f for f in REQUIRED_FIELDS if data.get(f)]
        missing = [f for f in REQUIRED_FIELDS if f not in present]
        return CredentialStatus(
            available=not missing, source_name=self.name,
            detail=(f"All credentials present in {self.path}." if not missing
                    else f"{self.path} is missing: {', '.join(missing)}"),
            present_fields=present, missing_fields=missing,
            remedy="" if not missing else REGISTRATION_REMEDY,
        )

    def resolve(self) -> TikTokCredentials:
        st = self.status()
        if not st.available:
            raise CredentialsUnavailable(st.detail, remedy=st.remedy)
        data = self._read()
        return TikTokCredentials(
            app_key=data["app_key"], app_secret=data["app_secret"],
            refresh_token=data["refresh_token"], shop_id=data["shop_id"],
        )

    def persist_refresh_token(self, token: str) -> None:
        """Write back a rotated refresh token.

        Passed to `TokenProvider(on_refresh_token_rotated=...)`. Without it a
        rotation is lost and the integration keeps working until the old token
        expires, then fails months later with no deploy to correlate against.
        """
        data = self._read()
        data["refresh_token"] = token
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            self.path.chmod(0o600)
        except OSError:  # pragma: no cover - platform dependent
            pass


class UnavailableCredentials(CredentialSource):
    """Explicitly no credentials, with a reason.

    Not a stub: this is the correct state while API registration is blocked. It
    lets the whole system be constructed, inspected, and tested without
    pretending access exists, and it names the blocker rather than producing a
    generic auth error.
    """

    name = "unavailable"

    def __init__(self, reason: str = "TikTok Open API access is not provisioned.",
                 *, remedy: str = REGISTRATION_REMEDY) -> None:
        self.reason = reason
        self.remedy = remedy

    def status(self) -> CredentialStatus:
        return CredentialStatus(
            available=False, source_name=self.name, detail=self.reason,
            present_fields=[], missing_fields=list(REQUIRED_FIELDS),
            remedy=self.remedy,
        )

    def resolve(self) -> TikTokCredentials:
        raise CredentialsUnavailable(self.reason, remedy=self.remedy)


class StaticCredentials(CredentialSource):
    """In-memory credentials. For tests and for one-off scripted runs."""

    name = "static"

    def __init__(self, credentials: TikTokCredentials) -> None:
        self._credentials = credentials

    def status(self) -> CredentialStatus:
        missing = [f for f in REQUIRED_FIELDS
                   if not getattr(self._credentials, f, "")]
        return CredentialStatus(
            available=not missing, source_name=self.name,
            detail="In-memory credentials." if not missing
                   else f"Missing: {', '.join(missing)}",
            present_fields=[f for f in REQUIRED_FIELDS if f not in missing],
            missing_fields=missing,
        )

    def resolve(self) -> TikTokCredentials:
        st = self.status()
        if not st.available:
            raise CredentialsUnavailable(st.detail)
        return self._credentials


class ChainedCredentials(CredentialSource):
    """Try sources in order, use the first that is available.

    The production default: a file (which can absorb token rotation) preferred
    over the environment, falling back to an explicit unavailable state rather
    than to a confusing error.
    """

    name = "chain"

    def __init__(self, *sources: CredentialSource) -> None:
        if not sources:
            raise ValueError("ChainedCredentials needs at least one source.")
        self.sources = sources

    def _first_available(self) -> CredentialSource | None:
        return next((s for s in self.sources if s.status().available), None)

    def status(self) -> CredentialStatus:
        winner = self._first_available()
        if winner is not None:
            st = winner.status()
            return CredentialStatus(
                available=True, source_name=f"chain->{st.source_name}",
                detail=st.detail, present_fields=st.present_fields,
                missing_fields=[],
            )
        details = "; ".join(f"{s.name}: {s.status().detail}" for s in self.sources)
        remedy = next((s.status().remedy for s in self.sources if s.status().remedy), "")
        return CredentialStatus(
            available=False, source_name="chain",
            detail=f"No source has complete credentials. Tried — {details}",
            present_fields=[], missing_fields=list(REQUIRED_FIELDS), remedy=remedy,
        )

    def resolve(self) -> TikTokCredentials:
        winner = self._first_available()
        if winner is None:
            st = self.status()
            raise CredentialsUnavailable(st.detail, remedy=st.remedy)
        return winner.resolve()


def default_source(*, credential_file: str | Path | None = None) -> CredentialSource:
    """The standard chain: file, then environment, then explicit unavailable."""
    sources: list[CredentialSource] = []
    path = credential_file or os.environ.get("TIKTOK_CREDENTIAL_FILE")
    if path:
        sources.append(FileCredentials(path))
    sources.append(EnvCredentials())
    sources.append(UnavailableCredentials())
    return ChainedCredentials(*sources)
