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

This project only targets public Steam Web API data. It does not support bypassing privacy settings or collecting unauthorized private data.
