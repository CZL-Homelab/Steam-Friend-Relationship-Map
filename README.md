# Steam Friend Relationship Map

一个本地运行的 Steam 好友关系图谱工具：输入公开 Steam 用户主页 URL，按 1-4 层抓取公开好友关系，写入 Neo4j Desktop，并用本地 Web GUI 查看头像卡片、备注、路径和关系网。

## Quick Start

1. 复制配置文件：

   ```powershell
   Copy-Item .env.example .env
   ```

2. 编辑 `.env`，填入 Steam Web API Key 和 Neo4j Desktop 的 Bolt 连接信息。

3. 同步依赖并启动：

   ```powershell
   uv sync
   uv run steam-friend-map
   ```

4. 打开浏览器访问：

   ```text
   http://127.0.0.1:8000
   ```

## Notes

- v1 只使用公开 Steam Web API，不读取 Cookie，不绕过隐私设置。
- 深度限制为 1-4，用户上限限制为 1-10000。
- 默认显示最多 2000 个节点；更大的图建议在 Neo4j Bloom 中分析。

