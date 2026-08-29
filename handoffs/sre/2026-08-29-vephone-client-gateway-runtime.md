# SRE 任务包：vePhone Client Gateway 受限运行时

- 工作包 ID：`SRE-PRISMLOOP-MEDIA-001`
- 目标仓库：`https://github.com/chadwangcn/Prismloop.git`
- 目标环境：14 构建服务器上的 Prismloop Media I/O Harness 运行目录
- Owner：SRE
- 优先级：P0（解除真实云手机外部音视频注入 PoC 的唯一环境阻塞）
- 状态：`ready_for_sre`

## 目标

为私有 `gateway-web` 进程提供可轮换、短期的 vePhone Web SDK 会话凭证，并准备其 Node + Chromium
运行环境。完成后，Harness 可以在不暴露长期 AK/SK、Pod ID、ADB 地址或 SDK token 的前提下，启动
一个连续的外部视频与独立 PCM 音频输入会话。

本任务不涉及 APP 业务、测试判断、TOS、MediaRun 公共 API 或 Harness 源码改动。SRE 不需要上传
素材、安装测试 APK、执行视频 A/B 切换或判定 PoC 成功。

## 已有输入与边界

- 私有 Gateway 位于 `gateway-web/`，默认只监听 `127.0.0.1:8791`。
- Harness 通过现有 Keychain/运行时凭证解析器调用 `GetCallerIdentity` 和 `AssumeRole`；浏览器仅接收
  内存中的短期 token。
- 待配置的非秘密运行时变量：`PRISMLOOP_VEPHONE_STS_ROLE_TRN`。
- 本地/受管 AK/SK、Keychain 与 STS 的完整获取方式见
  `docs/runbooks/本地凭证获取与运行时配置.md`。
- 不得将 AK/SK、STS token、角色值、Pod ID、连接地址或永久签名 URL 写入 Git、`.env` 模板、日志、
  SQLite、媒体证据或 SRE 回执。
- SDK 路径依据官方 [vePhone SDK 示例仓库](https://github.com/volcengine/vePhone)：客户端 SDK 会话在
  私有 Gateway 内建立，Harness 的公开 HTTP API 不接触厂商连接标识。

## SRE 实施范围

### 1. 配置最小 STS 委派

创建或复用一个仅供 Prismloop Media Gateway 使用的可委派角色，并将其 TRN 仅配置在 14 的受管
运行时密钥/环境管理中：

```text
PRISMLOOP_VEPHONE_STS_ROLE_TRN=<scoped-role-trn>
```

要求：

1. 当前 Harness 服务 principal 只能对该指定角色执行 `sts:AssumeRole`。
2. 短期凭证有效期支持至少 15 分钟；Gateway 不得回退使用长期 AK/SK。
3. 角色的云手机/SDK 资源权限采用火山引擎官方最小权限集，并限于本 PoC 的产品/资源范围。SRE
   应以当前租户已启用的 vePhone SDK 所需 Action 为准，不要猜测或扩大到全账号管理权限。
4. 角色信任策略与权限策略不得以明文放进仓库；回执只描述“已限制 principal、Action、资源范围”，
   不包含具体 ARN/TRN、账号、Pod 或地址。

### 2. 准备私有 Gateway 运行依赖

在 14 的受管部署目录中，使用 Node 22+ 安装 `gateway-web/package.json` 锁定的依赖，并安装
Playwright Chromium。依赖必须位于部署目录或受管缓存，不提交 `node_modules`。

```bash
cd <prismloop-deploy-dir>/gateway-web
npm install --ignore-scripts --no-audit --no-fund
npx playwright install chromium
npm run check
```

验证 Chromium 可启动（无 UI 即可）：

```bash
node --input-type=module -e "import { chromium } from 'playwright'; const b=await chromium.launch({headless:true}); await b.close(); console.log('chromium_launch_verified')"
```

Gateway 必须由 SRE 的受管进程机制运行，并只绑定 loopback。不要将 8791 暴露至公网、负载均衡、
VPC 入口或其他机器。

### 3. 验证 STS 前置条件

从仓库根目录、同一个受管运行环境执行：

```bash
python3 scripts/preflight_vephone_client_gateway.py
```

成功的唯一可接受输出为下列结构（布尔值均为 `true`）：

```json
{"account_identity_resolved":true,"status":"ready","temporary_token_complete":true,"token_expiry_present":true}
```

`capability_unavailable` 是明确失败，不可通过“使用长期 AK/SK 启动浏览器”绕过。该脚本只输出
脱敏状态，不会泄露身份或 token。

## 验收标准

SRE 完成此任务须同时满足：

1. 受管运行时存在 `PRISMLOOP_VEPHONE_STS_ROLE_TRN`，但其实际值未进入仓库或回执。
2. 上述 preflight 返回 `status=ready`。
3. `npm run check` 成功，且 Chromium 启动验证输出 `chromium_launch_verified`。
4. Gateway 服务进程仅监听 loopback；无公网暴露规则。
5. 回传下面的机器可读回执，且不包含秘密或基础设施标识。

完成后，Codex/Dev Owner 将负责用真实 APK 执行“视频 A 持续输入 → 无断流切换 B → 屏幕采集”、
独立 PCM 麦克风注入和证据归档。此 SRE 任务本身不代表外部音视频注入能力已经验证。

## SRE 回执格式

将以下模板填充为 JSON，并附带到工作单；不得加入原始命令输出中的敏感字段：

```json
{
  "work_package_id": "SRE-PRISMLOOP-MEDIA-001",
  "status": "ready_for_poc",
  "sts_assume_role_preflight": "ready",
  "temporary_token_complete": true,
  "node_dependency_check": "passed",
  "chromium_launch": "verified",
  "gateway_bind_scope": "loopback_only",
  "credential_storage": "managed_runtime_or_keychain",
  "policy_scope_reviewed": true,
  "secret_or_infrastructure_identifiers_in_receipt": false,
  "completed_at": "<UTC RFC3339>"
}
```

## 可直接粘贴给 SRE 的执行提示词

```text
你是 SRE。请在 Prismloop 的 14 受管运行环境完成工作包 SRE-PRISMLOOP-MEDIA-001。

目标：为私有 gateway-web 进程配置仅能被 Harness principal AssumeRole 的短期 vePhone SDK
会话委派，并准备 Node 22+ 与 Playwright Chromium。代码、MediaRun API、APP、测试素材和 TOS
不在本任务范围内。

仓库：https://github.com/chadwangcn/Prismloop.git
工作包：handoffs/sre/2026-08-29-vephone-client-gateway-runtime.md

必须遵守：
1) 角色 TRN 只进入受管运行时配置 PRISMLOOP_VEPHONE_STS_ROLE_TRN；不得写入 Git、模板、日志
   或回执。浏览器不得使用长期 AK/SK。
2) 将 sts:AssumeRole 限制为当前 Harness principal 到这一指定角色；角色权限按官方 vePhone SDK
   最小权限集合和本 PoC 资源范围配置。不要授予全账号管理权限。
3) 在 gateway-web 执行 npm install --ignore-scripts --no-audit --no-fund、npx playwright install
   chromium、npm run check，并验证 headless Chromium 可启动。
4) Gateway 仅绑定 127.0.0.1:8791，不创建公网暴露。
5) 在同一受管环境运行 python3 scripts/preflight_vephone_client_gateway.py。只有 JSON 中
   status=ready 才能回执完成。

验收：按工作包的 SRE 回执 JSON 返回脱敏结果。不要输出角色值、账号、Pod、端点、AK/SK、STS
token、截图 URL 或原始 provider 错误。
```
