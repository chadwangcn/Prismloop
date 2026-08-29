"""Harness process configuration has no credential-bearing values."""

from __future__ import annotations

import pytest

from src.media_config import MediaServiceSettings


def test_media_settings_read_declared_non_secret_environment(monkeypatch):
    monkeypatch.setenv("PRISMLOOP_MEDIA_LISTEN_HOST", "127.0.0.1")
    monkeypatch.setenv("PRISMLOOP_MEDIA_LISTEN_PORT", "18999")
    monkeypatch.setenv("PRISMLOOP_STATE_DB_PATH", "tmp/test.sqlite")
    monkeypatch.setenv("PRISMLOOP_ARTIFACT_STORE_REF", "tos/test-media")
    monkeypatch.setenv("PRISMLOOP_DEVICE_PROVIDER_PROFILE", "volc/pod-raw-frame")

    settings = MediaServiceSettings.from_env()

    assert settings.listen_port == 18999
    assert settings.device_provider_profile == "volc/pod-raw-frame"


def test_media_settings_reject_invalid_port(monkeypatch):
    monkeypatch.setenv("PRISMLOOP_MEDIA_LISTEN_PORT", "70000")

    with pytest.raises(ValueError, match="LISTEN_PORT"):
        MediaServiceSettings.from_env()
