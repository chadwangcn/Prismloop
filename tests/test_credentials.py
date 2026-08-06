"""src/credentials.py 单元测试"""

from __future__ import annotations

import os
import subprocess
from unittest import mock

import pytest

from src.credentials import (
    DEFAULT_AK_SERVICE,
    DEFAULT_SK_SERVICE,
    CredentialError,
    Credentials,
    load_credentials,
    read_keychain,
)


# ============================================================================
# Credentials 数据类
# ============================================================================


class TestCredentials:
    def test_valid(self):
        c = Credentials(ak="AK123", sk="SK456")
        assert c.ak == "AK123"
        assert c.sk == "SK456"

    def test_empty_ak_raises(self):
        with pytest.raises(CredentialError, match="AK 为空"):
            Credentials(ak="", sk="SK")

    def test_empty_sk_raises(self):
        with pytest.raises(CredentialError, match="SK 为空"):
            Credentials(ak="AK", sk="")


# ============================================================================
# read_keychain
# ============================================================================


class TestReadKeychain:
    @mock.patch("src.credentials.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = mock.MagicMock(stdout="AKLT123\n", stderr="")
        result = read_keychain("my-service")
        assert result == "AKLT123"
        mock_run.assert_called_once_with(
            ["security", "find-generic-password", "-s", "my-service", "-w"],
            capture_output=True,
            text=True,
            check=True,
        )

    @mock.patch("src.credentials.subprocess.run")
    def test_strips_whitespace(self, mock_run):
        mock_run.return_value = mock.MagicMock(stdout="  AKLT456  \n", stderr="")
        assert read_keychain("svc") == "AKLT456"

    @mock.patch("src.credentials.subprocess.run")
    def test_empty_value_raises(self, mock_run):
        mock_run.return_value = mock.MagicMock(stdout="\n", stderr="")
        with pytest.raises(CredentialError, match="值为空"):
            read_keychain("svc")

    @mock.patch("src.credentials.subprocess.run")
    def test_called_process_error_raises(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(
            returncode=1, cmd=["security"], stderr="not found"
        )
        with pytest.raises(CredentialError, match="从 Keychain 读取"):
            read_keychain("svc")

    def test_security_command_not_found(self):
        # 模拟 security 命令不存在:用环境变量 PATH 指向空目录
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
            with pytest.raises(CredentialError, match="未找到 `security`"):
                read_keychain("svc")


# ============================================================================
# load_credentials
# ============================================================================


class TestLoadCredentials:
    def test_env_variables_take_priority(self, monkeypatch):
        monkeypatch.setenv("VOLC_AK", "ENV_AK")
        monkeypatch.setenv("VOLC_SK", "ENV_SK")
        c = load_credentials()
        assert c.ak == "ENV_AK"
        assert c.sk == "ENV_SK"

    def test_env_partial_raises(self, monkeypatch):
        monkeypatch.setenv("VOLC_AK", "ENV_AK")
        monkeypatch.delenv("VOLC_SK", raising=False)
        with pytest.raises(CredentialError, match="必须同时设置"):
            load_credentials()

    def test_env_partial_other_direction_raises(self, monkeypatch):
        monkeypatch.delenv("VOLC_AK", raising=False)
        monkeypatch.setenv("VOLC_SK", "ENV_SK")
        with pytest.raises(CredentialError, match="必须同时设置"):
            load_credentials()

    @mock.patch("src.credentials.read_keychain")
    def test_falls_back_to_keychain(self, mock_read, monkeypatch):
        monkeypatch.delenv("VOLC_AK", raising=False)
        monkeypatch.delenv("VOLC_SK", raising=False)

        def side_effect(service):
            return "KC_AK" if "AK" in service else "KC_SK"

        mock_read.side_effect = side_effect
        c = load_credentials()
        assert c.ak == "KC_AK"
        assert c.sk == "KC_SK"

    @mock.patch("src.credentials.read_keychain")
    def test_custom_keychain_service(self, mock_read, monkeypatch):
        monkeypatch.delenv("VOLC_AK", raising=False)
        monkeypatch.delenv("VOLC_SK", raising=False)
        mock_read.return_value = "X"
        load_credentials(
            ak_service="custom-ak", sk_service="custom-sk"
        )
        services_read = [call.args[0] for call in mock_read.call_args_list]
        assert "custom-ak" in services_read
        assert "custom-sk" in services_read

    @mock.patch("src.credentials.read_keychain")
    def test_env_overrides_keychain_service_name(self, mock_read, monkeypatch):
        monkeypatch.delenv("VOLC_AK", raising=False)
        monkeypatch.delenv("VOLC_SK", raising=False)
        monkeypatch.setenv("VOLC_AK_KEYCHAIN_SERVICE", "env-ak-svc")
        monkeypatch.setenv("VOLC_SK_KEYCHAIN_SERVICE", "env-sk-svc")
        mock_read.return_value = "X"
        load_credentials()
        services_read = [call.args[0] for call in mock_read.call_args_list]
        assert "env-ak-svc" in services_read
        assert "env-sk-svc" in services_read

    @mock.patch("src.credentials.read_keychain")
    def test_allow_env_false_disables_env(self, mock_read, monkeypatch):
        # 即使设置了环境变量,allow_env=False 时也应走 Keychain
        monkeypatch.setenv("VOLC_AK", "ENV_AK")
        monkeypatch.setenv("VOLC_SK", "ENV_SK")
        mock_read.return_value = "KC"
        c = load_credentials(allow_env=False)
        assert c.ak == "KC"
        assert c.sk == "KC"

    def test_default_service_names_constant(self):
        assert DEFAULT_AK_SERVICE == "<YOUR_AK_KEYCHAIN_SERVICE>"
        assert DEFAULT_SK_SERVICE == "<YOUR_SK_KEYCHAIN_SERVICE>"
