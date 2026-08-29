# APP 开发验证闭环

> 文档版本:v1.1
> 创建日期:2026-08-06
> 更新日期:2026-08-29(开发 Agent 改为消费 Harness 服务的偏差输出)
> 适用范围:职责三 - 基于 Figma 设计稿的 APP 页面开发验证与迭代
>
> **v1.1 说明**：本文件不属于 Media I/O Harness 服务范围，仅可作为未来某个上层调用方
> 的独立设计参考。Harness 不接收 Dev Agent、Figma、APK、偏差或 commit 概念；它只提供
> 图片、视频、音频的输入注入、输出采集和可选识别。正式服务接口见
> [11-Harness媒体输入输出服务.md](./11-Harness媒体输入输出服务.md)。

---

## 一、核心目标

### 1.1 职责定义

提供 Figma 设计稿后,Agent 基于设计稿开发单个页面,截图/录像对比实际效果与设计交互功能的偏差,驱动迭代开发。

### 1.2 闭环流程

```
Figma 设计稿
  ↓
Agent 开发单个页面(基于 AGENTS.md 规范)
  ↓
APK 构建(云服务器)
  ↓
云手机安装
  ↓
截图/录像(按设计稿的 screen/state 采集)
  ↓
视觉 diff(对比实际与设计)
  ↓
偏差分析(交互/布局/动效)
  ↓
迭代反馈
  ↓
(回到开发,修复偏差)
```

---

## 二、阶段划分

### 2.1 阶段一:设计稿解析

| 步骤 | 说明 |
|---|---|
| 获取 Figma URL | 从管理后台或任务参数获取 |
| 解析设计合约 | screen/route/token/copy/state 五要素 |
| 提取视觉基线 | 每个 screen/state 的设计图作为基线 |
| 识别交互流程 | flow edges(state 之间的跳转) |

### 2.2 阶段二:Agent 代码生成

| 步骤 | 说明 |
|---|---|
| 读取 AGENTS.md | 遵循项目的 Android 开发规范 |
| 生成 Compose 代码 | 基于 Figma 设计稿生成 Jetpack Compose 代码 |
| 应用 design tokens | 颜色/字体/间距/动效 |
| 实现路由 | 按 route 配置实现导航 |
| 实现状态机 | 按 state 定义实现状态切换 |

### 2.3 阶段三:构建与安装

| 步骤 | 说明 |
|---|---|
| 触发构建 | 通知构建云主机构建 APK |
| 等待 webhook | 接收构建完成通知 |
| 下载 APK | 从 TOS 拉取 APK |
| 安装到云手机 | MUA 驱动或 ADB install |

### 2.4 阶段四:截图/录像采集

| 步骤 | 说明 |
|---|---|
| 冷启动 APP | MUA 驱动启动到 entry screen |
| 遍历 screen | 按设计稿的 screen 列表逐个截图 |
| 触发 state | 按 state 定义触发每个状态,截图 |
| 录制 flow | 按 flow edges 录制交互过程 |
| 上传 TOS | 截图/录像上传到 TOS |

### 2.5 阶段五:视觉 diff

| 步骤 | 说明 |
|---|---|
| 加载基线 | 从 Figma 设计稿提取的视觉基线 |
| 逐屏对比 | 实际截图 vs 设计稿 |
| 逐 state 对比 | 每个 state 的视觉差异 |
| 动效对比 | 录像 vs 设计的交互流程 |
| 生成 diff 报告 | 偏差列表 + 视觉对比图 |

### 2.6 阶段六:迭代反馈

| 步骤 | 说明 |
|---|---|
| 偏差分类 | 布局/颜色/字体/动效/交互 |
| 严重度评估 | Critical/Major/Minor |
| 反馈给开发 Agent | 驱动代码修改 |
| 重新构建 | 修复后重新走闭环 |

---

## 三、设计稿解析规范

### 3.1 设计合约五要素

```yaml
# Figma 设计合约示例
figma_url: "https://figma.com/file/xxx/Lumi-App"
design_version: "v1.2.0"

screens:
  - id: entry
    name: "入口页"
    route: "/entry"
    baseline_image: "figma://entry-baseline.png"
  
  - id: growth-home
    name: "成长首页"
    route: "/growth-home"
    baseline_image: "figma://growth-home-baseline.png"
    states:
      - id: loading
        name: "加载中"
        trigger: "进入页面"
      - id: loaded
        name: "加载完成"
        trigger: "数据返回"
      - id: empty
        name: "空状态"
        trigger: "无数据"
      - id: error
        name: "错误状态"
        trigger: "请求失败"

flows:
  - id: login-flow
    edges:
      - from: entry
        to: login
        trigger: "点击登录"
      - from: login
        to: growth-home
        trigger: "登录成功"

tokens:
  colors:
    primary: "#FF6B35"
    background: "#FFFFFF"
  typography:
    h1: { size: 24, weight: 700 }
    body: { size: 16, weight: 400 }
```

