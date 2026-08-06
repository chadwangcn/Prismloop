# Agent 驱动模型

> 文档版本:v1.0
> 创建日期:2026-08-06
> 适用范围:整个测试系统的 Agent 驱动架构

---

## 一、核心原则

### 1.1 全程 Agent 驱动

整个测试系统**不是人操作,而是由 Agent 驱动**。Agent 负责接收任务、调度资源、执行测试、采集证据、生成报告、反馈结果。

### 1.2 Agent 分层模型

```
┌─────────────────────────────────────────────────────┐
│                  管理 Agent(Master)                  │
│  - 接收测试任务(来自管理后台或 CI)                    │
│  - 调度子 Agent                                     │
│  - 汇总结果                                         │
└──────┬──────────────────────────────────────────────┘
       │
   ┌───┴────────────────┬─────────────────┬───────────┐
   │                    │                 │           │
┌──▼────────┐    ┌──────▼──────┐  ┌──────▼──────┐ ┌──▼────────┐
│ 单元功能   │    │ 双端协同     │  │ 开发验证    │ │ 压测 Agent │
│ 验证 Agent │    │ 测试 Agent   │  │ 闭环 Agent  │ │           │
│           │    │             │  │             │ │           │
│ 职责一    │    │ 职责二      │  │ 职责三      │ │ 后续承接  │
└───────────┘    └─────────────┘  └─────────────┘ └───────────┘
```

---

## 二、Agent 职责定义

### 2.1 管理 Agent(Master)

| 职责 | 说明 |
|---|---|
| 任务接收 | 从管理后台/CI/Git webhook 接收测试任务 |
| 任务解析 | 解析任务类型(单元/协同/开发验证/压测) |
| 子 Agent 调度 | 根据任务类型分发给对应子 Agent |
| 资源调度 | 申请 Pod 对、配置环境 |
| 结果汇总 | 聚合所有子 Agent 的结果 |
| 报告生成 | 生成统一报告 |
| 结果反馈 | 通知管理后台/CI/开发者 |

### 2.2 单元功能验证 Agent(职责一)

| 职责 | 说明 |
|---|---|
| 用例选择 | 根据变更的功能(日志/语音/拍照等)选择对应用例 |
| 环境准备 | 安装最新 APK、注入音视频资源 |
| 操作执行 | 驱动云手机模拟操作(点击/说话/拍照) |
| 断言验证 | 验证功能正确性 |
| 证据采集 | 截图/录像/音频分析 |
| 结果反馈 | 返回通过/失败 + 证据 |

### 2.3 双端协同测试 Agent(职责二)

| 职责 | 说明 |
|---|---|
| 序列编排 | 按业务序列编排设备端和 APP 端的操作 |
| 双端调度 | 同时调度两个 Pod(Guardian + K1) |
| 轮询等待 | 纯用户视角轮询 UI,不侵入业务 |
| 协同断言 | 验证双端状态一致性 |
| 证据采集 | 双端截图/录像 |
| 结果反馈 | 返回协同结果 |

### 2.4 APP 开发验证闭环 Agent(职责三)

| 职责 | 说明 |
|---|---|
| 设计稿获取 | 读取 Figma 设计稿 |
| 代码生成 | Agent 基于设计稿开发页面 |
| APK 构建 | 触发构建云主机构建 APK |
| 安装验证 | 安装到云手机,截图/录像 |
| 视觉 diff | 对比实际截图与设计稿偏差 |
| 迭代反馈 | 返回偏差报告,驱动迭代 |

### 2.5 压测 Agent(后续承接)

| 职责 | 说明 |
|---|---|
| 并发调度 | 申请多对 Pod,阶梯加压 |
| 指标采集 | 响应时间/成功率/错误率 |
| SLA 评估 | 对比 SLA 标准 |
| 报告生成 | 压测报告 + Grafana |

---

## 三、Agent 通信协议

### 3.1 任务下发协议

管理 Agent 向子 Agent 下发任务:

