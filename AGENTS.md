# Prismloop Repository Instructions

本仓提供 APP 测试执行 Harness 和版本化 APP Case。进入本仓的 Agent 必须先读取本文件，再按 Task 的 `request_class` 决定允许的工作范围。

APP 测试员工的岗位定义、Skill、Workflow 和 Eval 由 `chadwangcn/OpenAgent` 管理。Paperclip 管理任务、责任人、运行状态和证据索引。本文件只保存稳定的仓库边界、输入规则与验证入口，不保存任何当前被测项目的信息。

## 运行输入

### Paperclip Task

Task 提供本次执行的动态信息，包括适用项：

- 测试目标、范围、成功标准、禁止范围和 `request_class`；
- Figma 或其他设计基线的精确版本与节点；
- Case repository、commit、Case/suite ID；
- APK artifact reference、SHA-256、包名和版本；
- 目标设备/云手机能力、系统版本、环境引用和执行预算；
- 证据要求、输出位置、下一责任人和具名验收人；
- 是否允许维护 Case、修改 Prismloop、创建 Issue 或执行其他外部写操作。

### Paperclip Agent 环境

Agent 环境提供跨任务稳定的运行能力，例如 Prismloop 服务入口、设备资源、Artifact Store、Figma/GitHub 访问和测试凭证的 Secret binding。具体变量名由 OpenAgent Agent Definition 和 Skill 声明，本文件不复制项目变量清单或具体值。

非秘密输入以 Task 为本次执行优先值，Agent 环境提供默认值。Secret 值只能由运行时 Secret provider 注入，Task 和 Git 只能保存 Secret Reference 名称。

缺少当前 gate 的必填输入时返回 `BlockerNotice`。不得从历史任务、浮动分支、个人配置、已有设备状态或仓库中的示例值推断当前项目。

## 禁止写入本文件的信息

- 当前 APP、业务系统、仓库、Figma 文件或 Case 清单；
- 当前 APK、版本、下载地址、设备、Pod、ADB 地址或云手机租约；
- 服务 URL、路由、端口、当前部署和健康状态；
- 账号、Token、设备密钥、云平台凭证或临时签名 URL；
- 某次运行的结果、缺陷、截图、录像、日志或临时工作区。

稳定的 Prismloop 工具协议进入 schema、代码或工具文档；跨员工规范进入 OpenAgent Skill；单次项目事实进入 Task 或 Agent 环境。

## 请求类型与目录边界

### `test_execution`

- 默认只读仓库并调用 Task 授权的测试环境。
- 允许生成 `TestRunReceipt`、`FindingNotice`、`BlockerNotice` 和外部 Artifact 引用。
- 不修改 APP、工具、Case、设计、部署或路由。

### `test_case_maintenance`

- 只修改 `cases/`、Task 明确列出的 fixture/baseline 引用和 Case 文档。
- 不修改 Harness 实现，不削弱断言以适配失败产品。

### `test_tool_improvement`

- 只修改 Task 明确列出的工具路径，例如 `src/`、`schemas/`、`media-injector/`、`media-probe/`、`gateway-web/` 或工具测试。
- 不顺便修改业务 Case、被测 APP 或正式设计/合同。

### Agent、Skill 或 Workflow 迭代

在 `chadwangcn/OpenAgent` 完成，不在本仓复制岗位定义。

除 Task 明确授权的一次性迁移外，同一 PR 不得同时修改工具实现与业务 Case。

## 仓库约束

- 修改前读取最新文件并检查 Git 状态；使用干净、独立的分支或 worktree。
- 只在当前仓库和 Task 工作区内操作，不读取其他项目的本地目录和个人配置。
- `cases/index.json` 是正式 Case 的唯一发现入口；未登记资产不能用于正式验收。
- `reports/`、`logs/`、`tmp/` 和原始运行证据不进入 Git。
- 不提交 Secret、真实用户数据、Pod/ADB 地址、签名 URL、原始截图、录像、音频或日志。
- Prismloop run `completed` 只表示工具序列结束；Case 结论由 APP 测试员工逐条判断。
- 对真实设备或云环境的安装、输入注入、录制和外部写操作必须在 Task 授权范围内。

## 证据与验证

原始截图、录像、UI Tree、音视频和日志写入 Task 指定的 Artifact Store。Paperclip 只保存脱敏摘要、artifact reference、SHA-256、APK/Case pin 和结论。

Case 变更至少运行：

```bash
python3 scripts/validate_cases.py
git diff --check
```

工具变更还必须运行与修改范围匹配的 `pytest`、Android 构建或服务协议验证。没有 Task 提供的目标环境与授权时，只运行本地、合成或离线验证，不连接已知的历史服务。

交付时报告实际命令结果、修改文件、commit/tree、未验证项和下一 gate。实施者自测只能进入 `in_review`，不能自行给出最终验收。
