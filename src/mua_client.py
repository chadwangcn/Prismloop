"""火山引擎 Mobile Use Agent (MUA) OpenAPI 客户端封装

依据架构文档 `architecture/01-MUA-OpenAPI集成方案.md` 实现:
- V4 签名鉴权(AK/SK)
- RunAgentTaskOneStep 一键运行任务
- GetAgentResult 获取任务结果
- ListAgentRunCurrentStep 步骤轮询
- CancelTask 取消任务
- ListAgentRunTask 查询任务列表
- QPS 限流(令牌桶)
- 重试机制(指数退避)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)


# ============================================================================
# 异常定义
# ============================================================================


class MUAError(Exception):
    """MUA 客户端基础异常"""


class RateLimitError(MUAError):
    """QPS 限流错误"""


class TimeoutError(MUAError):  # noqa: A001 - 与架构文档命名一致
    """任务超时错误"""


class APIError(MUAError):
    """API 调用失败"""

    def __init__(self, action: str, code: str, message: str, request_id: str = ""):
        self.action = action
        self.code = code
        self.message = message
        self.request_id = request_id
        super().__init__(f"[{action}] {code}: {message} (request_id={request_id})")


# ============================================================================
# 令牌桶限流器
# ============================================================================


class RateLimiter:
    """令牌桶限流器(线程安全)

    架构文档 01-第八章:RunAgentTaskOneStep 单用户 QPS 10/s,
    ListAgentRunCurrentStep/GetAgentResult QPS 20/s。
    """

    def __init__(self, qps: int = 10):
        self.qps = qps
        self.interval = 1.0 / qps if qps > 0 else 0
        self._last_time = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """阻塞直到获得令牌"""
        with self._lock:
            now = time.time()
            wait = self.interval - (now - self._last_time)
            if wait > 0:
                time.sleep(wait)
            self._last_time = time.time()


# ============================================================================
# 重试装饰器(指数退避)
# ============================================================================


def retry(max_attempts: int = 3, base_delay: float = 1.0, factor: float = 2.0):
    """指数退避重试装饰器

    架构文档 01-第九章:
    - 网络超时:重试 3 次,指数退避
    - API 限流:等待 1 秒后重试
    """

    def decorator(func):
        def wrapper(*args, **kwargs):
            last_exc: Optional[Exception] = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except RateLimitError as e:
                    last_exc = e
                    if attempt >= max_attempts:
                        break
                    delay = base_delay * (factor ** (attempt - 1))
                    logger.warning(
                        "API 限流,%ds 后重试(%d/%d)", delay, attempt, max_attempts
                    )
                    time.sleep(delay)
                except (TimeoutError, requests.Timeout) as e:
                    last_exc = e
                    if attempt >= max_attempts:
                        break
                    delay = base_delay * (factor ** (attempt - 1))
                    logger.warning(
                        "请求超时,%ds 后重试(%d/%d)", delay, attempt, max_attempts
                    )
                    time.sleep(delay)
                except APIError as e:
                    # 业务错误不重试,直接抛出
                    raise
            assert last_exc is not None
            raise last_exc

        return wrapper

    return decorator


# ============================================================================
# V4 签名实现
# ============================================================================


class V4Signer:
    """火山引擎 V4 签名实现

    依据火山引擎 OpenAPI 鉴权规范:
    1. 构造 CanonicalRequest
    2. 构造 StringToSign
    3. 计算签名(SHA256-HMAC)
    4. 添加 Authorization 头
    """

    ALGORITHM = "HMAC-SHA256"
    SERVICE = "ipaas"

    def __init__(self, ak: str, sk: str, region: str, service: str = SERVICE):
        self.ak = ak
        self.sk = sk
        self.region = region
        self.service = service

    @staticmethod
    def _sha256_hex(data: bytes | str) -> str:
        if isinstance(data, str):
            data = data.encode("utf-8")
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _hmac_sha256(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    def _canonical_uri(self, path: str) -> str:
        return path if path else "/"

    def _canonical_query(self, query: Dict[str, Any]) -> str:
        """构造规范化查询字符串

        火山引擎要求按 key 字典序升序排列,值进行 URL 编码。
        """
        items = []
        for k in sorted(query.keys()):
            v = query[k]
            if isinstance(v, bool):
                v = "true" if v else "false"
            elif isinstance(v, (dict, list)):
                v = json.dumps(v, separators=(",", ":"), ensure_ascii=False)
            items.append((k, str(v)))
        return "&".join(
            f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in items
        )

    def _canonical_headers(self, headers: Dict[str, str]) -> str:
        items = sorted((k.lower(), v.strip()) for k, v in headers.items())
        return "".join(f"{k}:{v}\n" for k, v in items), " ".join(k for k, _ in items)

    def sign(
        self,
        method: str,
        query: Dict[str, Any],
        body: bytes,
        headers: Dict[str, str],
    ) -> Dict[str, str]:
        """对请求进行签名,返回带 Authorization 的完整 headers"""
        date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        short_date = date[:8]

        host = headers["Host"]
        canonical_headers_str, signed_headers = self._canonical_headers(headers)
        payload_hash = self._sha256_hex(body)
        canonical_query = self._canonical_query(query)

        canonical_request = "\n".join(
            [
                method.upper(),
                self._canonical_uri(""),
                canonical_query,
                canonical_headers_str,
                signed_headers,
                payload_hash,
            ]
        )

        credential_scope = f"{short_date}/{self.region}/{self.service}/request"
        string_to_sign = "\n".join(
            [
                self.ALGORITHM,
                date,
                credential_scope,
                self._sha256_hex(canonical_request),
            ]
        )

        # 派生签名密钥
        k_date = self._hmac_sha256(self.sk.encode("utf-8"), short_date)
        k_region = self._hmac_sha256(k_date, self.region)
        k_service = self._hmac_sha256(k_region, self.service)
        k_signing = self._hmac_sha256(k_service, "request")
        signature = hmac.new(
            k_signing, string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        authorization = (
            f"{self.ALGORITHM} Credential={self.ak}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )

        signed_headers = dict(headers)
        signed_headers["Authorization"] = authorization
        return signed_headers


# ============================================================================
# MUA Client
# ============================================================================


class MUAClient:
    """火山引擎 MUA OpenAPI 客户端

    依据架构文档 01-第七章客户端封装设计。
    """

    HOST = "open.volcengineapi.com"
    DEFAULT_TIMEOUT = 120

    def __init__(
        self,
        ak: str,
        sk: str,
        region: str = "cn-north-1",
        service: str = "ipaas",
        version: str = "2023-08-01",
        product_id: Optional[str] = None,
        run_qps: int = 10,
        query_qps: int = 20,
    ):
        self.ak = ak
        self.sk = sk
        self.region = region
        self.service = service
        self.version = version
        self.product_id = product_id
        self.signer = V4Signer(ak, sk, region, service)
        self.run_limiter = RateLimiter(run_qps)
        self.query_limiter = RateLimiter(query_qps)
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    # ----------------------------------------------------------------------
    # 公共 API
    # ----------------------------------------------------------------------

    @retry(max_attempts=3, base_delay=1.0, factor=2.0)
    def run_task_one_step(
        self,
        run_name: str,
        pod_id: str,
        user_prompt: str,
        product_id: Optional[str] = None,
        system_prompt: Optional[str] = None,
        thread_id: Optional[str] = None,
        max_step: int = 100,
        timeout: int = 120,
        retry_limit: int = 3,
        output_schema: Optional[Dict[str, Any] | str] = None,
        tos_bucket: Optional[str] = None,
        tos_endpoint: Optional[str] = None,
        tos_region: Optional[str] = None,
        use_base64_screenshot: bool = True,
        is_screen_record: bool = True,
        gps_info: Optional[str] = None,
        mcp_json: Optional[str] = None,
        max_output_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """RunAgentTaskOneStep:一键运行任务

        架构文档 01-第三章:无需预建配置,推荐使用。
        """
        params: Dict[str, Any] = {
            "Action": "RunAgentTaskOneStep",
            "Version": self.version,
        }
        body: Dict[str, Any] = {
            "RunName": run_name,
            "PodId": pod_id,
            "ProductId": product_id or self.product_id,
            "UserPrompt": user_prompt,
            "MaxStep": max_step,
            "Timeout": timeout,
            "RetryLimit": retry_limit,
            "UseBase64Screenshot": use_base64_screenshot,
            "IsScreenRecord": is_screen_record,
        }
        if system_prompt:
            body["SystemPrompt"] = system_prompt
        if thread_id:
            body["ThreadId"] = thread_id
        if output_schema:
            body["OutputSchema"] = (
                output_schema
                if isinstance(output_schema, str)
                else json.dumps(output_schema, ensure_ascii=False)
            )
        if tos_bucket:
            body["TosBucket"] = tos_bucket
            body["TosEndpoint"] = tos_endpoint
            body["TosRegion"] = tos_region
        if gps_info:
            body["GpsInfo"] = gps_info
        if mcp_json:
            body["McpJson"] = mcp_json
        if max_output_tokens:
            body["MaxOutputTokens"] = max_output_tokens

        self.run_limiter.acquire()
        resp = self._call_api("POST", params, body)
        return resp

    @retry(max_attempts=3, base_delay=1.0, factor=2.0)
    def get_agent_result(self, run_id: str) -> Dict[str, Any]:
        """GetAgentResult:获取任务最终结果

        架构文档 01-第四章:含 IsSuccess/Content/StructOutput/ScreenShots/Usage/RecordingUrl。
        """
        params = {
            "Action": "GetAgentResult",
            "Version": self.version,
            "RunId": run_id,
        }
        self.query_limiter.acquire()
        return self._call_api("GET", params, None)

    @retry(max_attempts=3, base_delay=1.0, factor=2.0)
    def list_agent_run_current_step(self, run_id: str) -> Dict[str, Any]:
        """ListAgentRunCurrentStep:查询当前步骤(轮询用)

        架构文档 01-第五章:轮询直到 Results[-1].Action == "finished"。
        """
        params = {
            "Action": "ListAgentRunCurrentStep",
            "Version": self.version,
            "RunId": run_id,
        }
        self.query_limiter.acquire()
        return self._call_api("GET", params, None)

    def cancel_task(self, run_id: str) -> Dict[str, Any]:
        """CancelTask:取消任务"""
        params = {
            "Action": "CancelTask",
            "Version": self.version,
            "RunId": run_id,
        }
        self.query_limiter.acquire()
        return self._call_api("POST", params, {"RunId": run_id})

    def list_agent_run_task(self, filters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """ListAgentRunTask:查询任务列表"""
        params = {
            "Action": "ListAgentRunTask",
            "Version": self.version,
        }
        if filters:
            params.update(filters)
        self.query_limiter.acquire()
        return self._call_api("GET", params, None)

    # ----------------------------------------------------------------------
    # 高级接口
    # ----------------------------------------------------------------------

    def execute_test_task(
        self,
        pod_id: str,
        user_prompt: str,
        run_name: Optional[str] = None,
        system_prompt: Optional[str] = None,
        output_schema: Optional[Dict[str, Any]] = None,
        tos_config: Optional[Dict[str, str]] = None,
        max_step: int = 100,
        timeout: int = 120,
        retry_limit: int = 3,
        poll_interval: float = 2.0,
    ) -> Dict[str, Any]:
        """执行完整测试任务:提交→轮询→获取结果

        架构文档 01-第 7.2 节封装的完整流程。
        """
        run_name = run_name or f"test-{int(time.time())}"
        tos_config = tos_config or {}

        logger.info("提交 MUA 任务: %s (pod=%s)", run_name, pod_id)
        run_resp = self.run_task_one_step(
            run_name=run_name,
            pod_id=pod_id,
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            output_schema=output_schema,
            tos_bucket=tos_config.get("bucket"),
            tos_endpoint=tos_config.get("endpoint"),
            tos_region=tos_config.get("region"),
            max_step=max_step,
            timeout=timeout,
            retry_limit=retry_limit,
        )
        run_id = run_resp.get("Result", {}).get("RunId")
        if not run_id:
            raise APIError(
                "RunAgentTaskOneStep",
                "NO_RUN_ID",
                f"响应缺少 RunId: {run_resp}",
            )

        logger.info("任务已提交, run_id=%s, 开始轮询", run_id)
        self.poll_task_steps(run_id, timeout_sec=timeout, interval_sec=poll_interval)

        logger.info("任务完成, 获取最终结果 run_id=%s", run_id)
        result = self.get_agent_result(run_id)
        return self._normalize_result(run_id, result)

    def poll_task_steps(
        self,
        run_id: str,
        timeout_sec: int = 120,
        interval_sec: float = 2.0,
    ) -> Dict[str, Any]:
        """轮询任务步骤直到完成或超时

        架构文档 01-第 5.2 节轮询策略。
        """
        start = time.time()
        last_action = ""
        while time.time() - start < timeout_sec:
            resp = self.list_agent_run_current_step(run_id)
            results = resp.get("Result", {}).get("Results", [])
            if results:
                last = results[-1]
                action = last.get("Action", "")
                if action != last_action:
                    logger.info(
                        "任务 %s 步骤变更: %s -> %s",
                        run_id,
                        last_action,
                        action,
                    )
                    last_action = action
                if action == "finished":
                    return resp
            time.sleep(interval_sec)
        raise TimeoutError(
            f"任务 {run_id} 轮询超时({timeout_sec}s), 最后状态: {last_action}"
        )

    # ----------------------------------------------------------------------
    # 内部实现
    # ----------------------------------------------------------------------

    def _call_api(
        self,
        method: str,
        query: Dict[str, Any],
        body: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """统一 API 调用入口(含 V4 签名)"""
        url = f"https://{self.HOST}/"

        body_bytes = b""
        if body is not None:
            body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")

        headers = {
            "Host": self.HOST,
            "Content-Type": "application/json",
        }
        if body_bytes:
            headers["Content-Length"] = str(len(body_bytes))

        signed_headers = self.signer.sign(method, query, body_bytes, headers)

        try:
            resp = self.session.request(
                method=method,
                url=url,
                params=query,
                data=body_bytes if body_bytes else None,
                headers=signed_headers,
                timeout=self.DEFAULT_TIMEOUT,
            )
        except requests.Timeout as e:
            raise TimeoutError(f"API 请求超时: {e}") from e
        except requests.RequestException as e:
            raise APIError(query.get("Action", "?"), "NETWORK_ERROR", str(e)) from e

        if resp.status_code == 429:
            raise RateLimitError(f"QPS 限流: {resp.text}")

        try:
            data = resp.json()
        except ValueError as e:
            raise APIError(
                query.get("Action", "?"),
                "INVALID_RESPONSE",
                f"响应非 JSON: {resp.text[:200]}",
            ) from e

        if resp.status_code >= 400:
            err = data.get("ResponseMetadata", {}).get("Error", {})
            raise APIError(
                query.get("Action", "?"),
                err.get("Code", str(resp.status_code)),
                err.get("Message", resp.text),
                data.get("ResponseMetadata", {}).get("RequestId", ""),
            )

        if "ResponseMetadata" in data and "Error" in data.get("ResponseMetadata", {}):
            err = data["ResponseMetadata"]["Error"]
            raise APIError(
                query.get("Action", "?"),
                err.get("Code", "UNKNOWN"),
                err.get("Message", ""),
                data.get("ResponseMetadata", {}).get("RequestId", ""),
            )

        return data

    @staticmethod
    def _normalize_result(run_id: str, raw: Dict[str, Any]) -> Dict[str, Any]:
        """规范化最终结果,便于上层断言"""
        result = raw.get("Result", {})
        return {
            "run_id": run_id,
            "is_success": result.get("IsSuccess") == 1,
            "content": result.get("Content"),
            "struct_output": result.get("StructOutput"),
            "screenshots": result.get("ScreenShots", {}),
            "recording_url": result.get("RecordingUrl"),
            "files": result.get("Files", []),
            "usage": result.get("Usage", {}),
        }


# ============================================================================
# 通用 OutputSchema 定义
# ============================================================================


# 架构文档 01-第 6.2 节:通用测试结果 Schema
COMMON_TEST_RESULT_SCHEMA: Dict[str, Any] = {
    "Type": "dict",
    "Fields": [
        {
            "Name": "test_results",
            "Type": "list",
            "Description": "测试步骤结果列表",
            "Required": True,
            "Fields": [
                {
                    "Type": "dict",
                    "Fields": [
                        {"Name": "step", "Type": "string", "Required": True},
                        {"Name": "expected", "Type": "string", "Required": True},
                        {"Name": "actual", "Type": "string", "Required": True},
                        {"Name": "passed", "Type": "boolean", "Required": True},
                        {"Name": "screenshot_id", "Type": "string", "Required": False},
                    ],
                }
            ],
        },
        {"Name": "summary", "Type": "string", "Required": False},
        {"Name": "passed", "Type": "boolean", "Required": True},
        {"Name": "failure_reason", "Type": "string", "Required": False},
    ],
}


# 架构文档 01-第 6.3 节:绑定协同场景 Schema
BINDING_SCHEMA: Dict[str, Any] = {
    "Type": "dict",
    "Fields": [
        {"Name": "binding_success", "Type": "boolean", "Required": True},
        {"Name": "device_sn", "Type": "string", "Required": True},
        {"Name": "family_id", "Type": "string", "Required": True},
        {"Name": "k1_shows_binding", "Type": "boolean", "Required": True},
        {"Name": "guardian_shows_k1", "Type": "boolean", "Required": True},
        {"Name": "screenshot_ids", "Type": "list", "Required": True},
    ],
}
