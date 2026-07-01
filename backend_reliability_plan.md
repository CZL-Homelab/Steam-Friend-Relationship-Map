# 后端可靠性自愈与请求重试开发计划 / Backend Resilience & Retry Implementation Plan

本计划针对图数据库的物理损坏隐患以及 Steam 接口网络超时问题，提出后端可靠性自愈与重试机制的开发方案。

---

## 1. Kùzu 数据库文件锁异常自愈 (Kuzu DB Self-Healing)

### 1.1 痛点诊断
Kùzu 数据库采用单进程排他文件锁机制。若 FastAPI 服务通过强制杀死进程退出（如 `kill -9` 或控制台强断），Kùzu 的独占锁和 WAL 日志在未优雅释放时极易造成文件损坏，导致下次服务拉起时抛出 `IO exception` 无法启动。

### 1.2 设计方案
在 FastAPI 启动的 `lifespan` 钩子或 `kuzu_repo.py` 初始化时，包裹异常捕捉。若触发损坏报错，执行**崩溃归档自愈（Self-Healing Archive）**：

```python
# 伪代码思路 / Pseudo Code
import os
import shutil
import logging
from datetime import datetime

def init_kuzu_database(db_path: str):
    try:
        # 尝试正常建立 Kùzu 连接
        return kuzu.Database(db_path)
    except Exception as e:
        if "IO" in str(e) or "lock" in str(e).lower() or "corrupted" in str(e).lower():
            backup_path = f"{db_path}_corrupted_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            logging.error(f"Kuzu DB corrupted! Archiving to {backup_path} and recreating...")
            
            # 自动归档损坏目录，防止数据丢失
            if os.path.exists(db_path):
                shutil.move(db_path, backup_path)
                
            # 原地重建空数据库目录并重新载入
            os.makedirs(db_path, exist_ok=True)
            return kuzu.Database(db_path)
        raise e
```

---

## 2. Steam API 请求指数退避重试 (Exponential Backoff Retries)

### 2.1 痛点诊断
在国内网络环境下，即使配置了代理，Steam 社区 API (API Key) 的访问仍可能面临偶发性握手超时（504 / Connection Timeout）。一旦单次请求抛错，爬虫直接中断，导致当前的抓取任务整体报废。

### 2.2 设计方案
引入 `tenacity`（Python 成熟重试库），为 `steam.py` 中的网络请求接口配置带随机抖动的指数退避重试（Exponential Backoff with jitter），既能避开瞬时抖动，又不会被 Steam Rate Limiter 速率封锁：

```python
from tenacity import retry, stop_after_attempt, wait_random_exponential

@retry(
    wait=wait_random_exponential(min=1, max=10), # 指数延迟 1s, 2s, 4s 并带随机抖动
    stop=stop_after_attempt(3),                   # 最多重试 3 次
    reraise=True                                  # 重试均失败时向上传播异常
)
def fetch_steam_friends_with_retry(steam_id: str, api_key: str):
    # 执行实际的 HTTP 抓取逻辑
    ...
```

---

## 3. 爬虫数据分段持久化与断点续爬 (Segmented Saving & Checkpoints)

### 3.1 痛点诊断
目前 `crawler.py` 采用“全部抓取完毕后一次性统一保存”的逻辑。若在抓取深度为 3 的过程中，第 800 个节点发生网络彻底中断，已成功抓取的 799 个节点的数据会全部丢失，容错率极低。

### 3.2 设计方案
1. **分层分段保存（Layer Checkpoints）**：
   - 爬虫由“一次性保存”重构为“每层抓取完毕”或“每抓取 N 个节点（如每 20 个）”立即调用一次 `kuzu_repo.save_nodes_and_relationships()` 进行一次事务提交。
   - 确保即使爬虫在深层意外崩溃，已抓取的浅层社交网络成果也已被安全写入 Kùzu / Neo4j 数据库。
2. **断点续爬支持**：
   - 在数据库中维护抓取日志，记录已完成 learnings。
   - 重新启动爬虫时，自动对比当前项目已落盘的节点，跳过已完成部分，直接继续往下爬取未完成的节点。
