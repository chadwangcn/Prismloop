# Prismloop Repository Instructions

本仓库提供 APP 测试所依赖的 Prismloop 执行 Harness 与版本化 APP Case。进入本仓的 Agent 必须先确认任务属于哪条迭代线。

## 目录与所有权

- `cases/`：APP 测试 Case、索引、schema、脱敏 fixture 与设计基线引用，由 APP 测试工程师维护。
- `src/`、`schemas/`、`media-injector/`、`media-probe/`、`gateway-web/`：Prismloop 工具实现，由工具改进任务维护。
- `tests/`：工具、Case schema 和集成验证。
- `evidence/`：证据与回执 schema、模板；不保存真实运行产物。
- `reports/`、`logs/`、`tmp/`：运行产物，不进入 Git。

## 三条迭代线

1. `test_case_maintenance`：默认只修改 `cases/` 与 Case 文档。
2. `test_tool_improvement`：修改 Harness 实现、协议、部署或工具测试，不得顺便降低 Case 断言。
3. Agent/Skill 迭代：在 `chadwangcn/OpenAgent` 中完成，不在本仓复制岗位提示词。

除明确授权的引导迁移外，一个 PR 不得同时修改工具实现与业务 Case。

## APP 测试输入与判定

- Case 必须固定 Figma URL、版本、node ID、Case ID 和所需证据。
- 每次执行必须从任务获得 Case commit、APK artifact reference、APK SHA-256、包名与环境引用。
- Prismloop `completed` 只表示工具序列完成；Case 是否通过由 APP 测试 Agent 按断言判断。
- Paperclip 中的 APP 测试 Agent 使用 `$prismloop-app-testing`，Case 设计使用 `$lumi-test-case-governance`。

## 安全与证据

真实 Token、云平台 AK/SK、Pod/ADB 地址、设备秘密、用户数据、签名 URL、原始日志、截图和录像不得提交。Case 只保存 Secret Reference 名称和不可变 `artifact_ref` + SHA-256。运行证据进入 Artifact Store，Paperclip 只保存脱敏回执和引用。

## 验证

- 工具变更：运行与改动范围匹配的 `pytest`、Android 构建或服务协议验证。
- Case 变更：运行 `python3 scripts/validate_cases.py`，校验索引、Case schema、引用存在性和秘密扫描。
- 提交前检查 `git diff --check` 与 `git status --short`，不得带入运行产物。