### 3.2 视觉基线提取

```python
class FigmaParser:
    """Figma 设计稿解析器"""
    
    def parse(self, figma_url):
        # 1. 获取 Figma 数据
        figma_data = self.figma_client.get_file(figma_url)
        
        # 2. 提取 screens
        screens = self._extract_screens(figma_data)
        
        # 3. 提取 states
        for screen in screens:
            screen["states"] = self._extract_states(figma_data, screen)
        
        # 4. 提取 flows
        flows = self._extract_flows(figma_data)
        
        # 5. 提取 tokens
        tokens = self._extract_tokens(figma_data)
        
        # 6. 下载基线图
        for screen in screens:
            screen["baseline_path"] = self._download_baseline(
                screen["baseline_image"]
            )
        
        return {
            "screens": screens,
            "flows": flows,
            "tokens": tokens
        }
```

---

## 四、截图/录像采集

### 4.1 采集策略

| 采集对象 | 方式 | 说明 |
|---|---|---|
| 单个 screen | MUA 驱动导航 + 截图 | 遍历所有 screen |
| 单个 state | MUA 驱动触发 + 截图 | 遍历每个 screen 的所有 state |
| flow 交互 | SDK 录屏 | 录制 state 之间的跳转 |
| 动效 | SDK 高频截图 | 捕获过渡动画 |

### 4.2 采集实现

```python
class ScreenshotCollector:
    """截图采集器"""
    
    def collect_all(self, pod, design_contract):
        """按设计合约采集所有截图"""
        screenshots = {}
        
        for screen in design_contract["screens"]:
            # 1. 导航到 screen
            self.mua.navigate_to(pod, screen["route"])
            
            # 2. 截图 screen 默认状态
            screenshots[screen["id"]] = {
                "default": self.sdk.screenShot(pod)
            }
            
            # 3. 遍历 states
            for state in screen.get("states", []):
                self.mua.trigger_state(pod, state["trigger"])
                time.sleep(1)  # 等待状态稳定
                screenshots[screen["id"]][state["id"]] = \
                    self.sdk.screenShot(pod)
        
        return screenshots
    
    def record_flows(self, pod, design_contract):
        """录制所有 flow"""
        recordings = {}
        
        for flow in design_contract["flows"]:
            self.sdk.startRecording(pod)
            
            for edge in flow["edges"]:
                self.mua.trigger_edge(pod, edge["trigger"])
                time.sleep(2)  # 等待跳转
            
            recordings[flow["id"]] = self.sdk.stopRecording(pod)
        
        return recordings
```

### 4.3 采集 OutputSchema

```json
{
  "Type": "dict",
  "Fields": [
    {
      "Name": "screens",
      "Type": "list",
      "Fields": [{
        "Type": "dict",
        "Fields": [
          { "Name": "screen_id", "Type": "string" },
          { "Name": "state_id", "Type": "string" },
          { "Name": "screenshot_id", "Type": "string" },
          { "Name": "reached", "Type": "boolean" }
        ]
      }]
    },
    { "Name": "flows_recorded", "Type": "list" }
  ]
}
```

---

## 五、视觉 diff

### 5.1 diff 类型

| 类型 | 说明 | 严重度 |
|---|---|---|
| 布局偏差 | 元素位置/尺寸不一致 | Major |
| 颜色偏差 | 颜色值不一致 | Minor |
| 字体偏差 | 字体大小/粗细不一致 | Minor |
| 缺失元素 | 设计有但实际没有 | Critical |
| 多余元素 | 设计没有但实际有 | Major |
| 动效偏差 | 过渡动画不一致 | Minor |
| 交互偏差 | flow 跳转不正确 | Critical |

### 5.2 diff 实现

```python
class VisualDiffer:
    """视觉 diff 工具"""
    
    def diff(self, actual_screenshots, design_baseline):
        """对比实际截图与设计基线"""
        diffs = []
        
        for screen_id, states in actual_screenshots.items():
            baseline = design_baseline["screens"][screen_id]
            
            for state_id, actual_img in states.items():
                expected_img = baseline["states"].get(state_id)
                
                if not expected_img:
                    diffs.append({
                        "screen_id": screen_id,
                        "state_id": state_id,
                        "type": "missing_state",
                        "severity": "Major",
                        "description": f"状态 {state_id} 在设计中不存在"
                    })
                    continue
                
                # 像素级 diff
                pixel_diff = self._pixel_diff(actual_img, expected_img)
                
                if pixel_diff["deviation_pct"] > 5:  # 偏差 > 5%
                    diffs.append({
                        "screen_id": screen_id,
                        "state_id": state_id,
                        "type": "layout",
                        "severity": "Major",
                        "deviation_pct": pixel_diff["deviation_pct"],
                        "diff_image": pixel_diff["diff_image"]
                    })
        
        return diffs
```

