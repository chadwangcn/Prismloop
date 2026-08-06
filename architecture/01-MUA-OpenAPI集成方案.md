# MUA OpenAPI 集成方案

> 文档版本:v1.0
> 创建日期:2026-08-06
> 适用范围:火山引擎 Mobile Use Agent (MUA) OpenAPI 集成

---

## 一、服务接入

### 1.1 服务信息

| 项 | 值 |
|---|---|
| 服务地址 | `open.volcengineapi.com` |
| 服务名 | `ipaas` |
| 区域 | `cn-north-1` |
| API 版本 | `2023-08-01` |
| 鉴权方式 |火山引擎 AK/SK 签名(V4) |

### 1.2 前置条件

1. 火山引擎账号注册并实名认证
2. 获取 AK/SK(API 访问密钥)
3. 跨服务访问请求为账号授权 ServiceName=ipaas
4. 创建 Mobile Use Agent 业务(ProductId)
5. 订购云手机实例(PodId)
6. 创建 TOS 存储桶(截图/录屏/产物)
7. 云手机开通 TOS 访问权限

---

## 二、OpenAPI 接口清单

### 2.1 配置管理接口(QPS 50/s,单用户 10/s)

| API | 方法 | 用途 |
|---|---|---|
| `CreateAgentRunConfig` | POST | 创建运行配置(TOS 桶、回调) |
| `UpdateAgentRunConfig` | POST | 更新配置 |
| `ListAgentRunConfig` | GET | 查询配置列表 |
| `DeleteAgentRunConfig` | GET | 删除配置 |

### 2.2 任务管理接口

| API | 方法 | 单用户 QPS | 用途 |
|---|---|---|---|
| `RunAgentTaskOneStep` | POST | 10/s | **一键运行**(无需预建配置,推荐) |
| `RunAgentTask` | POST | 10/s | 按配置运行 |
| `CancelTask` | POST | - | 取消任务 |
| `ListAgentRunTask` | GET | - | 查询任务列表 |
| `ListAgentRunCurrentStep` | GET | 20/s | 查询当前步骤(轮询) |
| `GetAgentResult` | GET | 20/s | **获取最终结果**(含截图/录屏/用量) |

---

## 三、RunAgentTaskOneStep 接口规范

### 3.1 请求

```
POST /?Action=RunAgentTaskOneStep&Version=2023-08-01
Host: open.volcengineapi.com
Service: ipaas
Region: cn-north-1
```

### 3.2 请求参数

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `RunName` | string | 是 | 任务运行名,1-127 字符 |
| `PodId` | string | 是 | 云手机实例 ID |
| `ProductId` | string | 是 | 业务 ID |
| `UserPrompt` | string | 是 | 自然语言指令,≤10000 字符 |
| `SystemPrompt` | string | 否 | 系统提示词 |
| `ThreadId` | string | 否 | 会话 ID,维持上下文 |
| `MaxStep` | int | 否 | 1-500,默认 100 |
| `Timeout` | int | 否 | 1-86400 秒,默认 120 |
| `RetryLimit` | int | 否 | 1-10,失败重试 |
| `OutputSchema` | string | 否 | 结构化输出 schema(JSON) |
| `CallbackInfo` | object | 否 | 回调配置(本方案不使用) |
| `UseBase64Screenshot` | bool | 否 | Base64 传输截图 |
| `IsScreenRecord` | bool | 否 | 开启录屏(需 TOS 配置) |
| `TosBucket` | string | 否 | 截图/录屏持久化桶 |
| `TosEndpoint` | string | 否 | TOS endpoint |
| `TosRegion` | string | 否 | TOS 区域 |
| `McpJson` | string | 否 | 第三方 MCP 工具 |
| `GpsInfo` | string | 否 | GPS 注入(经度,纬度,海拔,精度,速度,方向) |
| `MaxOutputTokens` | int | 否 | 模型输出 token 限制 |

### 3.3 请求示例

