# 参与贡献 / Contributing

感谢你愿意帮助改进 Steam Friend Relationship Map。本项目欢迎缺陷报告、文档改进、测试、性能优化和经过充分说明的功能提案。

Thank you for helping improve Steam Friend Relationship Map. Bug reports, documentation improvements, tests, performance work, and well-scoped feature proposals are welcome.

## 提交 Issue / Filing an Issue

提交 Issue 前，请先搜索现有 Issue 和 PR，避免重复。缺陷报告应尽量包含：

- 操作系统、Python、`uv` 和浏览器版本。
- 使用的图数据库引擎（Kuzu 或 Neo4j）及其版本。
- 最小复现步骤、预期结果和实际结果。
- 已脱敏的错误日志或截图。

Before opening an issue, search existing issues and pull requests. A useful bug report includes:

- Operating system, Python, `uv`, and browser versions.
- Graph database engine (Kuzu or Neo4j) and its version.
- Minimal reproduction steps, expected behavior, and actual behavior.
- Sanitized logs or screenshots.

不要公开提交 Steam API Key、密码、Cookie、Authorization Header、代理凭据、真实 SteamID、关系图谱、个人备注或数据库备份。请使用占位符和最小化的模拟数据。

Never post Steam API keys, passwords, cookies, authorization headers, proxy credentials, real SteamIDs, relationship graphs, private notes, or database backups. Use placeholders and minimal synthetic data.

## 安全问题 / Security Reports

不要通过公开 Issue 报告安全漏洞。请遵循 [SECURITY.md](SECURITY.md) 中的私密报告方式，或直接使用 [GitHub Private Vulnerability Reporting](https://github.com/CZL-Homelab/Steam-Friend-Relationship-Map/security/advisories/new)，并在报告中使用脱敏或模拟数据。隐私与数据处理约束见 [PRIVACY.md](PRIVACY.md)。

Do not report vulnerabilities in a public issue. Follow the private reporting guidance in [SECURITY.md](SECURITY.md), and use sanitized or synthetic data. See [PRIVACY.md](PRIVACY.md) for data-handling expectations.

## 开发环境 / Development Setup

项目要求 Python 3.12+，并使用 `uv` 管理依赖：

```powershell
git clone https://github.com/CZL-Homelab/Steam-Friend-Relationship-Map.git
cd Steam-Friend-Relationship-Map
uv sync --frozen
uv run steam-friend-map
```

默认 Kuzu 引擎无需外部数据库。不要把本地 `.env`、凭据、数据库文件或导出数据加入 Git。

The project requires Python 3.12+ and uses `uv` for dependency management. The default Kuzu backend does not require an external database. Never add local `.env` files, credentials, databases, or exports to Git.

## 分支流程 / Branch Workflow

所有改动从最新 `dev-base` 开始，不直接向 `main` 开发：

```text
dev/feat/*, dev/fix/*, dev/chore/*
   -> dev-base
   -> security-check-before-main
   -> independent review
   -> main
```

维护者的历史编号分支（例如 `dev-l-10`）可以继续存在；新的外部贡献建议使用：

- `dev/feat/<topic>`：新功能。
- `dev/fix/<topic>`：缺陷修复。
- `dev/chore/<topic>`：文档、测试、依赖或维护工作。

Start every change from the latest `dev-base`; do not develop directly against `main`. Historical numbered maintainer branches such as `dev-l-10` may remain, while new contributions should use `dev/feat/*`, `dev/fix/*`, or `dev/chore/*`.

## 测试与质量门禁 / Tests and Quality Gates

提交 PR 前至少运行：

```powershell
uv run --frozen pytest -q
uv run --frozen ruff check src tests
uv run --frozen ruff format --check src tests
uv run --frozen bandit -c pyproject.toml -r src
node --check src/steam_friend_relationship_map/static/app.js
node --check src/steam_friend_relationship_map/static/graph-collision.js
node -e "JSON.parse(require('fs').readFileSync('src/steam_friend_relationship_map/static/i18n.json', 'utf8'))"
```

如果改动依赖，请重新生成锁文件，并运行运行时依赖审计：

```powershell
uv lock
uv export --frozen --no-dev --format requirements-txt --no-emit-project --output-file requirements-audit.txt
uv run --frozen pip-audit --strict -r requirements-audit.txt
```

不要提交 `requirements-audit.txt`。GitHub Actions 会在 Windows 和 Ubuntu 上重新运行完整测试、静态检查、依赖审计和密钥扫描；所有门禁必须通过。

Do not commit `requirements-audit.txt`. GitHub Actions repeats the full suite, static checks, dependency audit, and secret scan on Windows and Ubuntu. Every required gate must pass.

## Pull Request 要求 / Pull Request Expectations

PR 应以 `dev-base` 为目标，并做到：

- 聚焦一个可审阅的问题，不混入无关重构。
- 说明动机、行为变化、风险和验证方式。
- 为行为变更补充或更新测试。
- 保持 Kuzu 与 Neo4j 的项目隔离和参数化查询语义。
- 新增界面文案时同步更新中文和英文 i18n。
- 不降低输入限制、日志脱敏、Origin/CSP 或密钥存储保护。
- 确认没有提交真实用户数据或凭据。

Pull requests should target `dev-base`, stay focused, explain motivation and risk, include tests for behavioral changes, preserve both graph backends, update both languages, and retain the existing security boundaries.

## Commit 规范 / Commit Convention

提交标题和正文使用中英双语、中文在前：

```text
fix: 修复示例问题 / fix the example issue

中文正文说明关键改动和原因。

English body describing the key changes and rationale.
```

保留有意义的提交历史，不为方便而重写已经共享的历史。

Use a meaningful bilingual history and do not rewrite commits that have already been shared.

## Review 与发布 / Review and Releases

维护者会根据可复现性、隐私影响、兼容性、测试覆盖和维护成本进行 Issue triage 与 PR review。进入 `main` 前，集成内容必须经过安全审计，并由另一位人员完成最终 review。

正式 Release 只从 `main` 创建，必须对应明确版本、Tag、变更说明和已通过的质量门禁；开发分支不用于正式发布。

Maintainers triage and review work based on reproducibility, privacy impact, compatibility, test coverage, and maintenance cost. Integration changes receive a security audit and independent final review before `main`. Formal releases are created only from `main`, with a versioned tag, release notes, and passing quality gates.
