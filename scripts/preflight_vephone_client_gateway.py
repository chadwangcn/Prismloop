#!/usr/bin/env python3
"""Safely verify the STS prerequisite for the private vePhone gateway.

This command intentionally emits only a small, non-secret readiness receipt.
It never writes short-lived credentials to disk and never prints account, Pod,
endpoint, access-key, secret-key, or session-token values.

Run from the repository root after SRE has configured
``PRISMLOOP_VEPHONE_STS_ROLE_TRN`` in the managed runtime environment.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src.config import load_config, load_credentials_from_config  # noqa: E402
from src.volc_client_session import ClientSessionError, VolcClientSessionBroker  # noqa: E402


def _receipt(status: str, **values: Any) -> None:
    print(json.dumps({"status": status, **values}, ensure_ascii=False, sort_keys=True))


def main() -> int:
    role_trn = os.environ.get("PRISMLOOP_VEPHONE_STS_ROLE_TRN", "").strip()
    if not role_trn:
        _receipt("capability_unavailable", reason="client_session_role_trn_not_configured")
        return 2

    try:
        import volcenginesdkcore as core
        import volcenginesdksts as sts

        config = load_config()
        credentials = load_credentials_from_config(config)
        sdk_config = core.Configuration()
        sdk_config.ak = credentials.ak
        sdk_config.sk = credentials.sk
        sdk_config.region = config.region
        api = sts.STSApi(core.ApiClient(sdk_config))
        broker = VolcClientSessionBroker(
            api,
            caller_identity_request=sts.GetCallerIdentityRequest,
            assume_role_request=sts.AssumeRoleRequest,
        )
        bootstrap = broker.issue(
            role_trn=role_trn,
            role_session_name=f"prismloop-media-preflight-{uuid.uuid4().hex[:12]}",
            duration_seconds=900,
        )
    except ClientSessionError as exc:
        _receipt("capability_unavailable", reason=str(exc))
        return 2
    except Exception:
        # Provider errors can carry internal addresses or request metadata.  The
        # SRE receipt must stay safe to share with the development team.
        _receipt("capability_unavailable", reason="sts_assume_role_failed")
        return 2

    # Accessing only the presence/format fields proves that a complete temporary
    # credential was returned without disclosing any of its values.
    _receipt(
        "ready",
        account_identity_resolved=bool(bootstrap.account_id),
        temporary_token_complete=all(bootstrap.token.web_sdk_value().values()),
        token_expiry_present=bool(bootstrap.token.expired_time),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
