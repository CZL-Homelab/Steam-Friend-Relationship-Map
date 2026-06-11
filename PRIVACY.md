# 隐私说明 / Privacy Notice

## 中文

本项目默认在你的本机运行，不提供作者托管的云服务，不内置遥测，不会主动把你的 Steam 数据、Neo4j 数据库内容、备注或配置上传到作者服务器。

### 可能被本地保存的数据

当你运行抓取任务时，以下信息可能会写入本机 Neo4j 数据库：

- SteamID
- Steam 昵称
- Steam 头像链接
- Steam 主页链接
- 公开好友关系
- Steam 公开资料可见性状态
- 你手动填写的备注、标签和分类
- 公开好友列表的好友数量
- 与前层用户池的连接数量、紧密度分数和朋友圈分析结果
- 本地系统日志中的脱敏错误信息和抓取状态

这些数据即使来自公开 API，在组合成关系图谱、备注、截图或导出文件后，也可能变成敏感信息。

朋友圈分析和紧密度分数只是基于当前本地数据库中已抓取的公开关系计算出的辅助指标，不应被当作对现实关系、亲密程度或个人身份的确定判断。

### 请谨慎公开分享

在公开 GitHub 仓库、提交 Issue、上传截图、发布数据集或分享 Neo4j dump 前，请先检查并删除：

- 真实 SteamID 和可识别好友关系
- 个人备注、标签、分类
- 导出的 CSV/JSON
- Neo4j 数据库备份或 dump
- 包含他人头像、昵称、备注或路径关系的截图
- 包含 SteamID、备注或路径上下文的系统日志

公开分享关系图谱前，请优先匿名化，或确认你有权分享相关数据。

### 删除本地数据

你可以通过以下方式删除本地数据：

- 在 Neo4j Desktop 中删除对应数据库。
- 在 Neo4j Browser / Bloom 中执行清理用 Cypher。
- 删除本地导出的 CSV/JSON、截图和数据库备份。
- 删除 `.env` 中的本地配置和密钥。

示例清库 Cypher：

```cypher
MATCH (n)
DETACH DELETE n
```

### 登录态和私密数据

当前版本不读取 Cookie，不接入 Steam 登录态，不存储 Steam 密码，不尝试绕过好友列表或个人资料隐私设置。

### Secret 保存方式

当前版本推荐将 Steam API Key 和 Neo4j 密码保存到系统凭据库，例如 Windows Credential Manager。网页端只显示“已配置/未配置”，不会回显原文。旧版 `.env` 中的 `STEAM_API_KEY` 和 `NEO4J_PASSWORD` 仍可兼容读取，但建议迁移到系统凭据库。

## English

This project runs locally by default. It does not include telemetry and does not upload your Steam data, Neo4j database, notes, or configuration to a server operated by the author.

The local Neo4j database may store SteamIDs, persona names, avatar URLs, profile URLs, public friend relationships, visibility states, and notes/tags/categories you manually enter. Even if some data comes from public APIs, combined relationship graphs, screenshots, exports, and notes may become sensitive.

Before publishing issues, screenshots, CSV/JSON exports, datasets, or Neo4j dumps, remove API keys, passwords, personal notes, identifiable relationship data, and any information you do not have permission to share.

This project does not read cookies, does not store Steam passwords, and does not attempt to bypass Steam privacy settings.