```json
{
  "task_id": "task-20260806-001",
  "task_type": "unit_functional",
  "priority": "P0",
  "target_feature": "voice_message",
  "target_pods": {
    "guardian": "pod-guardian-01",
    "k1": "pod-k1-01"
  },
  "build_manifest": {
    "commit_sha": "abc123",
    "manifest_path": "tos://<YOUR_BUILDS_BUCKET>/abc123/build-manifest.json"
  },
  "test_cases": ["GAPP-FUNC-001", "GAPP-FUNC-002"],
  "timeout_sec": 1800,
  "callback_url": "http://master-agent:8080/callback"
}
```

### 3.2 结果回传协议

子 Agent 向管理 Agent 回传结果:

```json
{
  "task_id": "task-20260806-001",
  "status": "completed",
  "result": {
    "passed": true,
    "total_cases": 2,
    "passed_cases": 2,
    "failed_cases": 0,
    "duration_sec": 450
  },
  "evidence": {
    "report_path": "reports/task-20260806-001/report.html",
    "screenshots_dir": "reports/task-20260806-001/screenshots/",
    "recordings_dir": "reports/task-20260806-001/recordings/"
  }
}
```

### 3.3 状态更新协议

子 Agent 执行过程中实时更新状态:

```json
{
  "task_id": "task-20260806-001",
  "event": "step_started",
  "step": 1,
  "pod": "pod-k1-01",
  "action": "按 AI 键唤醒语音助手",
  "timestamp": "2026-08-06T14:00:05Z"
}
```

---

## 四、Agent 执行流程

### 4.1 总体流程

```
1. 管理 Agent 接收任务
   ↓
2. 解析任务类型,选择子 Agent
   ↓
3. 子 Agent 准备环境(申请 Pod、安装 APK、注入资源)
   ↓
4. 子 Agent 执行测试用例
   ├─ MUA 驱动复杂流程
   ├─ SDK 驱动精确事件
   └─ ADB 调试(如需)
   ↓
5. 采集证据(截图/录像/日志)
   ↓
6. 执行断言
   ↓
7. 回传结果给管理 Agent
   ↓
8. 管理 Agent 汇总,生成报告
   ↓
9. 反馈给管理后台/CI/开发者
```

### 4.2 单元功能验证流程(职责一)

```python
class UnitFunctionalAgent:
    """单元功能验证 Agent"""
    
    def execute(self, task):
        # 1. 安装最新 APK
        self.install_apk(task["target_pods"], task["build_manifest"])
        
        # 2. 注入音视频资源(如需)
        if task.get("inject_assets"):
            self.inject_assets(task["target_pods"], task["inject_assets"])
        
        # 3. 执行测试用例
        results = []
        for case_id in task["test_cases"]:
            case = self.load_case(case_id)
            result = self.execute_case(case, task["target_pods"])
            results.append(result)
        
        # 4. 汇总结果
        summary = self.summarize(results)
        
        # 5. 回传
        self.report_to_master(task["task_id"], summary)
```

### 4.3 双端协同流程(职责二)

```python
class DualEndAgent:
    """双端协同测试 Agent"""
    
    def execute(self, task):
        pair = task["target_pods"]
        
        # 1. 按序列编排
        for step in task["sequence"]:
            if step["end"] == "k1":
                # K1 设备端先操作
                self.execute_on_k1(pair["k1"], step)
            elif step["end"] == "guardian":
                # APP 端后操作(查询/设置/配置)
                self.execute_on_guardian(pair["guardian"], step)
            
            # 轮询等待联动
            if step.get("wait_for_linkage"):
                self.poll_linkage(pair, step["wait_for_linkage"])
        
        # 2. 协同断言
        self.assert_state_consistency(pair)
        
        # 3. 采集双端证据
        evidence = self.collect_dual_evidence(pair)
        
        # 4. 回传
        self.report_to_master(task["task_id"], evidence)
```

### 4.4 APP 开发验证闭环流程(职责三)

```python
class DevLoopAgent:
    """APP 开发验证闭环 Agent"""
    
    def execute(self, task):
        # 1. 读取 Figma 设计稿
        design = self.fetch_figma(task["figma_url"])
        
        # 2. Agent 基于设计稿开发页面
        code = self.generate_code(design)
        
        # 3. 触发构建
        build = self.trigger_build(code)
        
        # 4. 安装到云手机
        self.install_apk(task["target_pod"], build)
        
        # 5. 截图/录像
        screenshots = self.capture_screenshots(task["target_pod"], design["screens"])
        recordings = self.capture_recordings(task["target_pod"], design["flows"])
        
        # 6. 视觉 diff
        diffs = self.visual_diff(screenshots, design)
        
        # 7. 迭代反馈
        if diffs.has_significant_deviation():
            return self.feedback_for_iteration(diffs)
        
        return {"status": "passed", "diffs": diffs}
```

