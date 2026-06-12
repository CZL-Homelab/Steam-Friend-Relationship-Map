# Steam 好友关系图谱工具

这是一个本地运行的 Steam 好友关系图谱工具。你输入一个公开 Steam 用户主页 URL，把这个用户作为 Root，它会按 1-4 层向下抓取公开好友关系，写入本机 Neo4j Desktop 数据库，并在本地 Web GUI 中展示头像、昵称、Steam 主页、备注、关系线、中心节点和最短路径。

## 这个工具是做什么的？

它适合用来做 Steam 好友关系网的本地整理和探索：

- 从一个 Steam 用户主页开始自动抓取公开好友列表。
- 自动生成好友关系图，不需要手动画线。
- 每个节点可以显示头像、昵称、Steam 主页、备注、标签和分类。
- 支持查询两个人之间的最短关系路径。
- 支持在本项目 GUI 中查看，也可以用 Neo4j Bloom 做更专业的大图分析。

本项目只使用公开 Steam Web API，不读取 Cookie，不接入 Steam 登录态，不尝试绕过隐私设置。

## 你需要准备什么？

开始前需要准备这些东西：

| 项目 | 用途 |
| --- | --- |
| Steam 账号 | 用来申请 Steam Web API Key |
| Steam Web API Key | 用来调用公开 Steam Web API，建议通过网页保存到系统凭据库 |
| Neo4j Desktop | 用来运行本地图数据库 |
| uv | 用来管理 Python 环境和依赖 |
| Python 3.12+ | 项目运行环境，`uv` 会自动使用/管理 |

推荐先只抓 1 层或 2 层。Steam 好友网络会指数增长，3-4 层可能很快接近或超过上限。

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

## 安全提醒：Public 仓库不要提交这些内容

如果这个仓库会公开，请特别注意不要提交：

- `.env`
- Steam Web API Key
- Neo4j 用户名和密码
- Neo4j 数据库 dump、backup、`.db`、SQLite 文件
- 导出的真实 CSV/JSON 图谱数据
- 包含个人备注、好友路径、SteamID、头像或昵称的截图
- 任何 Cookie、登录态、密码、访问令牌或浏览器会话信息

`.env` 已经被 `.gitignore` 忽略，但如果你把 Key 手动复制到 README、Issue、截图或其他文件里，Git 仍然可能记录这些内容。

## AI 生成声明

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

## 网页端安全配置说明

当前版本推荐在网页端填写 Steam API Key 和 Neo4j 密码。保存后它们会写入系统凭据库，例如 Windows Credential Manager，而不是写入 `.env`。

安全策略：

- 前端输入框使用密码框。
- 保存后输入框会清空。
- API 只返回“已配置/未配置”，不会回显 Steam API Key 或 Neo4j 密码原文。
- `.env` 只建议保存非敏感配置，例如 Neo4j 地址、用户名、端口和默认抓取参数。
- 旧版 `.env` 中的 `STEAM_API_KEY` 和 `NEO4J_PASSWORD` 仍然兼容读取，但网页会提示建议迁移到安全存储。
- 如果你需要真正的浏览器到后端传输层加密，应启用本地 HTTPS；普通 localhost HTTP 不应被描述为“全链路加密”。

## 从 0 开始安装

下面按第一次使用的真实顺序来走。不要跳步，尤其是 `.env` 要先创建，再把 Steam Key 和 Neo4j 密码填进去。

### 第 1 步：确认 uv 可用

在 PowerShell 里运行：

```powershell
uv --version
```

如果能看到版本号，说明 `uv` 已经可用。

如果还没有安装 `uv`，请先安装后再继续。项目依赖、虚拟环境和启动命令都通过 `uv` 管理。

### 第 2 步：打开项目目录

进入本项目目录：

```powershell
cd Steam-Friend-Relationship-Map
```

如果你把项目放在了其他位置，请换成自己的路径。

### 第 3 步：创建 `.env` 配置文件

先复制模板：

