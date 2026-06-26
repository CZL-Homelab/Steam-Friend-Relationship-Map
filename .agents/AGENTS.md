# Steam Friend Relationship Map - Workspace Agent Rules

These rules govern all AI coding assistants and developers contributing to this repository. All code modifications, git commits, and workflows must strictly adhere to these instructions.

---

## 1. Branching & Development Workflow

The repository follows a tiered branching model. All feature development must eventually go through a security check branch before merging into `main`.

```
dev-N (Feature Development)
   ↓  PR / merge
dev-base (Integration Branch)
   ↓  Security Audit & Code Normalization
security-check-before-main
   ↓  Final Review (by another person)
main (Production Branch)
```

### Workflow Steps
1. Create a feature branch `dev-N` (or a descriptive name) from `dev-base`.
2. Perform development and submit a PR to merge into `dev-base`.
3. Once features accumulate on `dev-base`, create `security-check-before-main` from it.
4. On `security-check-before-main`, execute:
   - Security audit (according to the template in `SECURITY.md`).
   - Fix any security vulnerabilities.
   - Normalize code formatting and comments.
   - Update the audit report in `SECURITY.md`.
5. Submit `security-check-before-main` for final review by **another person** before merging into `main`.

---

## 2. Commit Message Conventions

All commits must follow the bilingual commit format:
- **Title (First Line)**: Chinese and English bilingual, Chinese first. 
  - **Format**: `feat/fix/chore: 中文简述 / English brief`
  - **Example**: `feat: 新增项目管理隔离功能 / feat: add project management isolation`
- **Body**: One paragraph in Chinese and one paragraph in English, listing the key changes and justifications.

---

## 3. Security & Credentials Rules (Strictly Enforced)

Security is the highest priority. Under no circumstances should sensitive data be committed.

### 3.1 No Hardcoded Secrets
- Do not commit any Steam Web API Key, Neo4j Database Password, Cookies, session tokens, or real user datasets.
- Always use the secure credential storage mechanism in `src/steam_friend_relationship_map/secrets.py` (via `keyring`) for storing Steam API Keys and Neo4j passwords.
- The `.env` file must only store non-sensitive configuration values (e.g., `NEO4J_URI`, `NEO4J_USER`, port numbers).

### 3.2 Log Redaction
- All application and development logs must pass through the redaction handler (`AppLogBuffer` in `src/steam_friend_relationship_map/logs.py`) to scrub keys like `steam_api_key`, `neo4j_password`, cookies, and auth headers.

### 3.3 Data Privacy
- Only query public data via official Steam Web APIs. Do not scrape or attempt privilege escalation.
- Mark private profile friends list states as `private` and skip expansion.

---

## 4. Development & Code Quality Guidelines

- **Dependency Management**: Use `uv add` to add dependencies. Do not manually edit `pyproject.toml` or `uv.lock`.
- **Internationalization (i18n)**: All frontend texts, labels, and notifications must be defined dynamically in `src/steam_friend_relationship_map/static/i18n.json` in both English and Chinese.
- **Code Style**: 
  - Python: Adhere to PEP 8.
  - JavaScript: Use camelCase.
  - **Preservation**: Always preserve existing unrelated comments and docstrings when editing files.
- **Testing**: Run `pytest` to verify all test suites pass before wrapping up code changes.
