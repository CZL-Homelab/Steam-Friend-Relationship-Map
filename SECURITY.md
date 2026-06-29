# Security Audit Report / 安全审计报告

## Audit Scope / 审计范围

- Branch: `security-check-before-main`
- Baseline: `main...HEAD`
- Date: 2026-06-30
- Scope: FastAPI API surface, settings and secret storage, Steam API access, Neo4j/Kuzu query layers, frontend DOM rendering, export behavior, logs, documentation, and test coverage.

## Security Checklist / 安全检查项

- Secrets are stored through the system credential store when configured from the UI.
- `/api/settings` only returns configuration metadata and never returns Steam API Key or Neo4j password values.
- State-changing routes are protected by Origin/Referer CSRF checks.
- `.env` writes strip CR/LF characters before persistence.
- Settings payloads use strict validation for graph engine and numeric ranges.
- Secret names are restricted to `steam_api_key` and `neo4j_password`.
- Neo4j and Kuzu queries parameterize user-controlled values; only validated depth values and controlled assignment names are interpolated.
- Friend relationships are merged with `project_id` as part of the relationship identity to avoid cross-project edge reuse or overwrite.
- Frontend HTML rendering escapes user and API-sourced text before inserting markup.
- Logs redact configured secret values, key/password query fragments, Authorization headers, and Cookie headers.
- Exported graph data is local-only and documented as sensitive personal data.

## Findings and Fixes / 发现与修复

### Fixed: CSRF Origin Prefix Bypass

Previous write-request checks used string prefix matching for Origin/Referer. A crafted host such as `http://localhost:8000.evil.example` could pass the prefix test.

Fix: write-request CSRF checks now parse the URL and require an exact allowed hostname plus configured port.

### Fixed: Settings Validation Gaps

`graph_db_engine` accepted arbitrary strings until runtime, and text settings relied only on endpoint-level CR/LF stripping.

Fix: `SettingsPatch` now restricts `graph_db_engine` to `kuzu` or `neo4j` and strips CR/LF from text settings at the schema boundary.

### Fixed: Secret Name Whitelist at Schema Boundary

Secret names were validated by the secret store, but request validation did not express the allowed set.

Fix: `SecretUpdate.name` now accepts only `steam_api_key` or `neo4j_password`.

### Fixed: Frontend Escaping Hardening

Most dynamic HTML already used `escapeHtml`, but single quotes were not encoded and one translated analysis row was inserted without an explicit escape wrapper.

Fix: `escapeHtml` now escapes single quotes and analysis row text is escaped before insertion.

### Fixed: Cross-Project Relationship Merge Pollution

Friend relationships were previously merged only by endpoint pair. When the same Steam user pair appeared in multiple projects, Neo4j could overwrite the relationship `project_id`, while Kuzu could reuse the existing relationship and hide the edge from the later project.

Fix: Neo4j and Kuzu now merge `STEAM_FRIEND` relationships with `project_id` included in the relationship pattern. Tests cover Kuzu cross-project edge isolation and Neo4j query shape.

### Fixed: Dependabot Vulnerability Alerts

GitHub reported Starlette and pydantic-settings alerts through `uv.lock`.

Fix: Dependabot updates were adopted before this final audit. Runtime versions are now `starlette 1.3.1`, `pydantic-settings 2.14.2`, and `fastapi 0.136.3`. The codebase does not use `request.form()`, `Form(...)`, `secrets_dir`, or `request.url.hostname` for security decisions.

### Restored: Formal Security Report

`SECURITY.md` was missing while branch workflow documentation still required updating it before main.

Fix: this report restores the security audit artifact for final review.

## Residual Risk / 残余风险

- This is a local-first tool, but exported CSV/JSON files, screenshots, SteamIDs, notes, and relationship context may still contain personal data. Users must treat exports as sensitive.
- Legacy `.env` values for `STEAM_API_KEY` and `NEO4J_PASSWORD` remain readable for backward compatibility. The UI recommends migration to secure storage and never echoes the raw values.
- Kuzu and Neo4j query languages differ; depth interpolation remains necessary for variable-length path syntax and is limited to validated integer values.
- Steam API availability, privacy settings, and rate limits can make crawl results incomplete.

## Verification / 验证结果

- `uv run pytest`
- `node --check src/steam_friend_relationship_map/static/app.js`
- `uv run python -c "import starlette, pydantic_settings, fastapi; ..."`
- Static scan for shell execution, secret patterns, Cypher interpolation, frontend `innerHTML`, and tracked sensitive files.

## Final Assessment / 最终结论

The branch is ready for independent review before merging into `main`, assuming the verification commands above pass in the reviewer environment and no real secrets or private exported datasets are added before merge.
