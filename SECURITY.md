# 安全说明 / Security Notes

## 中文

### 不要提交这些内容

请不要把以下内容提交到公开仓库、Issue、讨论区或截图中：

- `.env`
- Steam Web API Key
- Neo4j 用户名和密码
- Neo4j 数据库 dump、backup、`.db`、SQLite 文件
- 导出的真实 CSV/JSON 图谱数据
- 包含个人备注、好友路径、SteamID、头像或昵称的截图
- 任何 Cookie、登录态、密码、访问令牌或浏览器会话信息

`.env` 已在 `.gitignore` 中忽略，但如果你手动复制密钥到 README、Issue、截图或其他文件，Git 仍然可能记录这些内容。

当前版本推荐通过网页端“安全配置”保存 Steam API Key 和 Neo4j 密码。它们会写入系统凭据库，例如 Windows Credential Manager，而不是写入 `.env`。旧版 `.env` 中的 `STEAM_API_KEY` 和 `NEO4J_PASSWORD` 仍可兼容读取，但建议迁移。

网页端“系统日志 / Dev Logs”会自动脱敏 Steam API Key、Neo4j 密码、Cookie、Authorization、`password=`、`key=` 等内容，用于本地排错。但日志中仍可能出现 SteamID、昵称、路径、备注分类或错误上下文。复制日志、提交 Issue 或分享截图前，请再手动检查并删除可识别个人信息。

### 如果密钥或数据泄露

如果你不小心公开了敏感信息：

1. 立即撤销或重置 Steam Web API Key。
2. 修改 Neo4j Desktop 数据库密码。
3. 删除公开的文件、截图、Issue 或发布包。
4. 检查 Git 历史，必要时使用历史清理工具处理已提交的密钥。
5. 如果泄露了他人的可识别关系数据或备注，尽快删除并通知相关人员。

### 报告安全问题

如果你发现安全问题：

- 不要在公开 Issue 中粘贴真实密钥、密码、Cookie、数据库 dump 或可识别个人数据。
- 可以用脱敏示例描述问题。
- 如果仓库启用了 GitHub Security Advisory，请优先使用私密安全报告。

### 使用边界

本项目只面向公开 Steam Web API 数据，不支持也不鼓励读取 Cookie、绕过隐私设置、抓取私密好友列表或收集无授权数据。

## English

Do not commit `.env`, Steam Web API keys, Neo4j passwords, database dumps, exported real graph data, screenshots with private notes, cookies, session tokens, or credentials.

If a secret is leaked, revoke or rotate it immediately, remove the public content, inspect Git history, and avoid posting raw secrets or identifiable personal data in public issues.

---

## 安全审计报告 / Security Audit Report

> 审计日期 / Audit date: 2026-06-16
> 审计分支 / Audited branch: `dev-base`
> 目标分支 / Target branch: `security-check-before-main`

### 已修复 / Resolved

| 严重度 | 问题 | 修复方式 |
|--------|------|---------|
| **HIGH** | Neo4j Cypher 查询深度值通过 f-string 拼接 | 提取 `_safe_depth()` 静态方法统一校验+钳制，所有深度值强制为安全整数后内插 |
| **MEDIUM** | `.env` 写入未过滤换行符，可注入环境变量 | `patch_settings` 写入前移除 `\n` `\r` |
| **LOW** | CSRF 中间件注释不够清晰 | 补充文档说明：空 Origin 头（同源请求）正常放行，仅拦截跨域写操作 |

### 已确认安全 / Confirmed Safe

| 检查项 | 结论 |
|--------|------|
| API Key / 密码存储 | 使用 OS 原生凭据库（Windows Credential Manager / macOS Keychain）加密存储 |
| 日志脱敏 | `AppLogBuffer.redact()` 对 API Key、密码、32 位十六进制令牌、Cookie、Authorization 头进行正则替换 |
| 输入校验 | Pydantic 模型对所有参数做类型+范围校验；`validate_crawl_payload` 和 `validateGraphFilters` 做前后双重校验 |
| 端点可访问的资源 | `/static` 仅暴露前端静态文件；`FileResponse` 只返回 index.html |
| CSRF 保护 | 所有 POST/PATCH/DELETE 请求检查 Origin 头，仅允许 localhost 和配置的 host:port |
| 无 shell 执行 | 代码中无 `os.system`、`subprocess`、`eval`、`exec` 调用 |
| Neo4j 参数化查询 | 除深度值外所有用户数据均通过 `$param` 参数化绑定，防止 Cypher 注入 |
| 密钥不回显 | `/api/settings` 只返回 `configured: true/false`，不返回密钥原文 |

### 已知风险 / Known Risks

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| 本地回环绑定 `127.0.0.1` 时无 TLS | 局域网嗅探可能截获密钥 | 默认绑定 127.0.0.1；若需远程访问请配置反向代理 + HTTPS |
| 日志脱敏可能遗漏自定义格式的密钥 | 密钥碎片出现在日志中 | 前端复制日志前会提示用户手动检查 |
| 32 位十六进制正则可能误脱敏非敏感数据（如 UUID） | 日志中 UUID 被替换为 [REDACTED] | 优先保密性；可后续优化为白名单模式 |
| Neo4j 深度值内插（已受控） | 若 `_safe_depth()` 被绕过则存在注入风险 | 静态方法+类型注解+文档警告三重防护 |
| 无请求频率限制 | 恶意脚本可高频调用 API | 本地工具场景风险可控；未来可添加 slowapi |

### 开发流程 / Dev Workflow

```
dev-N → dev-base → security-check-before-main → main
         ↑              ↑
    功能开发分支    安全审计+修复+规整化
                    (本分支)
```

- `dev-N`：功能开发分支
- `dev-base`：功能集成分支（所有 dev-N 合并到这里）
- `security-check-before-main`：安全检查+代码规整化（本分支）
- `main`：生产分支（由其他人最终 review）

This project only targets public Steam Web API data. It does not support bypassing privacy settings or collecting unauthorized private data.