```powershell
Copy-Item .env.example .env
```

然后打开 `.env`。刚创建出来大概是这样：

```env
STEAM_API_KEY=
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=
APP_HOST=127.0.0.1
APP_PORT=8000
DEFAULT_MAX_DEPTH=2
DEFAULT_MAX_NODES=2000
DEFAULT_DELAY_MS=300
```

这个模板不包含 Steam API Key 和 Neo4j 密码。敏感信息建议稍后在网页端“安全配置”区域填写。

### 第 4 步：获取 Steam Web API Key

Steam Web API Key 用来访问公开 Steam API。没有 Key 时无法抓取好友列表和用户资料。

获取方式：

1. 登录你的 Steam 账号。
2. 打开 Steam Web API Key 页面：

   ```text
   https://steamcommunity.com/dev/apikey
   ```

3. 如果页面要求填写 Domain Name，可以填写：

   ```text
   localhost
   ```

   这个项目默认是本地工具，不需要真实公网服务器。你也可以填写自己的域名。

4. 阅读并同意 Steam API Terms of Use。
5. 提交后页面会显示一串 API Key。
6. 复制这串 Key，稍后在网页端“安全配置”区域填写。

注意：

- Steam Web API Key 属于敏感信息，不要提交到 GitHub。
- 不要把 Key 发到 Issue、截图、README、聊天记录或公开文档里。
- 如果 Key 泄露，请回到 Steam API Key 页面撤销或重新生成。
- Steam 官方文档说明，使用 Steam Web API 需要 API Key，并需要同意 Steam API Terms of Use：`https://steamcommunity.com/dev`。

### 第 5 步：准备填写 Steam API Key

先把第 4 步得到的 Key 临时放在安全位置，后面打开网页后填写到“安全配置”区域。不要把真实 Key 写进 README 或提交到 Git。

如果你已经用旧版方式写进 `.env`，项目仍会兼容读取，但建议迁移到网页端安全配置。

### 第 6 步：准备 Neo4j Desktop

1. 打开 Neo4j Desktop。
2. 创建一个 Project，或者使用已有 Project。
3. 在 Project 里创建一个本地 DBMS。
4. 设置数据库密码，并记住它。
5. 点击 Start 启动数据库。
6. 确认数据库处于 Running 状态。

默认 Bolt 地址通常是：

```text
bolt://localhost:7687
```

默认用户名通常是：

```text
neo4j
```

这个工具会通过 Bolt 连接 Neo4j Desktop，把 Steam 用户和好友关系写进去。

### 第 7 步：填写 Neo4j 非敏感连接信息

继续编辑 `.env`：

```env
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
```

如果你在 Neo4j Desktop 里改过 Bolt 端口或用户名，就按你的实际配置填写。Neo4j 密码稍后在网页端“安全配置”区域填写并保存到系统凭据库。

### 第 8 步：检查完整 `.env`

最终 `.env` 应该类似这样：

```env
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
APP_HOST=127.0.0.1
APP_PORT=8000
DEFAULT_MAX_DEPTH=1
DEFAULT_MAX_NODES=200
DEFAULT_DELAY_MS=500
```

每一项含义：

| 配置项 | 含义 |
| --- | --- |
| `NEO4J_URI` | Neo4j Bolt 连接地址 |
| `NEO4J_USER` | Neo4j 用户名，通常是 `neo4j` |
| `APP_HOST` | 本地服务监听地址，默认 `127.0.0.1` |
| `APP_PORT` | 本地服务端口，默认 `8000` |
| `DEFAULT_MAX_DEPTH` | 默认抓取层数，建议先用 `1` 或 `2` |
| `DEFAULT_MAX_NODES` | 默认最大节点数 |
| `DEFAULT_DELAY_MS` | Steam API 请求间隔，单位毫秒 |

Steam API Key 和 Neo4j 密码不在这个表里，因为它们属于敏感信息，建议在网页端保存到系统凭据库。

### 第 9 步：安装依赖

在项目目录运行：

```powershell
uv sync
```

