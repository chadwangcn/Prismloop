"""MUA Client 单元测试(不依赖真实 API)"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from src.mua_client import (
    APIError,
    MUAClient,
    RateLimiter,
    RateLimitError,
    V4Signer,
    COMMON_TEST_RESULT_SCHEMA,
    BINDING_SCHEMA,
)


# ============================================================================
# RateLimiter
# ============================================================================


def test_rate_limiter_basic_interval():
    import time

    limiter = RateLimiter(qps=10)  # interval=0.1s
    start = time.time()
    limiter.acquire()
    limiter.acquire()
    elapsed = time.time() - start
    # 第二次 acquire 应至少等 0.1s
    assert elapsed >= 0.09


def test_rate_limiter_zero_qps():
    limiter = RateLimiter(qps=0)
    # 不应该阻塞
    limiter.acquire()


# ============================================================================
# V4 签名
# ============================================================================


class TestV4Signer:
    def test_sign_returns_authorization(self):
        signer = V4Signer(ak="AKTEST", sk="SKTEST", region="cn-north-1")
        headers = {"Host": "open.volcengineapi.com", "Content-Type": "application/json"}
        body = b'{"key": "value"}'
        query = {"Action": "Test", "Version": "2023-08-01"}

        signed = signer.sign("POST", query, body, headers)

        assert "Authorization" in signed
        assert "HMAC-SHA256" in signed["Authorization"]
        assert "AKTEST" in signed["Authorization"]

    def test_canonical_query_orders_keys(self):
        signer = V4Signer("ak", "sk", "cn-north-1")
        query = {"B": "2", "A": "1", "C": "3"}
        result = signer._canonical_query(query)
        # 按字母序
        assert result == "A=1&B=2&C=3"

    def test_canonical_query_url_encodes(self):
        signer = V4Signer("ak", "sk", "cn-north-1")
        query = {"Action": "Run Task", "Value": "a&b"}
        result = signer._canonical_query(query)
        assert "Run%20Task" in result
        assert "a%26b" in result

    def test_canonical_query_handles_bool(self):
        signer = V4Signer("ak", "sk", "cn-north-1")
        query = {"Flag": True, "Num": False}
        result = signer._canonical_query(query)
        assert "Flag=true" in result
        assert "Num=false" in result

    def test_canonical_headers_lowercases(self):
        signer = V4Signer("ak", "sk", "cn-north-1")
        headers = {"Host": "x.com", "Content-Type": "application/json"}
        ch, sh = signer._canonical_headers(headers)
        assert "host:x.com" in ch
        assert "content-type:application/json" in ch
        assert "host" in sh
        assert "content-type" in sh


# ============================================================================
# MUAClient 单元测试(使用 mock)
# ============================================================================


@pytest.fixture
def mua_client():
    return MUAClient(
        ak="AKTEST",
        sk="SKTEST",
        region="cn-north-1",
        product_id="prod-test",
        run_qps=1000,  # 测试中不限速
        query_qps=1000,
    )


class TestMUAClient:
    def test_run_task_one_step_builds_params(self, mua_client):
        """验证请求体构造"""
        expected_body = {
            "RunName": "test-run",
            "PodId": "pod-1",
            "ProductId": "prod-test",
            "UserPrompt": "测试任务",
            "MaxStep": 50,
            "Timeout": 60,
            "RetryLimit": 3,
            "UseBase64Screenshot": True,
            "IsScreenRecord": True,
        }
        with patch.object(mua_client, "_call_api") as mock_call:
            mock_call.return_value = {"Result": {"RunId": "run-123"}}
            mua_client.run_task_one_step(
                run_name="test-run",
                pod_id="pod-1",
                user_prompt="测试任务",
                max_step=50,
                timeout=60,
            )
            args, kwargs = mock_call.call_args
            method = args[0]
            query = args[1]
            body = args[2]

            assert method == "POST"
            assert query["Action"] == "RunAgentTaskOneStep"
            assert query["Version"] == "2023-08-01"
            for k, v in expected_body.items():
                assert body[k] == v

    def test_run_task_one_step_with_tos(self, mua_client):
        with patch.object(mua_client, "_call_api") as mock_call:
            mock_call.return_value = {"Result": {"RunId": "run-1"}}
            mua_client.run_task_one_step(
                run_name="t",
                pod_id="p",
                user_prompt="up",
                tos_bucket="bucket",
                tos_endpoint="endpoint",
                tos_region="region",
            )
            body = mock_call.call_args[0][2]
            assert body["TosBucket"] == "bucket"
            assert body["TosEndpoint"] == "endpoint"
            assert body["TosRegion"] == "region"

    def test_run_task_one_step_with_output_schema_dict(self, mua_client):
        with patch.object(mua_client, "_call_api") as mock_call:
            mock_call.return_value = {"Result": {"RunId": "run-1"}}
            schema = {"Type": "dict", "Fields": [{"Name": "passed", "Type": "boolean"}]}
            mua_client.run_task_one_step(
                run_name="t", pod_id="p", user_prompt="up", output_schema=schema
            )
            body = mock_call.call_args[0][2]
            assert isinstance(body["OutputSchema"], str)
            parsed = json.loads(body["OutputSchema"])
            assert parsed["Type"] == "dict"

    def test_get_agent_result_calls_query(self, mua_client):
        with patch.object(mua_client, "_call_api") as mock_call:
            mock_call.return_value = {"Result": {"IsSuccess": 1}}
            mua_client.get_agent_result("run-1")
            args = mock_call.call_args[0]
            assert args[0] == "GET"
            assert args[1]["Action"] == "GetAgentResult"
            assert args[1]["RunId"] == "run-1"

    def test_list_agent_run_current_step(self, mua_client):
        with patch.object(mua_client, "_call_api") as mock_call:
            mock_call.return_value = {"Result": {"Results": []}}
            mua_client.list_agent_run_current_step("run-1")
            args = mock_call.call_args[0]
            assert args[1]["Action"] == "ListAgentRunCurrentStep"

    def test_cancel_task(self, mua_client):
        with patch.object(mua_client, "_call_api") as mock_call:
            mock_call.return_value = {"Result": {}}
            mua_client.cancel_task("run-1")
            assert mock_call.called

    def test_poll_task_steps_returns_when_finished(self, mua_client):
        with patch.object(mua_client, "list_agent_run_current_step") as mock_step:
            mock_step.return_value = {
                "Result": {"Results": [{"Action": "finished"}]}
            }
            result = mua_client.poll_task_steps("run-1", timeout_sec=5, interval_sec=0.1)
            assert result["Result"]["Results"][-1]["Action"] == "finished"

    def test_poll_task_steps_times_out(self, mua_client):
        with patch.object(mua_client, "list_agent_run_current_step") as mock_step:
            mock_step.return_value = {
                "Result": {"Results": [{"Action": "running"}]}
            }
            with pytest.raises(Exception) as exc:
                mua_client.poll_task_steps("run-1", timeout_sec=1, interval_sec=0.1)
            assert "超时" in str(exc.value) or "timeout" in str(exc.value).lower()

    def test_execute_test_task_full_flow(self, mua_client):
        """完整流程:提交 → 轮询 → 获取结果"""
        with patch.object(mua_client, "run_task_one_step") as mock_run, \
             patch.object(mua_client, "poll_task_steps") as mock_poll, \
             patch.object(mua_client, "get_agent_result") as mock_get:
            mock_run.return_value = {"Result": {"RunId": "run-1"}}
            mock_poll.return_value = {}
            mock_get.return_value = {
                "Result": {
                    "IsSuccess": 1,
                    "Content": "测试通过",
                    "StructOutput": {"passed": True},
                    "ScreenShots": {"s1": {"screenshot": "https://x.png"}},
                    "Usage": {"in_tokens": 100, "out_tokens": 10},
                    "RecordingUrl": "https://rec.mp4",
                }
            }

            result = mua_client.execute_test_task(
                pod_id="pod-1",
                user_prompt="测试任务",
                output_schema=COMMON_TEST_RESULT_SCHEMA,
            )

            assert result["run_id"] == "run-1"
            assert result["is_success"] is True
            assert result["struct_output"]["passed"] is True
            assert "s1" in result["screenshots"]
            assert result["recording_url"] == "https://rec.mp4"
            assert result["usage"]["in_tokens"] == 100

    def test_execute_test_task_no_run_id_raises(self, mua_client):
        with patch.object(mua_client, "run_task_one_step") as mock_run:
            mock_run.return_value = {"Result": {}}  # 无 RunId
            with pytest.raises(APIError):
                mua_client.execute_test_task(
                    pod_id="pod-1",
                    user_prompt="测试",
                )


# ============================================================================
# Schema 常量
# ============================================================================


def test_common_schema_has_required_fields():
    fields = {f["Name"] for f in COMMON_TEST_RESULT_SCHEMA["Fields"]}
    assert "test_results" in fields
    assert "passed" in fields
    assert "failure_reason" in fields


def test_binding_schema_has_required_fields():
    fields = {f["Name"] for f in BINDING_SCHEMA["Fields"]}
    assert "binding_success" in fields
    assert "device_sn" in fields
    assert "family_id" in fields
    assert "k1_shows_binding" in fields
    assert "guardian_shows_k1" in fields
