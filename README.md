# Steam 好友关系图谱工具

这是一个本地运行的 Steam 好友关系图谱工具。它可以从一个公开 Steam 用户主页 URL 开始，把这个用户作为 Root，按 1-4 层向下抓取公开好友关系，写入本机 Neo4j Desktop 数据库，并在本地 Web GUI 里查看头像、昵称、Steam 主页、备注、关系线、中心节点和最短路径。

## Neo4j Desktop 还有用吗？

有用，而且当前项目里它不是多余组件。

本项目自己的 Web GUI 负责日常操作：配置连接、启动抓取、看卡片式人物信息、编辑备注、查最短路径。Neo4j Desktop 负责运行本地图数据库、保存所有节点和关系，并提供 Neo4j Bloom 这种更专业的图谱探索视图。

简单说：

- 本项目 GUI：更适合“抓取和日常操作”。
- Neo4j Desktop：更适合“本地数据库管理和长期存储”。
- Neo4j Bloom：更适合“大图谱探索、路径分析、图数据库视角检查”。

以后如果你想换成 Neo4j Aura 或远程 Neo4j，也可以改 `.env` 里的连接地址。但当前本地使用场景下，推荐继续保留 Neo4j Desktop。

## 架构

```text
Steam Web API
    ↓
FastAPI + BFS 抓取器
    ↓
Neo4j Desktop 本地数据库
    ↓
本项目 Web GUI / Neo4j Bloom
```

核心能力：

- 支持 Steam `/profiles/<steamid>` 和 `/id/<vanity>` 主页 URL。
- 使用公开 Steam Web API，不读取 Cookie，不绕过隐私设置。
- 抓取深度限制为 1-4 层，最大用户数限制为 10000。
- 自动写入 `SteamUser` 节点和 `STEAM_FRIEND` 关系。
- 图谱界面支持中文 / English 切换。
- 支持头像卡片、备注、标签、分类、中心节点排行和最短路径查询。

## 🤖 AI 生成声明

> **本项目由 AI 辅助生成。**
>
> 本仓库的全部代码、文档、配置和设计主要由 **GPT 5.5 Vibe Coding** 生成，人工做少量审阅和调整。这意味着：
>
> - 项目结构、实现细节和文档措辞可能存在不符合最佳实践的地方。
> - 代码逻辑可能包含 AI 产生的幻觉、冗余或不够优雅的实现。
> - 安全性和边界处理未必经过完整的人工审查。
>
> 作为一个 **公开（Public）仓库**，特此声明其 AI 生成属性，方便使用者评估代码质量和适用场景。欢迎通过 Issue 或 PR 指出问题和改进建议。

## 免责声明与敏感信息说明

本项目是非官方的本地 Steam 好友关系图谱工具，仅用于个人学习、研究和本地可视化分析。本项目与 Valve、Steam、Neo4j 没有隶属、合作、授权、背书或官方关联。

本项目只使用公开 Steam Web API 可访问的数据，不读取 Cookie，不存储 Steam 密码，不尝试绕过隐私设置。由于 Steam 用户隐私设置、API 限制、网络状态和接口变更，抓取结果可能不完整、不准确或随时失效。

