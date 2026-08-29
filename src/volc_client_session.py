"""Short-lived Volc client-SDK session credentials for the private gateway.

The MediaRun API never accepts an account id, a Pod id, AK/SK, or an STS
token.  This module is only used by a deployment bootstrap: it discovers the
account identity with the service principal and assumes one explicitly
configured, scoped role.  The resulting token must be passed directly to the
private client-SDK gateway process and never persisted in SQLite or evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class ClientSessionError(RuntimeError):
    """The private client-SDK session cannot be safely created."""


class STSApi(Protocol):
    def get_caller_identity(self, request: Any) -> Any: ...

    def assume_role(self, request: Any) -> Any: ...


@dataclass(frozen=True)
class ClientSessionToken:
    """The exact token shape required by the official Web SDK start call."""

    access_key_id: str
    secret_access_key: str
    session_token: str
    current_time: str
    expired_time: str

    def web_sdk_value(self) -> dict[str, str]:
        return {
            "AccessKeyID": self.access_key_id,
            "SecretAccessKey": self.secret_access_key,
            "SessionToken": self.session_token,
            "CurrentTime": self.current_time,
            "ExpiredTime": self.expired_time,
        }


@dataclass(frozen=True)
class ClientSessionBootstrap:
    """In-memory data passed only to the private client gateway."""

    account_id: str
    token: ClientSessionToken


class VolcClientSessionBroker:
    """Mints a short-lived token through the official STS API."""

    def __init__(self, api: STSApi, *, caller_identity_request: Any, assume_role_request: type[Any]) -> None:
        self._api = api
        self._caller_identity_request = caller_identity_request
        self._assume_role_request = assume_role_request

    def issue(self, *, role_trn: str, role_session_name: str, duration_seconds: int = 3_600) -> ClientSessionBootstrap:
        if not role_trn.strip():
            raise ClientSessionError("client_session_role_trn_not_configured")
        if not role_session_name.strip():
            raise ClientSessionError("client_session_role_session_name_required")
        if not 900 <= duration_seconds <= 43_200:
            raise ClientSessionError("client_session_duration_seconds_out_of_range")

        identity = self._api.get_caller_identity(self._caller_identity_request())
        account_id = str(getattr(identity, "account_id", "") or "")
        if not account_id:
            raise ClientSessionError("caller_identity_missing_account_id")

        response = self._api.assume_role(
            self._assume_role_request(
                role_trn=role_trn,
                role_session_name=role_session_name,
                duration_seconds=duration_seconds,
            )
        )
        credentials = getattr(response, "credentials", None)
        token = ClientSessionToken(
            access_key_id=str(getattr(credentials, "access_key_id", "") or ""),
            secret_access_key=str(getattr(credentials, "secret_access_key", "") or ""),
            session_token=str(getattr(credentials, "session_token", "") or ""),
            current_time=str(getattr(credentials, "current_time", "") or ""),
            expired_time=str(getattr(credentials, "expired_time", "") or ""),
        )
        if not all((token.access_key_id, token.secret_access_key, token.session_token, token.current_time, token.expired_time)):
            raise ClientSessionError("assume_role_returned_incomplete_token")
        return ClientSessionBootstrap(account_id=account_id, token=token)