```json
{
  "RunName": "guardian-bind-k1-001",
  "PodId": "pod-guardian-01",
  "ProductId": "lumi-app-test",
  "SystemPrompt": "你是 Guardian APP 的 QA 工程师。设备:云手机 AOSP 11,720x1280。APP 包名:com.lumi.guardian。规则:每步操作前截图,每步断言,不得输入真实账号。",
  "UserPrompt": "在 Guardian APP 中扫描 K1 设备二维码完成绑定,验证绑定列表包含 K1 设备。",
  "MaxStep": 50,
  "Timeout": 120,
  "RetryLimit": 3,
  "OutputSchema": "{\"Type\":\"dict\",\"Fields\":[{\"Name\":\"steps\",\"Type\":\"list\",\"Fields\":[{\"Type\":\"dict\",\"Fields\":[{\"Name\":\"step\",\"Type\":\"string\"},{\"Name\":\"passed\",\"Type\":\"boolean\"},{\"Name\":\"screenshot_id\",\"Type\":\"string\"}]}]},{\"Name\":\"passed\",\"Type\":\"boolean\"},{\"Name\":\"failure_reason\",\"Type\":\"string\"}]}",
  "UseBase64Screenshot": true,
  "IsScreenRecord": true,
  "TosBucket": "<YOUR_EVIDENCE_BUCKET>",
  "TosEndpoint": "tos-s3-cn-beijing.volces.com",
  "TosRegion": "cn-beijing"
}
```

---

## 四、GetAgentResult 结果规范

### 4.1 响应结构

```json
{
  "IsSuccess": 1,
  "Content": "任务执行完成内容",
  "StructOutput": { /* 按 OutputSchema 返回的 JSON */ },
  "ScreenShots": {
    "<uuid>": {
      "id": "...",
      "original_dimensions": [720, 1280],
      "original_screenshot": "https://xxx.tos-cn-beijing.volces.com/...",
      "screenshot": "https://xxx.tos-cn-beijing.volces.com/...",
      "screenshot_dimensions": [720, 1280]
    }
  },
  "Usage": { "in_tokens": 9183, "out_tokens": 459 },
  "RecordingUrl": "https://cn-beijing.volces.com/xxxxxx",
  "Files": ["/sdk_files/<run_id>/1.txt"]
}
```

### 4.2 关键字段说明

| 字段 | 用途 |
|---|---|
| `IsSuccess` | 任务整体成功(1)/失败(0) |
| `Content` | 文本结果 |
| `StructOutput` | 结构化输出(按 OutputSchema 填充) |
| `ScreenShots` | 截图集合(含 URL、尺寸) |
| `Usage` | token 用量(成本统计) |
| `RecordingUrl` | 录屏视频 URL |
| `Files` | 附属文件路径 |

---

## 五、ListAgentRunCurrentStep 步骤轮询

### 5.1 响应结构

```json
{
  "RunId": "...",
  "ThreadId": "...",
  "Results": [{
    "Action": "finished",
    "Param": { "content": "..." },
    "StepResult": {
      "IsSuccess": true,
      "Result": "步骤结果描述"
    },
    "Timestamp": "2026-08-06T13:17:12+08:00"
  }]
}
```

### 5.2 轮询策略

```python
def poll_task_steps(run_id, timeout_sec=120, interval_sec=2):
    """轮询任务步骤直到完成或超时"""
    start = time.time()
    while time.time() - start < timeout_sec:
        result = mua.list_agent_run_current_step(run_id)
        if result["Results"][-1]["Action"] == "finished":
            return result
        time.sleep(interval_sec)
    raise TimeoutError(f"任务 {run_id} 超时")
```

---

## 六、OutputSchema 结构化输出

### 6.1 设计原则

OutputSchema 定义 MUA 返回的 JSON 结构,用于断言。本方案定义统一的测试结果 schema。

### 6.2 通用测试结果 Schema

