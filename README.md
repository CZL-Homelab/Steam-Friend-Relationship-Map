# Steam 好友关系图谱工具(Web)

[中文](README.md) | [English](document/README_EN.md)

## 目录

- [这个工具是做什么的？](#这个工具是做什么的)
- [你需要准备什么？](#你需要准备什么)
- [Neo4j Desktop 还有用吗？](#neo4j-desktop-还有用吗)
- [架构](#架构)
- [安全提醒：Public 仓库不要提交这些内容](#安全提醒public-仓库不要提交这些内容)
- [分支开发流程](#分支开发流程--branch-workflow)
- [AI 生成声明](#ai-生成声明)
- [免责声明与敏感信息说明](#免责声明与敏感信息说明)
- [网页端安全配置说明](#网页端安全配置说明)
- [从 0 开始安装](#从-0-开始安装)
  - [第 1 步：确认 uv 可用](#第-1-步确认-uv-可用)
  - [第 2 步：打开项目目录](#第-2-步打开项目目录)
  - [第 3 步：创建 .env 配置文件](#第-3-步创建-env-配置文件)
  - [第 4 步：获取 Steam Web API Key](#第-4-步获取-steam-web-api-key)
  - [第 5 步：准备填写 Steam API Key](#第-5-步准备填写-steam-api-key)
  - [第 6 步：准备 Neo4j Desktop](#第-6-步准备-neo4j-desktop)
  - [第 7 步：填写 Neo4j 非敏感连接信息](#第-7-步填写-neo4j-非敏感连接信息)
  - [第 8 步：检查完整 .env](#第-8-步检查完整-env)
  - [第 9 步：安装依赖](#第-9-步安装依赖)
  - [第 10 步：启动本地应用](#第-10-步启动本地应用)
- [第一次成功运行检查清单](#第一次成功运行检查清单)
- [第一次抓取好友图谱](#第一次抓取好友图谱)
  - [扫描前筛选怎么用？](#扫描前筛选怎么用)
  - [扫描后筛选、排序和朋友圈分析](#扫描后筛选排序和朋友圈分析)
  - [日志和安全排错](#日志和安全排错)
- [在 Neo4j Bloom 里查看图谱](#在-neo4j-bloom-里查看图谱)
- [常见问题](#常见问题)

---

这是一个本地运行的 Steam 好友关系图谱工具。你输入一个公开 Steam 用户主页 URL，把这个用户作为 Root，它会按 1-4 层向下抓取公开好友关系，支持写入本地轻量级嵌入式图数据库 Kùzu（默认，免安装）或外部 Neo4j Desktop 数据库（可选），并在本地 Web GUI 中展示头像、昵称、Steam 主页、备注、关系线、中心节点和最短路径。

## 这个工具是做什么的？

它适合用来做 Steam 好友关系网的本地整理和探索：

- 从一个 Steam 用户主页开始自动抓取公开好友列表。
- 自动生成好友关系图，不需要手动画线。
- 每个节点可以显示头像、昵称、Steam 主页、备注、标签和分类。
- 支持查询两个人之间的最短关系路径。
- 支持在本项目 GUI 中查看，也可以用 Kùzu Explorer 或 Neo4j Bloom 做更专业的大图分析。

本项目只使用公开 Steam Web API，不读取 Cookie，不接入 Steam 登录态，不尝试绕过隐私设置。

## 你需要准备什么？

开始前需要准备这些东西：

| 项目              | 用途                                                     |
| ----------------- | -------------------------------------------------------- |
| Steam 账号        | 用来申请 Steam Web API Key                               |
| Steam Web API Key | 用来调用公开 Steam Web API，建议通过网页保存到系统凭据库 |
| Kùzu 嵌入式图数据库 (默认) | **免安装**，在应用运行进程内由 Python 直接拉起，数据保存在 `./data/graph_kuzu` |
| Neo4j Desktop (可选) | 若选用 Neo4j 引擎，则用来运行本地数据库并使用 Bloom 探索 |
| uv                | 用来管理 Python 环境和依赖                               |
| Python 3.12+      | 项目运行环境，`uv` 会自动使用/管理                       |

推荐先只抓 1 层或 2 层。Steam 好友网络会指数增长，3-4 层可能很快接近或超过上限。

## 数据库引擎选择：Kùzu 还是 Neo4j？

本项目采用**图数据库“双引擎”架构**，默认使用 Kùzu 嵌入式图数据库，同时也支持外部 Neo4j 数据库。

- **Kùzu 嵌入式数据库（默认）**：
  - **优势**：**免去任何数据库软件的安装**，解压即用。它直接作为 Python 的一个包（嵌入式进程内）运行，内存与磁盘开销极低。数据默认保存在本地目录 `./data/graph_kuzu` 下。
  - **可视化**：支持通过本项目自带的 Web GUI 进行日常关系图谱查看、搜索和路径查询；如果需要更底层的 Cypher 数据调试，可使用 `kuzu-explorer` 容器（见后文说明）。
- **Neo4j Desktop（可选）**：
  - **优势**：支持使用 Neo4j Bloom 等外部成熟的图探索生态和算法包，适合更大规模的社交分析和专业图探索。
  - **配合使用**：本项目自己的 Web GUI 负责“抓取和日常操作”（如看卡片式人物信息、编辑备注、查最短路径）；Neo4j Desktop/Bloom 则更适合大图谱的高级分析。

以后如果你想换成 Neo4j Aura 或远程 Neo4j，也可以改 `.env` 里的连接地址。但为了快速上手，建议直接使用默认的 Kùzu 引擎。

## 架构

```text
       Steam Web API
             ↓
     FastAPI + BFS 抓取器
       /             \
   (默认)             (可选)
   Kùzu 引擎          Neo4j 引擎
 (本地嵌入式存储)    (外部图数据库)
       \             /
              ↓
       本项目 Web GUI (Cytoscape.js)
              ↓
  (针对 Neo4j 可选) Neo4j Bloom / (针对 Kùzu) Kùzu Explorer
```

核心能力：

- 支持 Steam `/profiles/<steamid>` 和 `/id/<vanity>` 主页 URL。
- 使用公开 Steam Web API，不读取 Cookie，不绕过隐私设置。
- 抓取深度限制为 1-4 层，最大用户数限制为 10000。
- 自动写入 `SteamUser` 节点和 `STEAM_FRIEND` 关系。
- Kùzu 使用每批 500 行的事务化写入，批量保存用户、项目元数据和好友关系，减少大型抓取任务的数据库往返。Kùzu writes users, project metadata, and friend relationships in transactional batches of 500 rows to reduce database round trips during large crawls.
- 抓取器会按请求批次批量读写 Kùzu 或 Neo4j 好友列表缓存，并自动忽略不完整的旧缓存后重新抓取。The crawler reads and writes Kùzu or Neo4j friend-list caches in request-sized batches and refetches incomplete legacy cache entries.
- 抓取进度、错误数和私密用户数按请求批次合并写入，暂停与完成、停止、失败等终态仍会立即持久化。Crawl progress and counters are coalesced per request batch while pause and terminal states remain immediately durable.
- 项目列表使用固定次数的独立聚合查询统计成员、关系和抓取任务，避免随项目数量增长的 N+1 查询和 Neo4j 笛卡尔中间结果。Project listings use a fixed set of independent aggregate queries, avoiding per-project N+1 reads and Neo4j Cartesian intermediates.
- 多项目通过显式 `IN_PROJECT` 成员关系隔离；同一 Steam 用户可安全出现在多个项目，删除一个项目不会删除其他项目仍在使用的用户。
- 备注、标签、分类、Root 层数、内层连接数和紧密度分数也存放在 `IN_PROJECT` 上；同一用户在不同项目中可拥有完全独立的视图数据和分析指标。
- 旧版仅使用 `project_id` 的数据库会在启动时自动执行一次幂等成员关系迁移，无需手工转换。
- 配置、密钥、项目切换和抓取任务创建使用统一的运行时互斥保护；切换或配置重载失败时会恢复原状态，避免后台任务继续使用已关闭的数据库或 HTTP 客户端。
- CSV 导出包含项目、备注、标签、层数和评分等完整字段，使用 UTF-8 BOM，并转义电子表格公式前缀；JSON 导出保持原始结构。
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

## 分支开发流程 / Branch Workflow

本仓库采用层级分支模型，所有功能开发最终经过安全检查后合并到 `main`：

```
dev-N (功能开发)
   ↓  PR / merge
dev-base (集成分支)
   ↓  安全审计+代码规整化
security-check-before-main
   ↓  最终 review (由他人执行)
main (生产分支)
```

| 分支                         | 用途                              | 谁能合并                         |
| ---------------------------- | --------------------------------- | -------------------------------- |
| `dev-N`                      | 功能开发分支（N=1,2,3...）        | 开发者自行管理                   |
| `dev-base`                   | 功能集成分支，所有 dev-N 合并到此 | 开发者                           |
| `security-check-before-main` | 安全审计+修复+代码规整化          | 安全审计者                       |
| `main`                       | 生产分支                          | **必须由他人**最终 review 后合并 |

### 开发流程

1. 从 `dev-base` 创建功能分支 `dev-N`
2. 开发完成后 PR 到 `dev-base`
3. 当 `dev-base` 积累足够功能后，从 `dev-base` 创建 `security-check-before-main`
4. 在 `security-check-before-main` 上执行：
   - 安全审计（参考 `SECURITY.md` 中的审计报告格式）
   - 修复发现的安全问题
   - 代码注释规整化
   - 更新 `SECURITY.md` 审计报告
5. 提交 `security-check-before-main`，由**其他人** review 后合并到 `main`

### Commit 规范

- **Title**：中英双语，中文在前。格式：`feat/fix/chore: 中文简述 / English brief`
- **Body**：中英文各一段，列出关键改动

## AI 生成声明

> **本项目由 AI 辅助生成。**
>
> 本仓库的全部代码、文档、配置和设计主要由 **GPT 5.5 Vibe Coding** 生成，人工做少量审阅和调整。这意味着：
>
> - 项目结构、实现细节和文档措辞可能存在不符合最佳实践的地方。
> - 代码逻辑可能包含 AI 产生的幻觉、冗余或不够优雅的实现。
> - 安全性和边界处理未必经过完整的人工审查。
> - **Vibe Coding 规则强制要求**：任何时候使用 AI（包括 Vibe Coding 模式）辅助开发时，**必须首先要求 AI 助手完整阅读并严格遵循项目根目录下的 `.cursorrules` 规则**，以确保密钥脱敏、日志安全、代码结构和分支开发/Commit 规范得到贯彻执行。
>
> 作为一个 **公开（Public）仓库**，特此声明其 AI 生成属性，方便使用者评估代码质量和适用场景。欢迎通过 Issue 或 PR 指出问题和改进建议。

## 免责声明与敏感信息说明

本项目是非官方的本地 Steam 好友关系图谱工具，仅用于个人学习、研究和本地可视化分析。本项目与 Valve、Steam、Neo4j 没有隶属、合作、授权、背书或官方关联。

本项目只使用公开 Steam Web API 可访问的数据，不读取 Cookie，不存储 Steam 密码，不尝试绕过隐私设置。由于 Steam 用户隐私设置、API 限制、网络状态和接口变更，抓取结果可能不完整、不准确或随时失效。

请不要将本项目用于骚扰、人肉搜索、未授权监控、营销轰炸、隐私侵犯或任何违法违规用途。使用者应自行确保其使用方式符合 [Steam Web API Terms of Use](https://steamcommunity.com/dev/apiterms)、Steam Subscriber Agreement、当地法律法规以及相关用户的隐私权益。

`.env`、Steam API Key、Neo4j 密码、数据库备份、导出文件、截图和手动备注可能包含敏感信息。公开仓库、提交 Issue、分享截图或发布数据集前，请先删除密钥、密码、个人备注和可识别的关系数据。

这不是法律建议。是否可以抓取、保存、分析或公开分享某些数据，需要使用者根据自己的使用场景自行判断并承担责任。

## 💾 图数据库“双引擎”配置与选型指南

本项目采用**图数据库“双引擎”架构**，支持进程内嵌入式运行与外部专业数据库服务，用户可根据场景自由切换：

### 双引擎特性对比

| 维度 | Kùzu (默认) | Neo4j |
| :--- | :--- | :--- |
| **激活参数** | `GRAPH_DB_ENGINE=kuzu` | `GRAPH_DB_ENGINE=neo4j` |
| **使用场景** | 本地轻量化使用、快速开发与验证、CI 自动化测试 | 大规模社交分析、协同可视化探索、长期持久化存储 |
| **部署门槛** | **零门槛** (在应用运行进程内由 Python 直接拉起) | **中门槛** (需独立安装运行 Neo4j Desktop 或外部 AuraDB 服务) |
| **外部生态** | 无独立可视化客户端，不支持 Neo4j 原生算法包 | 支持 Neo4j Bloom, Browser 等专业图探索生态 |
| **开源许可** | MIT License (非常宽松商业友好) | GPL v3 / 闭源企业授权限制 |

### 切换配置说明

您可通过修改本地 `.env` 配置文件（或启动后的前端网页设置面板）自由切换引擎，前端配置字段会随引擎自动联动切换隐藏：

#### 1. 使用 Kùzu 嵌入式图数据库 (默认)
数据默认保存在本地 `./data/graph_kuzu` 下。
```ini
GRAPH_DB_ENGINE=kuzu
KUZU_DB_PATH=./data/graph_kuzu         # 数据存储文件路径
KUZU_BUFFER_POOL_SIZE_GB=1             # 限制 Kùzu 占用物理内存的最大缓冲池大小
```

#### 2. 使用 Neo4j 独立图数据库
连接外部运行中的 Neo4j 数据库服务：
```ini
GRAPH_DB_ENGINE=neo4j
NEO4J_URI=bolt://localhost:7687        # 数据库 Bolt 连接地址
NEO4J_USER=neo4j                       # 用户名
```
> [!NOTE]
> 为保护隐私安全，Neo4j 密码**不推荐**写入 `.env`，应在 Web UI 启动后在设置中保存，它会自动加密写入系统底层钥匙串/凭据管理器中。

#### 3. 双库数据一键迁移工具 (CLI)
如果您之前使用的是 Neo4j 并且想将所有社交网络数据、好友关系、自定义项目、标签、备注等迁移到 Kùzu 嵌入式数据库（或者相反），项目已内置了专门的跨库迁移工具。

在终端中运行如下命令即可完成数据单向一键迁移：

*   **从 Neo4j 迁移至 Kùzu (推荐)**：
    ```bash
    uv run python -m steam_friend_relationship_map.migration --from-engine neo4j --to-engine kuzu
    ```
*   **从 Kùzu 迁移至 Neo4j**：
    ```bash
    uv run python -m steam_friend_relationship_map.migration --from-engine kuzu --to-engine neo4j
    ```
*   **参数与重写选项**：
    *   `--project-id <id>`: 可选，只迁移特定项目 ID（留空默认迁移所有项目）。
    *   `--neo4j-uri` / `--neo4j-user` / `--kuzu-db-path`: 重写对应的连接配置参数。
    *   若命令行中没有传入 Neo4j 密码，工具会自动尝试从钥匙串凭据管理器中提取；若未提取到则会有安全命令行输入提示。

## 网页端安全配置说明

当前版本推荐在网页端填写 Steam API Key、Steam 代理 URL 和 Neo4j 密码。保存后它们会写入系统凭据库，例如 Windows Credential Manager，而不是写入 `.env`。

安全策略：

- 前端输入框使用密码框。
- 保存后输入框会清空。
- API 只返回“已配置/未配置”，不会回显 Steam API Key、代理 URL 或 Neo4j 密码原文。
- Steam 代理支持 `http://`、`https://`、`socks5://` 和 `socks5h://`；包含账号密码的代理 URL 同样保存在系统凭据库并进入日志脱敏列表。
- `.env` 只建议保存非敏感配置，例如 Neo4j 地址、用户名、端口和默认抓取参数。
- `.env` 中的 `STEAM_PROXY_URL` 可作为兼容回退；若 URL 包含认证信息，仍建议迁移到网页端安全存储。
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

### 第 3 步：初始化 `.env` 配置文件

为了让项目正确启动，你需要创建 `.env` 配置文件。现在项目支持**交互式自动初始化**：

1. **自动引导**：直接在终端中启动项目：
   ```bash
   uv run steam-friend-map
   ```
   如果系统检测到本地没有 `.env` 文件，会自动在控制台中引导你输入希望运行 Web UI 的本地端口（例如 `8000`）。完成后会自动基于 `.env.example` 生成 `.env` 配置文件。

2. **显式初始化/重新配置**：如果你想重新配置或显式运行初始化命令，可以附加 `--init` 参数：
   ```bash
   uv run steam-friend-map --init
   ```

3. **手动复制（备用）**：你也可以像以前一样手动复制模板：
   - Windows (PowerShell):
     ```powershell
     Copy-Item .env.example .env
     ```
   - Linux/macOS:
     ```bash
     cp .env.example .env
     ```
   刚生成的 `.env` 文件大约如下，你可以在其中通过修改 `APP_PORT` 来配置 Web UI 的本地端口：
   ```env
   APP_PORT=8000
   ```

这个配置文件不推荐包含 Steam API Key 和 Neo4j 密码等敏感信息。敏感信息建议稍后在 Web UI 启动后的网页端“安全配置”区域填写，系统会自动安全保存。

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

### 第 6 步：准备 Neo4j Desktop（若使用默认的 Kùzu 引擎，可直接跳过第 6、7 步）

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

### 第 7 步：填写 Neo4j 非敏感连接信息（若使用默认的 Kùzu 引擎，可直接跳过第 6、7 步）

继续编辑 `.env`：

```env
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
```

如果你在 Neo4j Desktop 里改过 Bolt 端口或用户名，就按你的实际配置填写。Neo4j 密码稍后在网页端“安全配置”区域填写并保存到系统凭据库。

### 第 8 步：检查完整 `.env`

最终的 `.env` 配置文件会根据你选择的引擎而有所不同。

#### 方案 A：使用 Kùzu 嵌入式数据库（默认推荐，免安装）

如果你想使用 Kùzu 作为底层图数据库，你的 `.env` 应当如下：

```env
GRAPH_DB_ENGINE=kuzu
KUZU_DB_PATH=./data/graph_kuzu         # 本地数据存储路径
KUZU_BUFFER_POOL_SIZE_GB=1             # Kùzu 内存缓冲池大小限制
APP_HOST=127.0.0.1
APP_PORT=8000
DEFAULT_MAX_DEPTH=1
DEFAULT_MAX_NODES=200
DEFAULT_DELAY_MS=500
```

#### 方案 B：使用 Neo4j Desktop 数据库（可选）

如果你想使用 Neo4j 作为底层图数据库，你的 `.env` 应当如下：

```env
GRAPH_DB_ENGINE=neo4j
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
APP_HOST=127.0.0.1
APP_PORT=8000
DEFAULT_MAX_DEPTH=1
DEFAULT_MAX_NODES=200
DEFAULT_DELAY_MS=500
```

每一项配置的含义：

| 配置项 | 含义 |
| :--- | :--- |
| `GRAPH_DB_ENGINE` | 激活的图数据库引擎类型，可选 `kuzu` 或 `neo4j` |
| `KUZU_DB_PATH` | Kùzu 数据库文件的本地存储路径 |
| `KUZU_BUFFER_POOL_SIZE_GB` | 限制 Kùzu 占用物理内存的最大缓冲池大小 (GB) |
| `NEO4J_URI` | Neo4j Bolt 连接地址 |
| `NEO4J_USER` | Neo4j 用户名，通常是 `neo4j` |
| `APP_HOST` | 本地服务监听地址，默认 `127.0.0.1` |
| `APP_PORT` | 本地服务端口，默认 `8000` |
| `DEFAULT_MAX_DEPTH` | 默认抓取层数，建议先用 `1` 或 `2` |
| `DEFAULT_MAX_NODES` | 默认最大节点数 |
| `DEFAULT_DELAY_MS` | Steam API 请求间隔，单位毫秒 |

Steam API Key 和 Neo4j 密码不在该配置文件中，因为它们属于敏感信息，建议在网页端保存到系统凭据库中。

### 第 9 步：安装依赖

在项目目录运行：

```powershell
uv sync
```

它会创建虚拟环境并自动安装 FastAPI、Kùzu 数据库驱动、Neo4j Driver、httpx 等所有项目依赖。

### 第 10 步：启动本地应用

如果您使用的是 Neo4j 引擎，请确认 Neo4j Desktop 数据库已经 Start 运行；如果使用默认的 Kùzu 引擎，则无需启动任何外部数据库。

在终端中运行：

```powershell
uv run steam-friend-map
```

看到类似 `Uvicorn running on http://127.0.0.1:8000` 后，打开浏览器访问：

```text
http://127.0.0.1:8000
```

可用健康接口确认应用和图数据库都已就绪：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health
```

图数据库可用时返回 HTTP 200；不可用时返回 HTTP 503 和已脱敏的错误信息。服务退出会先停止后台抓取任务，再依次关闭 Steam HTTP 客户端和数据库，避免遗留 Kuzu 文件锁。

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

| 项目                | 建议           |
| ------------------- | -------------- |
| Depth               | `1`            |
| Nodes               | `200` 或 `500` |
| Delay ms            | `300`          |
| Concurrent requests | `4`            |

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

“并发请求数”限制同时等待 Steam 响应的请求数量，范围为 `1-16`。建议从 `4` 开始；若频繁遇到 HTTP 429，可降低并发或增大 Delay ms，程序也会按 Steam 的 `Retry-After` 自动退避。

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

## 📊 使用 Kùzu Explorer 可视化探索图数据

因为 Kùzu 属于嵌入式图数据库，默认没有运行独立的图管理服务。如果您需要直观调试、运行自定义 Cypher 语句或探索节点关系，可使用 Kùzu 官方提供的 **Kùzu Explorer** 可视化调试面板。

项目已内置了支持一键启动的可视化环境配置，最方便的方式是通过 Docker 一键拉起：

### 一键启动可视化面板 (推荐)

在项目根目录下，直接在终端中运行如下 Docker 命令（注意将本地数据库绝对路径映射入容器内）：

```bash
docker run -p 8080:8000 \
  -v "$(pwd)/data/graph_kuzu:/database" \
  --name kuzu-explorer \
  --rm \
  kuzudb/kuzu-explorer:latest
```

> [!TIP]
> * 启动后，打开浏览器访问 **[http://localhost:8080](http://localhost:8080)** 即可进入 Kùzu Explorer 可视化管理后台。
> * 进入后可在顶部的 Cypher 输入框中直接输入任意标准查询（如 `MATCH (u:SteamUser) RETURN u LIMIT 50`）绘制本地图谱。

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

   Check readiness at `http://127.0.0.1:8000/api/health`. It returns HTTP 503 when the graph database is unavailable. Shutdown stops background crawls before closing the Steam HTTP client and database.

8. Use the Secure Settings panel to save your Steam API Key and Neo4j password into the system credential store.

The app only uses public Steam Web API data. Private friend lists are marked as inaccessible and skipped. Pre-scan filters can limit candidates by public friend count or by links to the prior user pool; post-scan filters and Friend Circle Analysis work only on data already stored in your local Neo4j database.

Project membership is represented by explicit `IN_PROJECT` relationships. Notes, tags, categories, Root depth, prior-pool link counts, and closeness scores are stored on that membership, so one Steam user can have independent annotations and analysis metrics in different projects. Legacy node metadata is migrated idempotently on startup.

Runtime mutations are serialized with crawl creation. Settings, secrets, and project switches roll back when persistence or runtime reload fails, preventing a background crawl from retaining a closed database or HTTP client.

CSV exports include project annotations and analysis fields, use a UTF-8 BOM, and escape spreadsheet formula prefixes. JSON exports retain the original graph structure.

System Logs / Dev Logs redact API keys, passwords, Cookie, Authorization, and common `password=` / `key=` values before showing them in the browser. SteamIDs, notes, screenshots, and relationship context may still be personal data, so review logs before sharing.

Disclaimer: this is an unofficial local research and visualization tool. It is not affiliated with, endorsed by, or sponsored by Valve, Steam, or Neo4j. Do not use it for harassment, doxxing, unauthorized monitoring, spam, privacy invasion, or any illegal activity. Never commit `.env`, Steam API keys, Neo4j passwords, database dumps, exported relationship data, screenshots with private notes, or other sensitive files to a public repository.