### 5.3 diff 报告

```json
{
  "design_version": "v1.2.0",
  "actual_version": "commit-abc123",
  "total_screens": 5,
  "total_states": 20,
  "diffs": [
    {
      "screen_id": "growth-home",
      "state_id": "loaded",
      "type": "layout",
      "severity": "Major",
      "deviation_pct": 8.5,
      "description": "列表项间距与设计不一致",
      "diff_image": "diffs/growth-home-loaded-diff.png"
    },
    {
      "screen_id": "entry",
      "state_id": "default",
      "type": "missing_element",
      "severity": "Critical",
      "description": "设计中的'登录'按钮缺失"
    }
  ],
  "summary": {
    "critical": 1,
    "major": 1,
    "minor": 0,
    "passed": false
  }
}
```

---

## 六、迭代反馈机制

### 6.1 偏差优先级

| 严重度 | 处理 | 是否阻塞 |
|---|---|---|
| Critical | 必须修复 | 是 |
| Major | 应该修复 | 否(但记录) |
| Minor | 可选修复 | 否 |

### 6.2 反馈给开发 Agent

```python
class IterationFeedback:
    """迭代反馈"""
    
    def generate_feedback(self, diffs):
        """生成迭代反馈"""
        critical_diffs = [d for d in diffs if d["severity"] == "Critical"]
        major_diffs = [d for d in diffs if d["severity"] == "Major"]
        
        feedback = {
            "passed": len(critical_diffs) == 0,
            "critical_issues": critical_diffs,
            "major_issues": major_diffs,
            "recommendation": "修复 Critical 后重新构建" if critical_diffs 
                             else "可接受,记录 Major 后继续"
        }
        
        return feedback
```

### 6.3 迭代闭环

```
第 1 轮:开发 → 构建 → 截图 → diff → 发现 Critical
  ↓ 反馈
第 2 轮:修复 → 重新构建 → 重新截图 → diff → Critical 修复,剩 Major
  ↓ 反馈
第 3 轮:优化 → 重新构建 → 重新截图 → diff → 全部通过
  ↓
闭环完成
```

---

## 七、与已有 D0 项目的对接

### 7.1 继承 D0 经验

| D0 经验 | 复用方式 |
|---|---|
| Figma 设计合约 + 校验器 | 本方案读取设计合约作为基线 |
| visual-report.json | 本方案自动填充 |
| .local-acceptance/visual/ 目录 | 本方案使用相同结构 |
| 13 Case 外部进程 harness | 保留,harness 验 API,本方案验 UI |

### 7.2 目录结构对接

```
.local-acceptance/visual/
├── <design-version>/
│   ├── baseline/              # 从 Figma 提取的基线
│   │   ├── guardian/
│   │   └── k1/
│   ├── actual/                # 实际截图
│   │   ├── guardian/
│   │   └── k1/
│   ├── diffs/                 # diff 结果
│   └── visual-report.json     # diff 报告
```

---

## 八、双端开发验证

### 8.1 双端设计稿

| 端 | 设计稿 | 基线 |
|---|---|---|
| Guardian APP | Figma Guardian 设计稿 | 720×1280 |
| K1 设备端 | Figma K1 设计稿 | 640×480 |

### 8.2 双端独立 diff

```python
def dual_dev_loop(guardian_design, k1_design):
    """双端开发验证闭环"""
    # 1. 双端独立开发验证
    guardian_result = dev_loop_agent.execute({
        "figma_url": guardian_design,
        "target_pod": guardian_pod,
        "resolution": "720x1280"
    })
    
    k1_result = dev_loop_agent.execute({
        "figma_url": k1_design,
        "target_pod": k1_pod,
        "resolution": "640x480"
    })
    
    # 2. 双端都通过才算通过
    return {
        "guardian_passed": guardian_result["passed"],
        "k1_passed": k1_result["passed"],
        "overall_passed": guardian_result["passed"] and k1_result["passed"]
    }
```

---

## 九、相关文档

- [00-总体架构设计.md](./00-总体架构设计.md)
- [03-APK构建流水线.md](./03-APK构建流水线.md)
- [06-证据链与报告规范.md](./06-证据链与报告规范.md)
- [08-Agent驱动模型.md](./08-Agent驱动模型.md)