---

## 五、Agent 调度策略

### 5.1 任务优先级

| 优先级 | 任务类型 | 说明 |
|---|---|---|
| P0 | 冒烟测试 | 每次构建必跑 |
| P1 | 功能验证 | 功能开发完成后跑 |
| P1 | 开发验证闭环 | 页面开发迭代 |
| P2 | 双端协同 | 集成阶段 |
| P3 | 压测 | 发版前 |

### 5.2 资源调度

```python
class ResourceScheduler:
    """资源调度器"""
    
    def schedule(self, task):
        # 根据任务类型申请 Pod
        if task["task_type"] == "unit_functional":
            pod = self.pod_pool.acquire_single(task["target_end"])
        elif task["task_type"] == "dual_end":
            pair = self.pod_pool.acquire_pair()
        elif task["task_type"] == "pressure":
            pairs = self.pod_pool.acquire_pairs(task["concurrency"])
        
        # 检查资源可用性
        if not self.check_resources_available(task):
            return {"status": "queued", "reason": "insufficient_pods"}
        
        return {"status": "scheduled", "resources": resources}
```

---

## 六、Agent 与 MUA/SDK/ADB 的关系

### 6.1 层次关系

```
Agent(决策层)
  ↓ 调用
MUA OpenAPI(语义层) + SDK(事件层) + ADB(调试层)
  ↓ 控制
云手机 Pod(执行层)
```

### 6.2 选择策略

Agent 根据场景选择调用方式:

| 场景 | Agent 调用 | 理由 |
|---|---|---|
| 复杂流程(登录/绑定) | MUA | 语义级,适应 UI 变更 |
| 精确按键(AI/拍照) | SDK | 事件级,精确控制 |
| 多点触控(缩放/旋转) | SDK | 原生支持多点 |
| 中文输入 | SDK | 支持输入法 |
| 音视频注入 | ADB broadcast | 推送文件 |
| 本地调试 | ADB input | 快速验证 |
| 视觉断言 | MUA | 视觉理解 |

---

## 七、Agent 状态机

### 7.1 状态定义

```
IDLE(空闲)
  ↓ [接收任务]
PREPARING(准备环境)
  ↓ [环境就绪]
EXECUTING(执行中)
  ↓ [步骤完成]
ASSERTING(断言中)
  ↓ [断言完成]
COLLECTING(采集证据)
  ↓ [证据采集完成]
REPORTING(生成报告)
  ↓ [报告完成]
COMPLETED(完成)
  ↓ [返回 IDLE]
IDLE
```

### 7.2 异常状态

```
EXECUTING
  ↓ [超时]
TIMEOUT
  ↓ [重试]
EXECUTING(重试)
  ↓ [重试失败]
FAILED
  ↓ [报告失败]
REPORTING
```

---

## 八、Agent 配置

### 8.1 Agent 注册

```yaml
# config/agents.yaml
agents:
  master:
    name: "Master Agent"
    endpoint: "http://master-agent:8080"
    responsibilities: [task_dispatch, result_aggregation]
  
  unit_functional:
    name: "Unit Functional Agent"
    endpoint: "http://unit-agent:8081"
    responsibilities: [unit_functional_test]
    target_pods: [guardian, k1]
  
  dual_end:
    name: "Dual End Agent"
    endpoint: "http://dual-agent:8082"
    responsibilities: [dual_end_test]
    target_pods: [guardian, k1]
  
  dev_loop:
    name: "Dev Loop Agent"
    endpoint: "http://dev-loop-agent:8083"
    responsibilities: [figma_to_code, visual_diff]
  
  pressure:
    name: "Pressure Agent"
    endpoint: "http://pressure-agent:8084"
    responsibilities: [pressure_test]
```

---

## 九、相关文档

- [00-总体架构设计.md](./00-总体架构设计.md)
- [09-APP开发验证闭环.md](./09-APP开发验证闭环.md)
- [10-管理后台集成规划.md](./10-管理后台集成规划.md)
