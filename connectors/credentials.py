"""Provider-agnostic credential resolution.

Every connector needs the same five answers about its secrets — are they here,
where did they come from, what is missing, how do I get them, and can I write a
rotated one back — and none of those answers are marketplace-specific. Only the
*field list* is. So the machinery lives here once and each provider contributes
a `CredentialSpec`.

The reason this is a module and not four lines inside each connector is the
charter clause that adding a marketplace must never require changing business
logic. Auth is the place that clause is easiest to violate: it is tempting to
read `os.environ` at the call site, and then every new provider edits the same
functions. Here, a new provider is a spec.

Two states that are constantly conflated and must not be:

**Absent** is not **wrong**. A missing credential is a state to wait in; a
rejected one is a bug to fix. `UnavailableCredentials` is a first-class
implementation precisely so a system with no access yet can still be
constructed, inspected, and tested — it answers every question except "give me
a token", and for that one it names the blocker.

**Rotating** is not **static**. Providers that rotate a refresh token on every
use (TikTok does) cannot be configured by environment variable alone: there is
nowhere to write the new value back. `FileCredentials.persist_rotated` exists
so a rotation is not silently lost, which otherwise fails weeks later with no
deploy to correlate against.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


class CredentialsUnavailable(RuntimeError):
    """Credentials do not exist yet. Distinct from credentials being wrong."""

    def __init__(self, reason: str, *, remedy: str = "") -> None:
        message = reason
        if remedy:
            message += f"\n\nTo resolve: {remedy}"
        super().__init__(message)
        self.reason = reason
        self.remedy = remedy


@dataclass(frozen=True)
class CredentialSpec:
    """What one provider's credentials look like.

    `build` maps a plain dict of resolved fields into whatever dataclass the
    provider's auth layer expects, so this module never imports a provider.
    """

    provider: str
    required: tuple[str, ...]
    env_keys: dict[str, str]
    build: Callable[[dict[str, str]], Any]
    optional: tuple[str, ...] = ()
    rotating_field: str | None = None
    remedy: str = ""

    @property
    def all_fields(self) -> tuple[str, ...]:
        return self.required + self.optional

    def env_key(self, field_name: str) -> str:
        return self.env_keys.get(field_name, field_name.upper())


@dataclass
class CredentialStatus:
    """What a source can report without producing a secret."""

    available: bool
    source_name: str
    detail: str
    present_fields: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    remedy: str = ""


class CredentialSource(ABC):
    """Where credentials come from. Implementations must never log secrets."""

    name: str = "base"

    def __init__(self, spec: CredentialSpec) -> None:
        self.spec = spec

    @abstractmethod
    def status(self) -> CredentialStatus:
        """Describe availability without resolving anything sensitive."""

    @abstractmethod
    def resolve(self) -> Any:
        """Return credentials, or raise `CredentialsUnavailable`."""

    @property
    def available(self) -> bool:
        return self.status().available

    def persist_rotated(self, field_name: str, value: str) -> bool:
        """Write back a rotated secret. False when this source cannot store one.

        The boolean matters: a caller that assumes persistence succeeded will
        happily keep using a source that silently discarded the new value.
        """
        return False

    def __repr__(self) -> str:
        # Deliberately derived from availability only. A repr that interpolated
        # the credential object would leak secrets into every log and traceback.
        return (f"<{type(self).__name__} provider={self.spec.provider} "
                f"name={self.name} available={self.available}>")


def _missing(spec: CredentialSpec, present: list[str]) -> list[str]:
    return [f for f in spec.required if f not in present]


class EnvCredentials(CredentialSource):
    """Read from environment variables. The usual production source."""

    name = "environment"

    def status(self) -> CredentialStatus:
        present = [f for f in self.spec.all_fields
                   if os.environ.get(self.spec.env_key(f))]
        missing = _missing(self.spec, present)
        detail = (
            "All credentials present in the environment." if not missing
            else "Missing environment variable(s): "
                 f"{', '.join(self.spec.env_key(f) for f in missing)}"
        )
        return CredentialStatus(
            available=not missing, source_name=self.name, detail=detail,
            present_fields=present, missing_fields=missing,
            remedy="" if not missing else self.spec.remedy,
        )

    def resolve(self) -> Any:
        st = self.status()
        if not st.available:
            raise CredentialsUnavailable(st.detail, remedy=st.remedy)
        values = {f: os.environ[self.spec.env_key(f)] for f in st.present_fields}
        return self.spec.build(values)


class FileCredentials(CredentialSource):
    """Read from a JSON file. The only source that can absorb a rotation."""

    name = "file"

    def __init__(self, spec: CredentialSpec, path: str | Path) -> None:
        super().__init__(spec)
        self.path = Path(path)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            # Deliberately not treated as absent. Falling back on a malformed
            # file would silently downgrade to no access, and the operator
            # would look configured while doing nothing.
            raise CredentialsUnavailable(
                f"Credential file {self.path} is not valid JSON: {exc}",
                remedy="Fix or delete the file.",
            ) from exc
        return data if isinstance(data, dict) else {}

    def status(self) -> CredentialStatus:
        if not self.path.exists():
            return CredentialStatus(
                available=False, source_name=self.name,
                detail=f"Credential file {self.path} does not exist.",
                missing_fields=list(self.spec.required), remedy=self.spec.remedy,
            )
        data = self._read()
        present = [f for f in self.spec.all_fields if data.get(f)]
        missing = _missing(self.spec, present)
        return CredentialStatus(
            available=not missing, source_name=self.name,
            detail=(f"All credentials present in {self.path}." if not missing
                    else f"{self.path} is missing: {', '.join(missing)}"),
            present_fields=present, missing_fields=missing,
            remedy="" if not missing else self.spec.remedy,
        )

    def resolve(self) -> Any:
        st = self.status()
        if not st.available:
            raise CredentialsUnavailable(st.detail, remedy=st.remedy)
        data = self._read()
        return self.spec.build({f: str(data[f]) for f in st.present_fields})

    def persist_rotated(self, field_name: str, value: str) -> bool:
        data = self._read()
        data[field_name] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            self.path.chmod(0o600)
        except OSError:  # pragma: no cover - platform dependent
            pass
        return True


class StaticCredentials(CredentialSource):
    """In-memory credentials. For tests and one-off scripted runs."""

    name = "static"

    def __init__(self, spec: CredentialSpec, credentials: Any) -> None:
        super().__init__(spec)
        self._credentials = credentials

    def status(self) -> CredentialStatus:
        missing = [f for f in self.spec.required
                   if not getattr(self._credentials, f, "")]
        return CredentialStatus(
            available=not missing, source_name=self.name,
            detail=("In-memory credentials." if not missing
                    else f"Missing: {', '.join(missing)}"),
            present_fields=[f for f in self.spec.all_fields if f not in missing],
            missing_fields=missing,
        )

    def resolve(self) -> Any:
        st = self.status()
        if not st.available:
            raise CredentialsUnavailable(st.detail)
        return self._credentials


class UnavailableCredentials(CredentialSource):
    """Explicitly no credentials, with a reason.

    Not a stub. This is the correct state before an API is provisioned, and
    naming the blocker beats emitting a generic auth error for an expected
    situation.
    """

    name = "unavailable"

    def __init__(self, spec: CredentialSpec, reason: str = "",
                 *, remedy: str | None = None) -> None:
        super().__init__(spec)
        self.reason = reason or f"{spec.provider} API access is not provisioned."
        self.remedy = spec.remedy if remedy is None else remedy

    def status(self) -> CredentialStatus:
        return CredentialStatus(
            available=False, source_name=self.name, detail=self.reason,
            missing_fields=list(self.spec.required), remedy=self.remedy,
        )

    def resolve(self) -> Any:
        raise CredentialsUnavailable(self.reason, remedy=self.remedy)


class ChainedCredentials(CredentialSource):
    """Try sources in order; use the first that is complete.

    The production default is file-then-environment: a file can absorb a
    rotation, so it wins when both are present.
    """

    name = "chain"

    def __init__(self, *sources: CredentialSource) -> None:
        if not sources:
            raise ValueError("ChainedCredentials needs at least one source.")
        super().__init__(sources[0].spec)
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
            )
        statuses = [(s, s.status()) for s in self.sources]
        details = "; ".join(f"{s.name}: {st.detail}" for s, st in statuses)
        remedy = next((st.remedy for _s, st in statuses if st.remedy), "")
        return CredentialStatus(
            available=False, source_name="chain",
            detail=f"No source has complete credentials. Tried — {details}",
            missing_fields=list(self.spec.required), remedy=remedy,
        )

    def resolve(self) -> Any:
        winner = self._first_available()
        if winner is None:
            st = self.status()
            raise CredentialsUnavailable(st.detail, remedy=st.remedy)
        return winner.resolve()

    def persist_rotated(self, field_name: str, value: str) -> bool:
        """Persist into the source currently in use, if it can store one."""
        winner = self._first_available()
        return winner.persist_rotated(field_name, value) if winner else False


def build_default_chain(spec: CredentialSpec, *,
                        credential_file: str | Path | None = None,
                        file_env_var: str = "") -> ChainedCredentials:
    """File (if configured), then environment, then an explicit unavailable."""
    sources: list[CredentialSource] = []
    path = credential_file or (os.environ.get(file_env_var) if file_env_var else None)
    if path:
        sources.append(FileCredentials(spec, path))
    sources.append(EnvCredentials(spec))
    sources.append(UnavailableCredentials(spec))
    return ChainedCredentials(*sources)
