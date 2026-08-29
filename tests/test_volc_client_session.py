"""Gateway STS bootstrap never needs caller-supplied credentials."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.volc_client_session import ClientSessionError, VolcClientSessionBroker


class Request:
    def __init__(self, **values):
        self.values = values


class FakeSTS:
    def __init__(self):
        self.assume_request = None

    def get_caller_identity(self, request):
        return SimpleNamespace(account_id="account-1")

    def assume_role(self, request):
        self.assume_request = request
        return SimpleNamespace(credentials=SimpleNamespace(
            access_key_id="temporary-ak", secret_access_key="temporary-sk", session_token="session-token",
            current_time="2026-08-29T00:00:00Z", expired_time="2026-08-29T01:00:00Z",
        ))


def test_broker_discovers_identity_and_mints_only_short_lived_gateway_token():
    api = FakeSTS()
    bootstrap = VolcClientSessionBroker(api, caller_identity_request=Request, assume_role_request=Request).issue(
        role_trn="trn:iam::account:role/prismloop", role_session_name="media-gateway", duration_seconds=3600,
    )

    assert bootstrap.account_id == "account-1"
    assert bootstrap.token.web_sdk_value()["SessionToken"] == "session-token"
    assert api.assume_request.values["role_session_name"] == "media-gateway"


def test_broker_refuses_to_fallback_to_long_lived_credentials_when_role_is_missing():
    with pytest.raises(ClientSessionError, match="role_trn_not_configured"):
        VolcClientSessionBroker(FakeSTS(), caller_identity_request=Request, assume_role_request=Request).issue(
            role_trn="", role_session_name="media-gateway",
        )