它会创建虚拟环境并安装 FastAPI、Neo4j Driver、httpx 等依赖。

### 第 10 步：启动本地应用

确认 Neo4j Desktop 数据库已经 Start，然后运行：

```powershell
uv run steam-friend-map
```

看到类似 `Uvicorn running on http://127.0.0.1:8000` 后，打开浏览器访问：

```text
http://127.0.0.1:8000
```

## 第一次成功运行检查清单

打开页面后，按这个顺序检查：

1. 页面能打开。
2. 左侧能看到连接、抓取、筛选等面板。
3. 在“安全配置”区域填写 Steam API Key 和 Neo4j 密码，点击保存。
4. 点击连接测试按钮。
5. Steam 状态显示正常。
6. Neo4j 状态显示正常。
7. 如果 Neo4j 失败，先确认 Neo4j Desktop 数据库是否已经 Start。
8. 如果 Steam 失败，先确认 Steam API Key 是否保存成功。
9. 展开“系统日志 / Dev Logs”，确认没有红色错误。日志会自动脱敏，适合排查连接、图谱查询和前端异常。

## 第一次抓取好友图谱

第一次建议用很保守的设置：

| 项目 | 建议 |
| --- | --- |
| Depth | `1` |
| Nodes | `200` 或 `500` |
| Delay ms | `300` |

操作步骤：

1. 找一个公开 Steam 用户主页。
2. 复制主页 URL，例如：

   ```text
   https://steamcommunity.com/id/example
   https://steamcommunity.com/profiles/7656119xxxxxxxxxx
   ```

3. 粘贴到 Root URL。
4. Depth 先填 `1`。
5. Nodes 先填 `200`。
6. 点击开始抓取。
7. 等待状态变成完成。
8. 在图谱区查看节点和关系线。
9. 点击一个节点，右侧会显示头像、昵称、主页链接、备注、标签和分类。

确认 1 层正常后，再尝试 2 层。不要一上来就抓 4 层。

### 扫描前筛选怎么用？

抓取面板里有“扫描前筛选”：

- 最小/最大好友数：只让公开好友数落在范围内的候选用户进入下一层。例如 `100-500`、`1000 以上` 或 `100 以下`。
- 前层朋友圈连接阈值：候选用户必须和更靠近 Root 的用户池至少有多少条已知好友关系。默认 `0` 表示不启用。

注意：好友数筛选会额外请求候选人的公开好友列表，因此会更慢，也更容易触发 API 限速。阈值越高，扫描越收敛，适合减少指数爆炸。

### 扫描后筛选、排序和朋友圈分析

左侧“筛选”面板作用于已经写入 Neo4j 的数据：

- 可以按好友数范围、前层朋友圈连接阈值过滤当前图谱。
- 可以按层数、度数、好友数、朋友圈连接数、紧密度排序。
- 可以选择头像大小依据，让共同连接更多或更紧密的用户在图上更明显。
- 布局选择“紧密度靠中心”后，紧密度更高的节点会更靠近图谱中心。

右侧“朋友圈分析”会查找潜在 Root 朋友：这些人不是 Root 的直接好友，但和更靠近 Root 的用户池有多条已知连接。结果里的“共同连接”和“分数”只基于当前数据库中已经抓到的公开关系，不代表真实社交关系的完整结论。

### 日志和安全排错

页面有两类日志：

- 抓取日志：只显示当前抓取任务的进度事件。
- 系统日志 / Dev Logs：显示后端 API、图谱查询、Neo4j、Steam API 和前端异常。

日志进入页面前会自动脱敏 Steam API Key、Neo4j 密码、Cookie、Authorization、`password=`、`key=` 等内容。即便如此，SteamID、昵称、头像、备注、路径和截图仍可能包含个人信息，复制日志或截图前请再检查一遍。

## 在 Neo4j Bloom 里查看图谱

Neo4j Bloom 适合查看更大的图，或者做更专业的图数据库探索。

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