请不要将本项目用于骚扰、人肉搜索、未授权监控、营销轰炸、隐私侵犯或任何违法违规用途。使用者应自行确保其使用方式符合 [Steam Web API Terms of Use](https://steamcommunity.com/dev/apiterms)、Steam Subscriber Agreement、当地法律法规以及相关用户的隐私权益。

`.env`、Steam API Key、Neo4j 密码、数据库备份、导出文件、截图和手动备注可能包含敏感信息。公开仓库、提交 Issue、分享截图或发布数据集前，请先删除密钥、密码、个人备注和可识别的关系数据。

这不是法律建议。是否可以抓取、保存、分析或公开分享某些数据，需要使用者根据自己的使用场景自行判断并承担责任。

## 安装

### 1. 准备 Neo4j Desktop

1. 打开 Neo4j Desktop。
2. 创建或打开一个本地 DBMS。
3. 启动数据库。
4. 记住 Bolt 地址、用户名和密码。

默认 Bolt 地址通常是：

```text
bolt://localhost:7687
```

### 2. 准备 Steam Web API Key

Steam Web API Key 用来访问公开 Steam API。没有 Key 时无法抓取好友列表和用户资料。

### 3. 创建配置文件

```powershell
Copy-Item .env.example .env
```

编辑 `.env`：

```env
STEAM_API_KEY=你的SteamWebAPIKey
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=你的Neo4j密码
APP_HOST=127.0.0.1
APP_PORT=8000
DEFAULT_MAX_DEPTH=2
DEFAULT_MAX_NODES=2000
DEFAULT_DELAY_MS=300
```

### 4. 安装依赖

```powershell
uv sync
```

### 5. 启动本地应用

```powershell
uv run steam-friend-map
```

打开：

```text
http://127.0.0.1:8000
```

## 使用流程

1. 先在 Neo4j Desktop 里启动数据库。
2. 在本项目页面点击连接测试按钮，确认 Steam 和 Neo4j 都可用。
3. 在 Root URL 输入 Steam 用户主页，例如：

   ```text
   https://steamcommunity.com/id/example
   https://steamcommunity.com/profiles/7656119xxxxxxxxxx
   ```

4. 设置抓取深度和最大节点数。
5. 点击开始抓取。
6. 抓取完成后在图谱区查看关系网。
7. 点击节点查看头像、昵称、Steam 主页、好友状态、备注、标签和分类。
8. 在最短路径区域输入两个 SteamID，查询两个人之间的连接路径。
9. 需要大图探索时，打开 Neo4j Bloom 并使用页面里的 Cypher 示例。

## Neo4j Bloom 查询示例

Root 周边 3 层：

```cypher
MATCH p=(r:SteamUser {steam_id:$root})-[:STEAM_FRIEND*1..3]-(n)
RETURN p
LIMIT 500
```

两人最短路径：

```cypher
MATCH p=shortestPath(
  (a:SteamUser {steam_id:$from})-[:STEAM_FRIEND*..4]-(b:SteamUser {steam_id:$to})
)
RETURN p
```

## 常见问题

### 为什么有些好友无法继续向下抓？

Steam 好友列表可能是私密、仅好友可见，或者接口返回 401/403/404。项目会把这类节点标记为私密分支，不会尝试绕过隐私设置。

### 为什么不建议一开始就抓 4 层？

Steam 好友网络增长非常快。假设每个人平均 100 个好友，2 层就可能接近 10000 人，3-4 层会指数爆炸。建议先从 1-2 层和较小节点上限开始。

### Neo4j 连接失败怎么办？

检查：

- Neo4j Desktop 数据库是否已启动。
- `.env` 里的 `NEO4J_URI` 是否正确。
- 用户名和密码是否正确。
- Bolt 端口 `7687` 是否被防火墙或其他程序拦截。

### 端口 8000 被占用怎么办？

修改 `.env`：

```env
APP_PORT=8001
```

然后重新启动：

```powershell
uv run steam-friend-map
```

### 能不能抓取仅好友可见或私密好友列表？

当前版本不支持，也不计划在 v1 支持。项目只使用公开 Steam Web API，不读取 Cookie，不接入登录态。

## 测试

```powershell
uv run pytest
```

## English Quick Start

This project is a local Steam friend graph crawler and Neo4j visualizer. The Web GUI handles crawling, profile cards, notes, shortest paths, and graph exploration. Neo4j Desktop is still useful because it runs the local graph database and lets you inspect the same data with Neo4j Bloom.

1. Start your local database in Neo4j Desktop.
2. Copy `.env.example` to `.env`.
3. Fill in `STEAM_API_KEY`, `NEO4J_URI`, `NEO4J_USER`, and `NEO4J_PASSWORD`.
4. Install dependencies:

   ```powershell
   uv sync
   ```

5. Start the app:

   ```powershell
   uv run steam-friend-map
   ```

6. Open:

   ```text
   http://127.0.0.1:8000
   ```

The app only uses public Steam Web API data. Private friend lists are marked as inaccessible and skipped.

Disclaimer: this is an unofficial local research and visualization tool. It is not affiliated with, endorsed by, or sponsored by Valve, Steam, or Neo4j. Do not use it for harassment, doxxing, unauthorized monitoring, spam, privacy invasion, or any illegal activity. Never commit `.env`, Steam API keys, Neo4j passwords, database dumps, exported relationship data, screenshots with private notes, or other sensitive files to a public repository.
