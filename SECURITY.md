# 安全审计报告 / Security Audit Report

## 报告安全漏洞 / Reporting a Vulnerability

请不要在公开 Issue、Discussion、PR、日志或截图中披露尚未修复的漏洞、凭据或真实关系数据。请使用仓库的 [GitHub Private Vulnerability Reporting](https://github.com/CZL-Homelab/Steam-Friend-Relationship-Map/security/advisories/new) 私密提交报告。

Do not disclose unpatched vulnerabilities, credentials, or real relationship data in public issues, discussions, pull requests, logs, or screenshots. Submit reports privately through [GitHub Private Vulnerability Reporting](https://github.com/CZL-Homelab/Steam-Friend-Relationship-Map/security/advisories/new).

报告应包含受影响版本或提交、最小复现步骤、潜在影响、已知缓解方式，以及不含真实密钥或个人数据的证明材料。维护者会先确认报告内容，再通过私密 Advisory 协调验证、修复和披露。不要对不属于你的系统、账户或数据进行测试。

Include the affected version or commit, minimal reproduction steps, potential impact, known mitigations, and evidence that contains no real secrets or personal data. The maintainer will validate the report and coordinate remediation and disclosure through the private advisory. Do not test against systems, accounts, or data you do not own or have permission to assess.

## 审计信息 / Audit Information

- 审计日期 / Date: `2026-08-01`
- 集成基线 / Integration baseline: `dev-base` at `82d96ee937137b4ba317d440b6161a0c90d2871f`
- 审计分支 / Audit branch: `dev/fix/security-audit-2026-08-01`
- 候审目标 / Review target: `security-check-before-main`
- 明确不在范围内 / Explicitly out of scope: creating, approving, or merging any PR into `main`

审计覆盖 FastAPI API、运行时配置与密钥存储、Steam HTTP 客户端、Kuzu/Neo4j 仓储层、项目隔离、抓取任务生命周期、NetworkX 分析、前端 DOM 与 URL 处理、CSV/JSON 导出、日志脱敏、依赖与 CI 供应链。

The audit covers the FastAPI surface, runtime settings and secret storage, Steam HTTP access, Kuzu and Neo4j repositories, project isolation, crawl lifecycle, NetworkX analysis, frontend DOM and URL handling, CSV/JSON exports, log redaction, dependencies, and CI supply-chain controls.

## 已修复问题 / Remediated Findings

### Kuzu 数据库启动与资源生命周期 / Kuzu Startup and Resource Lifecycle

- Kuzu 打开失败只尝试一次，不移动、归档、删除、覆盖或重建用户数据库。
- 文件锁冲突返回可执行的停止其他进程或更换 `KUZU_DB_PATH` 提示；旧自动恢复遗留文件只做只读识别。
- Windows 工作线程连接和数据库关闭行为有回归测试；事务 rollback 失败会带上下文写入日志，不再静默忽略。

Kuzu open failures are single-attempt and never move, archive, delete, overwrite, or recreate user data. Lock conflicts provide an actionable message, legacy recovery artifacts are inspected read-only, Windows connection closure is covered by tests, and rollback failures are logged with context.

### 查询与项目隔离 / Queries and Project Isolation

- Kuzu Root 图谱使用逐层 BFS，避免可变长度路径枚举导致 buffer pool 耗尽；多 Root、简单路径计数和边返回均有固定深度、节点、路径和边上限。
- 潜在好友查询在 Kuzu 与 Neo4j 中固定为参数化二跳查询，并严格限定当前项目。
- 用户值全部参数化；必须插值的深度、排序和更新字段来自固定范围。抓取任务动态更新字段新增白名单，未知字段在查询执行前拒绝。
- 多项目成员、关系、分析、统计和导出保持当前项目隔离，删除项目不会删除其他项目仍引用的用户。

Root graph traversal uses bounded BFS in Kuzu. Potential-friend queries are fixed, parameterized two-hop queries in both engines. User values are parameterized, interpolated identifiers come from fixed allowlists, and unknown crawl-update field names are rejected before query execution. Project membership, relationships, analysis, statistics, exports, and deletion remain project-scoped.

### 输入、内存与任务边界 / Input, Memory, and Task Bounds

- 图谱 Root 最多 5 个、单项最多 128 字符、深度最多 4、响应最多 10,000 个节点。
- 搜索、分类、路径端点、朋友圈 Root、项目、标签、配置和密钥均有数量或长度上限。
- NetworkX 分析最多处理 10,000 个节点和 50,000 条边，超限返回 `413`，不进入 PageRank/Louvain 计算。
- 抓取并发、节点数、后台任务取消、异步仓储 offload 和关闭顺序均有界并有回归测试。

Graph roots, depth, response size, query text, categories, path endpoints, tags, settings, and secrets are bounded. NetworkX analysis rejects graphs above 10,000 nodes or 50,000 edges before PageRank or Louvain computation. Crawl concurrency, node counts, cancellation, repository offloading, and shutdown ordering are bounded and tested.

### Web、日志与导出 / Web, Logging, and Export

- 写请求使用严格的 Origin/Referer scheme、host 和 port 校验；API 与 HTML 使用 `no-store`，并设置 CSP、`nosniff`、frame、referrer 和 permissions 响应头。
- 最近 Root、头像和个人主页 URL 使用协议白名单；`javascript:`、跨域明文 HTTP 和非图片 data URL 被拒绝。
- 最近 Root 和日志改用 DOM API 与 `textContent`，移除内联错误事件；其余动态 HTML 对 API 数据执行转义。
- 新窗口链接使用 `noopener noreferrer`。CSV 单元格防护 `= + - @ TAB CR` 公式前缀，导出不在前端复制整份文件。
- API key、密码、代理凭据、Authorization、Cookie 与常见密钥参数会在日志和异常响应前脱敏。

State-changing requests enforce exact Origin/Referer scheme, host, and port checks. Security and cache headers are permanent. External profile and image URLs use protocol allowlists, recent-root and log rendering use DOM APIs with `textContent`, new-window links use `noopener noreferrer`, CSV formula prefixes are neutralized, and configured credentials plus common authentication fields are redacted before logs or error responses are emitted.

### 永久质量门禁 / Permanent Quality Gates

- GitHub Actions 权限为只读，启用并发取消，第三方 Action 固定到完整提交 SHA。
- Ubuntu 与 Windows 均运行 `uv sync --frozen` 和完整 pytest。
- Ruff、Bandit、pip-audit、Node 语法、i18n JSON 与 TruffleHog verified/unknown 密钥扫描成为永久检查。
- 本次审计 PR 对完整 Git 历史执行密钥扫描；后续 PR 继续检查变化范围。

GitHub Actions uses read-only permissions, concurrency cancellation, and full-SHA action pins. Ubuntu and Windows run the frozen full suite. Ruff, Bandit, pip-audit, Node syntax, i18n JSON, and TruffleHog verified/unknown secret scanning are permanent gates.

## 工具结果 / Tool Results

- Ruff `E4/E7/E9/F/I`, Python 3.12, line length 100: passed locally.
- Bandit recursive source scan: zero findings after five narrowly documented suppressions.
- pip-audit against exported locked runtime requirements: no known vulnerabilities.
- pytest: `210 passed`（完整测试 / full suite）。
- Node syntax and i18n JSON parse: passed locally.
- TruffleHog full-history scan: passed in audit PR `#27` at `830589b` with no blocking verified/unknown findings after the documented historical URI false-positive handling.
- Application lifecycle smoke test with an isolated temporary Kuzu database: HTTP `200`; the existing `data/graph_kuzu` path was not opened or modified.
- GitHub Actions: Ubuntu tests, Windows tests, static/dependency checks, and secret scan all passed in audit PR `#27` at `830589b`.

## 候审交付记录 / Review Delivery Record

- 审计 PR `#27` 已通过 merge commit `cf8d05620b55e65c3e4674b381df7007f2631573` 合入 `security-check-before-main`，合并后四项质量门禁再次全部通过。
- GitHub API 客户端在创建该 merge commit 时错误地将中文元数据编码为问号。为遵守“不改写历史”规则，原提交保持不变；其规范双语标题记录为：`security: 合入最终安全审计交付 / merge final security audit delivery`。
- 后续元数据 PR 仅补充本记录并使用显式 UTF-8 请求创建双语 merge commit，不包含功能或安全行为变化。

Audit PR `#27` was merged into `security-check-before-main` as `cf8d05620b55e65c3e4674b381df7007f2631573`, and all four post-merge gates passed again. The API client incorrectly encoded the Chinese portion of that merge commit metadata as question marks. The commit is retained to honor the no-history-rewrite rule; its canonical bilingual title is recorded above. The metadata follow-up PR contains no functional or security behavior changes.

## Bandit 局部抑制 / Local Bandit Suppressions

以下 5 处是误报，均采用最小行级 `nosec`，未全局关闭规则：

- `B105`: `NEO4J_PASSWORD` 是环境变量名称，不是硬编码密码。
- `B105`: `[REDACTED]` 是日志输出占位符，不是凭据。
- `B104`: `0.0.0.0` 仅用于比较监听配置，没有在该行绑定网络接口。
- `B311` 两处: `random.uniform` 仅用于 HTTP 重试抖动，不生成令牌、密钥或其他安全随机值。

These five false positives are suppressed only on the relevant lines: two non-secret labels, one bind-address comparison, and two retry-jitter calls that do not require cryptographic randomness.

TruffleHog 完整历史首次扫描还发现 2 条 `unverified URI`：均为 `tests/test_app.py` 历史版本中指向 `127.0.0.1` 的显式假代理凭据。它们不属于要求阻断的 verified/unknown，但 Action 的 GitHub 输出模式仍返回 `183`。由于本次交付禁止重写历史，工作流仅排除无法验证的通用 `URI` detector；当前测试夹具已拆分，不再保存凭据 URI 字面量，并由 `tests/test_security_policy.py` 扫描当前源码与文档中的嵌入式 URI 凭据。其他 detector 和完整历史扫描保持启用。

The first full-history TruffleHog run also found two `unverified URI` results, both explicit fake loopback proxy credentials in historical `tests/test_app.py` revisions. They are outside the verified/unknown blocking scope, but GitHub output mode still returned exit `183`. Because history rewriting is prohibited, only the non-verifying generic `URI` detector is excluded. Current fixtures no longer contain credential-URI literals, and `tests/test_security_policy.py` scans the current source and documentation tree for them. All other detectors and full-history scanning remain enabled.

## 展示材料安全复核 2026-08-30 / Showcase Material Security Review 2026-08-30

- 集成基线 / Integration baseline: `dev-base` at `45ca6247fd63f2a8458be917ffd77e6b3f03261b`
- 审计分支 / Audit branch: `dev/fix/security-audit-showcase-2026-08-30`
- 候审目标 / Review target: `security-check-before-main`

本轮复核覆盖 README badges、中英文导航、公开维护者说明和 Demo 截图。截图由隔离的临时 Kuzu 数据库生成，只使用虚构姓名、不可用于 Steam 的 `demo-*` 标识和生成式占位头像；画面不包含真实 SteamID、关系、凭据、本地数据库路径或个人备注。新增文本敏感标记扫描未发现 17 位 SteamID、本地服务地址、凭据字段或数据库路径。

This review covers README badges, language navigation, public maintainer documentation, and the demo screenshot. The screenshot was generated from an isolated temporary Kuzu database using only fictional names, non-Steam `demo-*` identifiers, and generated placeholder avatars. It contains no real SteamID, relationship, credential, local database path, or personal note. Added-text scanning found no 17-digit SteamID, local service address, credential field, or database path.

审计发现截图内容为 JPEG/JFIF，但最初误用了 `.png` 扩展名；现已修正为 `.jpg`，避免 MIME 和缓存工具误判。最终文件为 `1280x720`、`68,283` 字节，SHA-256 为 `D65A2AC7AF358B2FF169D9ED8A3324E5149AFF234D4CEE4E9FF3A5C108E128D2`。JPEG 只包含 JFIF、量化表、基线帧和 Huffman 表段，不含 EXIF、XMP、IPTC 或注释元数据。

The audit found that the screenshot content was JPEG/JFIF but initially used a `.png` extension. It is now correctly named `.jpg` to avoid MIME and cache-tool ambiguity. The final file is `1280x720`, `68,283` bytes, with SHA-256 `D65A2AC7AF358B2FF169D9ED8A3324E5149AFF234D4CEE4E9FF3A5C108E128D2`. It contains only JFIF, quantization, baseline-frame, and Huffman-table segments, with no EXIF, XMP, IPTC, or comment metadata.

仓库 About、Topics、`v0.1.0` Release、`CONTRIBUTING.md`、顶部中英文互链以及 CI/Python/License/Tests badges 均已复验。`k2316020523` 与 `LiaoYK001` 的 Profile 和 `CZL-Homelab` 公开成员关系可由未登录访客核验。未发现与本次展示改动相关的高风险或中风险问题。

The repository About description, Topics, `v0.1.0` release, `CONTRIBUTING.md`, top-level language links, and CI/Python/License/Tests badges were reverified. The profiles and public `CZL-Homelab` membership of `k2316020523` and `LiaoYK001` are visible to unauthenticated visitors. No high- or medium-risk issue related to this showcase change was identified.

## 剩余风险 / Residual Risk

- Kuzu 是进程内数据库，同一路径仅允许一个持有者；同时启动多个实例会得到明确锁提示，应用不会自动修改数据文件。
- Neo4j 的最短路径和朋友圈功能仍需要插值可变长度语法，但深度在 API 和仓储层均限制为 `1..4`；Kuzu Root 图谱不使用该枚举策略。
- 本工具默认使用本地 HTTP。需要跨不可信网络访问时，必须在可信反向代理后启用 HTTPS，并限制可访问主机。
- 导出文件、截图、SteamID、备注和关系上下文可能包含个人数据，必须作为敏感数据处理。
- 旧 `.env` 密钥读取为兼容行为；推荐迁移到系统凭据库。Steam 隐私设置、限流和 API 可用性仍可能导致图谱不完整。

Kuzu remains single-owner per database path. Neo4j variable-length syntax is bounded to depth four. Local HTTP is not transport encryption and requires a trusted HTTPS reverse proxy for untrusted networks. Exports contain personal relationship data. Legacy `.env` secrets remain a compatibility risk, and Steam privacy or rate limiting can make results incomplete.

## 复验命令 / Reverification Commands

```powershell
uv sync --frozen
uv run --frozen pytest -q
uv run --frozen ruff check src tests
uv run --frozen ruff format --check src tests
uv run --frozen bandit -c pyproject.toml -r src
uv export --frozen --no-dev --format requirements-txt --no-emit-project --output-file requirements-audit.txt
uv run --frozen pip-audit --strict -r requirements-audit.txt
node --check src/steam_friend_relationship_map/static/app.js
node --check src/steam_friend_relationship_map/static/graph-collision.js
node -e "JSON.parse(require('fs').readFileSync('src/steam_friend_relationship_map/static/i18n.json', 'utf8'))"
```

最终结论 / Final assessment: all actionable high- and medium-risk findings identified in this audit are closed, and the permanent audit PR gates pass. The branch is ready to merge into `security-check-before-main`; independent review is still required before any later merge to `main`.
