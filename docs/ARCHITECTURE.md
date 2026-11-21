# 架构与不变量

这份文档回答三个问题：**谁拥有什么数据**、**一张图会经历哪些状态**、**哪些失败必须
保守处理**。实现细节见 `wallhaven_sync.py`，用户命令见 `docs/CAPABILITIES.md`。

## 1. 设计目标

1. 池子目录始终是 Noctalia 能直接扫描的普通目录，不引入软链、FUSE 或自建轮换器。
2. active 保持 `target`（默认 365）张，磁盘约 1.4 GiB，不随使用时间增长。
3. 已看过的图不反复占位；每天用新图替换一批旧图。
4. 任何会自动删文件的路径都必须能回答：**当前屏幕上的图绝不会被删**。
5. 网络失败、API 失败、数据库损坏都不能导致池子被清空或索引被大面积误删。

## 2. 数据所有权

| 数据 | 位置 | 谁是权威 | 说明 |
| --- | --- | --- | --- |
| active 图片 | `<pool>/wallhaven-<id>.<ext>` | 文件系统 + `index.json` | Noctalia 只读这个目录 |
| active 元数据 | `index.json` | **权威** | 下载时写入，逐张原子落盘 |
| 元数据库 | `library.db` | active 的镜像；evicted 的权威 | SQLite，可随时从 index 重建 active 部分 |
| 原始展示日志 | `history.jsonl` | 只追加 | 钩子 best-effort，便于排查/审计 |
| 展示计数 | `library.db` | **权威** | `record` 钩子写入；`index.json` 不存计数 |
| 回收站 | `<pool>-trash/YYYYMM/` | 文件系统 | 7 天后清理，DB 保留元数据 |
| 隔离区 | `<pool>-quarantine/duplicates/` | `index.json` | pHash 重复，不参与轮换 |

**为什么是混合方案**：`index.json` 已经驱动采集、品控和 Noctalia 目录，改动面最小；
SQLite 负责查询、历史、淘汰状态和 pin。两者通过 `mirror_index_to_db()` 单向同步：
index 增删 → DB 跟随；DB 的 `shown_count` / `pinned` / `evicted_*` 不会被镜像覆盖。

## 3. 图片状态机

```
                 add / sync / restore
        ┌──────────────────────────────────┐
        │                                  ▼
   [active] ──replenish──▶ [evicted] ──7d──▶ 文件删除（DB 记录保留）
        │                     │
        │                     └──restore──▶ [active]（回收站或重新下载）
        ├──dedupe──▶ [quarantine] ──restore──▶ [active]
        └──ban────▶ [banned]（文件立即删除，不可 restore）
```

- `active`：在池目录里，参与 Noctalia 轮换。
- `evicted`：已移出池目录；文件在回收站或已清理；**ID 和 pHash 永久阻止再次下载**。
- `quarantine`：视觉重复，只保留一份；不参与轮换。
- `banned`：用户明确拉黑，永久不再下载。

DB 的 `status` 是 evicted/quarantine/banned 的唯一权威。`index.json` 只保留 active。

## 4. 每日滚动窗口（replenish）

顺序经过刻意设计，保证“补货失败时保留旧图”：

```
1. 清理超过 N 天的回收站
2. 计算今日配额 = max_per_day - 今天已淘汰数（本地日期）
3. 选候选旧图：
     - 只选 active、非 pinned、非当前壁纸、最近 2h 没展示过
     - 按 last_shown_at 从旧到新
     - 从未展示过的图默认不选（除非 --evict-unshown）
4. 以 target + 候选数 为临时目标补货（允许短暂超出 target）
5. 只淘汰“实际补到的新图数量”那么多的旧图
     - 补到 0 张 → 不淘汰
     - 补到 10 张 → 只淘汰 10 张，池子回到 target
6. 移动文件到回收站，更新 index + DB
7. 对淘汰的图调用 /w/{id} 补 tags（≤24 次/天）
8. 把 index.target 恢复为真实 target
9. 硬容量保护：active 超过 target 或池子超过 max_bytes 时，
   无视每日配额继续淘汰（仍保护当前壁纸/pin/最近展示），并清理 24h 以上的索引外文件
```

**容量上限**：`max_bytes`（默认 3 GiB）是独立于 `target` 的硬上限。`sync` 在下载前
同时检查张数和字节数；`replenish` 在补货后如果发现超限，会额外淘汰到 target/字节数以下。
每日 24 张只是“常规换血速度”，不是唯一的容量保护。

**为什么先补后删**：如果先删后补，网络中断会让池子缩水；反过来最多只是临时多占
24 张图（约 100 MiB），失败时旧图原样保留。

**为什么默认不淘汰“从未展示”**：新补进来的图还没轮到展示就被删掉，等于白下载。

**为什么保护最近 2 小时**：`noctalia msg wallpaper-get` 只返回默认输出，多显示器时
可能漏掉某块屏；时间窗口是第二道保险。