其中 `$root`、`$from`、`$to` 要替换成真实 SteamID。

## 常见问题

### 为什么有些好友无法继续向下抓？

Steam 好友列表可能是私密、仅好友可见，或者接口返回 401/403/404。项目会把这类节点标记为私密分支，不会尝试绕过隐私设置。

### 为什么不建议一开始就抓 4 层？

Steam 好友网络增长非常快。假设每个人平均 100 个好友，2 层就可能接近 10000 人，3-4 层会指数爆炸。建议先从 1-2 层和较小节点上限开始。

### Steam API Key 页面打不开怎么办？

检查：

- 是否已经登录 Steam。
- 是否能正常访问 Steam Community。
- 是否打开的是 `https://steamcommunity.com/dev/apikey`。
- 如果 Key 已泄露，请到同一页面撤销或重新生成。

### Neo4j 连接失败怎么办？

检查：

- Neo4j Desktop 数据库是否已启动。
- `.env` 里的 `NEO4J_URI` 是否正确。
- 用户名和密码是否正确。
- Bolt 端口 `7687` 是否被防火墙或其他程序拦截。
- 是否启动了多个 Neo4j，占用了不同端口。

### Neo4j Desktop 里为什么只看到几个节点？

Neo4j Explore/Bloom 当前画布可能只显示当前场景、当前搜索结果或当前透视图中的节点，不等于数据库真实总量。网页端“数据库状态”会显示 Neo4j 中的真实 `SteamUser` 和 `STEAM_FRIEND` 数量。

也可以在 Neo4j Query 中执行：

```cypher
MATCH (u:SteamUser)
RETURN count(u)
```

```cypher
MATCH ()-[r:STEAM_FRIEND]->()
RETURN count(r)
```

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

### 可以把抓到的数据发到 GitHub 吗？

不建议。SteamID、好友关系、备注、截图、导出 CSV/JSON、Neo4j dump 都可能包含敏感信息。公开分享前请先匿名化，或者确认你有权分享。

## 测试

运行：

```powershell
uv run pytest
```

如果看到全部测试通过，说明基础功能没有被破坏。

## English Quick Start

This project is a local Steam friend graph crawler and Neo4j visualizer. The Web GUI handles crawling, profile cards, notes, shortest paths, and graph exploration. Neo4j Desktop is still useful because it runs the local graph database and lets you inspect the same data with Neo4j Bloom.

1. Copy `.env.example` to `.env`:

   ```powershell
   Copy-Item .env.example .env
   ```

2. Get a Steam Web API Key from:

   ```text
   https://steamcommunity.com/dev/apikey
   ```

3. Fill in non-secret `.env` values:

   ```env
   NEO4J_URI=bolt://localhost:7687
   NEO4J_USER=neo4j
   ```

4. Start your local database in Neo4j Desktop.

5. Install dependencies:

   ```powershell
   uv sync
   ```

6. Start the app:

   ```powershell
   uv run steam-friend-map
   ```

7. Open:

   ```text
   http://127.0.0.1:8000
   ```

8. Use the Secure Settings panel to save your Steam API Key and Neo4j password into the system credential store.

The app only uses public Steam Web API data. Private friend lists are marked as inaccessible and skipped. Pre-scan filters can limit candidates by public friend count or by links to the prior user pool; post-scan filters and Friend Circle Analysis work only on data already stored in your local Neo4j database.

System Logs / Dev Logs redact API keys, passwords, Cookie, Authorization, and common `password=` / `key=` values before showing them in the browser. SteamIDs, notes, screenshots, and relationship context may still be personal data, so review logs before sharing.

Disclaimer: this is an unofficial local research and visualization tool. It is not affiliated with, endorsed by, or sponsored by Valve, Steam, or Neo4j. Do not use it for harassment, doxxing, unauthorized monitoring, spam, privacy invasion, or any illegal activity. Never commit `.env`, Steam API keys, Neo4j passwords, database dumps, exported relationship data, screenshots with private notes, or other sensitive files to a public repository.