```json
{
  "Type": "dict",
  "Fields": [
    {
      "Name": "test_results",
      "Type": "list",
      "Description": "测试步骤结果列表",
      "Required": true,
      "Fields": [{
        "Type": "dict",
        "Fields": [
          { "Name": "step", "Type": "string", "Required": true },
          { "Name": "expected", "Type": "string", "Required": true },
          { "Name": "actual", "Type": "string", "Required": true },
          { "Name": "passed", "Type": "boolean", "Required": true },
          { "Name": "screenshot_id", "Type": "string", "Required": false }
        ]
      }]
    },
    { "Name": "summary", "Type": "string", "Required": false },
    { "Name": "passed", "Type": "boolean", "Required": true },
    { "Name": "failure_reason", "Type": "string", "Required": false }
  ]
}
```

### 6.3 特定场景 Schema 示例(绑定协同)

```json
{
  "Type": "dict",
  "Fields": [
    { "Name": "binding_success", "Type": "boolean", "Required": true },
    { "Name": "device_sn", "Type": "string", "Required": true },
    { "Name": "family_id", "Type": "string", "Required": true },
    { "Name": "k1_shows_binding", "Type": "boolean", "Required": true },
    { "Name": "guardian_shows_k1", "Type": "boolean", "Required": true },
    { "Name": "screenshot_ids", "Type": "list", "Required": true }
  ]
}
```

---

## 七、客户端封装设计

### 7.1 MUA Client 类设计

```python
class MUAClient:
    """火山引擎 MUA OpenAPI 客户端"""
    
    def __init__(self, ak, sk, region="cn-north-1"):
        self.ak = ak
        self.sk = sk
        self.region = region
        self.service = "ipaas"
        self.version = "2023-08-01"
    
    def run_task_one_step(self, run_name, pod_id, product_id, 
                          user_prompt, system_prompt=None,
                          max_step=100, timeout=120, retry_limit=3,
                          output_schema=None, tos_bucket=None,
                          tos_endpoint=None, tos_region=None,
                          use_base64_screenshot=True, 
                          is_screen_record=True):
        """一键运行任务"""
        params = {
            "RunName": run_name,
            "PodId": pod_id,
            "ProductId": product_id,
            "UserPrompt": user_prompt,
            "MaxStep": max_step,
            "Timeout": timeout,
            "RetryLimit": retry_limit,
            "UseBase64Screenshot": use_base64_screenshot,
            "IsScreenRecord": is_screen_record
        }
        if system_prompt:
            params["SystemPrompt"] = system_prompt
        if output_schema:
            params["OutputSchema"] = json.dumps(output_schema)
        if tos_bucket:
            params["TosBucket"] = tos_bucket
            params["TosEndpoint"] = tos_endpoint
            params["TosRegion"] = tos_region
        return self._call_api("RunAgentTaskOneStep", params)
    
    def get_agent_result(self, run_id):
        """获取任务结果"""
        return self._call_api("GetAgentResult", {"RunId": run_id})
    
    def list_agent_run_current_step(self, run_id):
        """查询当前步骤"""
        return self._call_api("ListAgentRunCurrentStep", {"RunId": run_id})
    
    def cancel_task(self, run_id):
        """取消任务"""
        return self._call_api("CancelTask", {"RunId": run_id})
    
    def list_agent_run_task(self, filters=None):
        """查询任务列表"""
        return self._call_api("ListAgentRunTask", filters or {})
    
    def _call_api(self, action, params):
        """调用 API(含 V4 签名)"""
        # 基于 volcenginesdkcore 实现签名
        pass
```

### 7.2 任务执行流程封装

```python
def execute_test_task(mua_client, pod_id, product_id, 
                      user_prompt, system_prompt, output_schema,
                      tos_config, timeout=120):
    """执行测试任务并返回结构化结果"""
    # 1. 提交任务
    run = mua_client.run_task_one_step(
        run_name=f"test-{int(time.time())}",
        pod_id=pod_id,
        product_id=product_id,
        user_prompt=user_prompt,
        system_prompt=system_prompt,
        output_schema=output_schema,
        tos_bucket=tos_config["bucket"],
        tos_endpoint=tos_config["endpoint"],
        tos_region=tos_config["region"],
        timeout=timeout
    )
    run_id = run["Result"]["RunId"]
    
    # 2. 轮询步骤
    poll_task_steps(run_id, timeout_sec=timeout)
    
    # 3. 获取结果
    result = mua_client.get_agent_result(run_id)
    
    return {
        "run_id": run_id,
        "is_success": result["Result"]["IsSuccess"] == 1,
        "struct_output": result["Result"].get("StructOutput"),
        "screenshots": result["Result"].get("ScreenShots", {}),
        "recording_url": result["Result"].get("RecordingUrl"),
        "usage": result["Result"].get("Usage", {}),
        "content": result["Result"].get("Content")
    }
```