**幂等性**：同一天重复登录/重复 timer 不会超量淘汰。配额按**本地日期**统计
`evicted_reason='rotation'` 的记录（DB 内时间仍是 UTC，查询时换算）。

## 5. 采集来源

1M 榜单只有约 591 个可用候选，无法支撑每天 24 张的滚动。因此来源是：

```
Toplist 1M/3M/6M/1y
  × 纯榜单
  × 8 个标签（landscape/city/abstract/minimal/space/nature/architecture/cyberpunk）
Latest（sorting=date_added）
  - 收藏 ≥100、浏览 ≥2000
```

- `categories=100`：只 general（用户选择）。
- 标签查询用 `q=+tag`（必须含该标签）。
- 每次请求间隔由 `api_get()` 统一限速（1.4s，约 43 次/分钟）；429 优先遵守
  `Retry-After`，其余 5xx/网络错误指数退避 + 抖动（最多 5 次）。
- 下载同样走 `.part` → 校验 → `fsync` → `os.replace`，并独立重试 5 次。
- `replenish` 默认每个来源只翻 2 页，并按日期轮换起始页（0–19），避免每天都从
  第一页开始、反复扫描已经全部见过的候选。
- `sync`（首次填充/手动）从第 1 页开始，可以深翻。

## 6. 去重

三层，顺序固定：

| 层 | 依据 | 对 active | 对 DB 中 evicted/quarantine/banned |
| --- | --- | --- | --- |
| 1 | Wallhaven ID | 跳过 | **跳过**（不再下载） |
| 2 | SHA-256 | 记 `duplicates` | 记为 `rejected: duplicate-of-archived` |
| 3 | pHash ≤3 | 记 `duplicates` | 记为 `rejected: near-duplicate-of-archived` |

4–7 的相似图照旧只报告、不自动删。DB 里保留所有历史 pHash，所以滚动窗口不会把
“两年前淘汰过的同一张图”重新下载回来。

## 7. 失败模式与安全阀

| 失败 | 行为 |
| --- | --- |
| `noctalia msg wallpaper-get` 失败 | **不淘汰**，除非显式 `--force` |
| 池目录缺失 >50% | 拒绝清空索引，需 `--force-prune` |
| 下载失败/超大/无法解码 | 跳过该候选，不中断整次运行 |
| API 429 / 5xx / 网络抖动 | 指数退避 + 抖动，优先 `Retry-After`；5 次后放弃该候选 |
| 下载中途断开 / 0 字节 | 只在 `.incoming-*.part` 里写，校验 + `fsync` 后才改名；不会进池 |
| 池子超过 `max_bytes` / target | 停止下载；`replenish` 无视每日配额硬淘汰到上限以下 |
| 索引写入失败 | 淘汰时把文件移回原处并保留索引条目 |
| DB 写入失败 | warning；index 仍是权威，下次镜像会修复 |
| 钩子写 DB 超时 | 1 秒超时后放弃，绝不影响换壁纸 |
| 回收站文件被清理 | DB 保留元数据，`restore` 走重新下载 |
| 当前壁纸无法确定 | 保守停止淘汰 |

并发：`sync`/`replenish`/`ban`/`pin`/`restore`/`dedupe` 共用 `flock` 排他锁；
`record` 钩子不拿锁（只写 JSONL + 1 秒 SQLite 事务），保证换壁纸不被阻塞。

## 8. 备份与恢复

- **必须备份**：`~/.local/state/wallhaven-sync/`（`index.json`、`library.db`、
  `history.jsonl`）。图片本身可以重新下载，元数据不行。
- 已在 `~/code/.dsh/tools/userdata-backup.sh` 的复制清单里。
- SQLite 使用 WAL；复制时把 `library.db-wal` / `library.db-shm` 一起复制（整个
  目录 `cp -a` 即可），SQLite 恢复时会自动重放。

## 8.5 换机与多设备

- 备份只包含 state（`index.json`/`library.db`/`history.jsonl`）和 API key，不包含
  1.4 GiB 图片。
- 新机器恢复 state 后，池目录为空；`rehydrate` 按 active ID 重新下载，保留同一批图、
  pin 和展示计数。不要用 `sync --force-prune` 代替，它会丢掉 pin 并重新选图。
- **不支持两台设备实时共用一个 DB**：SQLite/`flock`/原子 rename 都假设本地文件系统；
  Syncthing/NFS 上会锁失效、路径不一致、两边 replenish 互相覆盖。
- 多设备推荐：各自独立 state；用 collections 做共同保护名单；迁移用备份快照 +
  `rehydrate`。同一 API key 可以共用，但 45 次/分钟是共享额度。

## 9. 明确不做

- 不实现第二个轮换器；Noctalia 的 `ShuffleState` 已经无放回。
- 不自动收藏到 Wallhaven：API 没有收藏写入接口。
- 不通过 `/favorites` 同步收藏：**该端点返回 404**；只有 collections 可读，且需要
  账号用户名（`wallhaven-sync collections --username`）。
- 不做图片内容识别/水印检测；误删成本高于多留一张。