---

## 八、QPS 与并发策略

### 8.1 QPS 限制

| 接口 | 单用户 QPS |
|---|---|
| `RunAgentTaskOneStep` | 10/s |
| `RunAgentTask` | 10/s |
| `ListAgentRunCurrentStep` | 20/s |
| `GetAgentResult` | 20/s |

### 8.2 压测并发策略

由于单用户 QPS 限制 10/s,压测高并发场景需:

1. **申请多个子账号**:每个子账号独立 AK/SK,QPS 独立计算
2. **阶梯加压**:10 → 30 → 50 → 100 并发,每档稳定 5 分钟
3. **任务排队**:超过 QPS 限制时,本地排队等待

```python
class RateLimiter:
    """令牌桶限流器"""
    def __init__(self, qps=10):
        self.qps = qps
        self.interval = 1.0 / qps
        self.last_time = 0
        self.lock = threading.Lock()
    
    def acquire(self):
        with self.lock:
            now = time.time()
            wait = self.interval - (now - self.last_time)
            if wait > 0:
                time.sleep(wait)
            self.last_time = time.time()
```

---

## 九、错误处理与重试

### 9.1 错误分类

| 错误类型 | 处理策略 |
|---|---|
| 网络超时 | 重试 3 次,指数退避 |
| API 限流(QPS 超限) | 等待 1 秒后重试 |
| 任务执行失败 | 检查 IsSuccess,重跑用例 1 次 |
| MUA 视觉判断失败 | 人工审核截图 |
| TOS 链接过期 | 重新触发截图下载 |

### 9.2 重试机制

```python
@retry(max_attempts=3, backoff=ExponentialBackoff(base=1, factor=2))
def call_mua_api(func, *args, **kwargs):
    """带重试的 API 调用"""
    try:
        return func(*args, **kwargs)
    except RateLimitError:
        time.sleep(1)
        raise
    except TimeoutError:
        raise
```

---

## 十、配置文件规范

### 10.1 env.template.json

```json
{
  "mua": {
    "ak": "<YOUR_AK>",
    "sk": "<YOUR_SK>",
    "region": "cn-north-1",
    "service": "ipaas",
    "version": "2023-08-01",
    "product_id": "<YOUR_PRODUCT_ID>"
  },
  "pods": {
    "guardian": {
      "pod_id": "<GUARDIAN_POD_ID>",
      "resolution": "720x1280",
      "aosp_version": 11
    },
    "k1": {
      "pod_id": "<K1_POD_ID>",
      "resolution": "640x480",
      "aosp_version": 10
    }
  },
  "tos": {
    "bucket": "<YOUR_EVIDENCE_BUCKET>",
    "endpoint": "tos-s3-cn-beijing.volces.com",
    "region": "cn-beijing"
  }
}
```

### 10.2 配置加载

```python
def load_config(env_path="config/env.local.json"):
    """加载环境配置"""
    with open(env_path) as f:
        config = json.load(f)
    
    # 验证必填项
    required = ["mua.ak", "mua.sk", "mua.product_id", 
                "pods.guardian.pod_id", "pods.k1.pod_id",
                "tos.bucket"]
    for key in required:
        if not get_nested(config, key):
            raise ValueError(f"缺少必填配置: {key}")
    
    return config
```

---

## 十一、相关文档

- [00-总体架构设计.md](./00-总体架构设计.md)
- [02-云手机与外设模拟.md](./02-云手机与外设模拟.md)
- [04-测试用例矩阵规范.md](./04-测试用例矩阵规范.md)
