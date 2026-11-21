# 能力说明（当前实现）

这份文档描述 `wallhaven-noctalia` **现在已经能做到什么**，以及不做什么。
架构、数据所有权和失败模式见 [ARCHITECTURE.md](ARCHITECTURE.md)。

核心仍然是 **365 张本地池 + Noctalia 无放回轮换**，新增的是 **滚动窗口**：
每天淘汰一批已经看过的旧图，补入新图，active 始终约 365 张。

## 一条数据流

```
wallhaven API（Toplist 1M/3M/6M/1y + 标签 + Latest）
        │
        ├─ API 初筛：SFW / general / 16:9 / ≥1920x1080 / ≤16 MiB / 收藏·浏览门槛
        │
        ├─ 下载 → SHA-256 去重（含已淘汰历史）
        │
        ├─ stats + Noctalia 调色：亮度/层次/饱和度、m3-content 的 primary
        │
        ├─ pHash：≤3 自动拒绝（含已淘汰历史），4–7 留给 dedupe 报告
        │
        ├─ judge：亮度 / 层次 / 饱和度 / 色相配额
        │
        ├─ index.json（active 元数据，权威）
        │        └── mirror ──▶ library.db（active 镜像 + evicted 历史 + 展示计数）
        │
        └─ 本地池 ~/Pictures/wallhaven/
                    │
              Noctalia wallpaper.automation
              （random，无放回洗牌）
                    │
              wallpaper_changed hook
                    │
              history.jsonl + library.db（展示历史/计数）
                    │
              每日 replenish
                    │
              旧图 → -trash/YYYYMM/ → 7 天后删除
              新图 → 池子（保持 target）
```

## 1. 采集

- 来源四档 Toplist：`1M → 3M → 6M → 1y`。
- 每档再叠加 8 个标签检索：`landscape`、`city`、`abstract`、`minimal`、
  `space`、`nature`、`architecture`、`cyberpunk`（`q=+tag`）。
- 另加 `Latest`（`sorting=date_added`），要求收藏 ≥100、浏览 ≥2000。
- 只 general（`categories=100`），SFW，16:9，至少 1920×1080，≤16 MiB。
- 目标张数默认 365；首次运行补满，之后由 `replenish` 维持。
- 首次全量约 1.4 GiB / 2 小时；可中断，索引逐张落盘。
- API 用 `X-API-Key` 头；所有端点统一限速 1.4s/次；429 优先遵守 `Retry-After`，
  5xx/网络错误指数退避 + 抖动（最多 5 次）。
- 下载先写 `<pool>/.incoming-<id>.part`，校验（SHA/pHash/品控）+ `fsync` 通过后
  才 `os.replace` 成正式文件名；半张图、0 字节文件不会进池。
- 单个来源失败只跳过该来源，不影响后面的来源。
- 断点续跑、原子写索引、`fsync`、并发锁。
- 下载失败/过大/无法解析不会中断整次同步。

## 2. 去重

三层：

| 级别 | 依据 | 行为 |
| --- | --- | --- |
| 1 | wallhaven ID | 已存在直接跳过；**已淘汰/隔离/拉黑的 ID 也不再下载** |
| 2 | SHA-256 | 文件字节完全相同的另一个 ID，记入 `duplicates`；与历史图相同则永久拒绝 |
| 3 | pHash | 视觉相似的另一个 ID，≤3 自动拒绝（含历史图）；4–7 在 sync 里接受、由 `dedupe` 报告 |

pHash 算法：

- ImageMagick 缩到 **16×16** 灰度；
- 纯 Python 二维 DCT（无 numpy）；
- 取左上 8×8 低频，按中位数生成 64 位指纹；
- 汉明距离比较。
- 这是 **16×16 变体**（不是标准 imagehash 的 32×32），3/7 阈值按本实现标定，
  不能直接套用文献阈值。

在现有 365 张上实测：

- 距离 2：同一张 “OUTLAWS FOR LIFE”，不同压缩/饱和度 → 已自动隔离；
- 距离 4：同一场景不同景深 → 只报告，不删除；
- 距离 6：待确认；
- 距离 ≥8：视为不同图片。

`dedupe` 的默认阈值是 7，自动隔离阈值是 3。**4–7 永远不自动删**，宁可多留一张。
`sync` 只对 ≤3 做拒绝；4–7 会正常入池，之后由 `dedupe` 报告。

## 3. 品控

| 检查 | 阈值 | 说明 |
| --- | --- | --- |
| 亮度 | 0.03 – 0.70 | 太黑/太白 |
| 层次（标准差） | ≥ 0.03 | 纯色/死板 |
| 饱和度 | ≥ 0.06 | 灰图 |
| 主色 chroma | ≥ 10 | 调色板太闷 |
| 闷图配额 | ≤ 10% | 灰图/低 chroma 总量 |
| 色相配额 | 每 30° 一格，单格约 `target/12*1.5`；池子 <85% 时生效 | 避免全蓝/全橙 |
| 收藏/浏览 | 可配 `--min-favorites` / `--min-views`；Latest 固定 ≥100/≥2000 | 控制来源质量 |
| 文件大小 | ≤ 16 MiB | |
| 格式 | 拒绝 GIF/AVIF | 动图/解码依赖 |

**软拒绝**：`hue-quota` / `muted-quota` 不写永久黑名单；第一遍结束后如果没满，
自动进入宽松模式（只保留亮度/层次/大小硬过滤）再补一遍。

**硬拒绝**：太亮/太暗/太平/太大/格式不支持/pHash 重复，写进 `rejected`/`duplicates`，
不再重试。

## 4. 展示历史与元数据库

`wallpaper_changed` 钩子：

```toml
[hooks]
wallpaper_changed = "wallhaven-sync record"
```

Noctalia 提供 `NOCTALIA_WALLPAPER_PATH` / `NOCTALIA_WALLPAPER_CONNECTOR`。
`record` 做两件事：

1. 追加一行 JSONL 到 `~/.local/state/wallhaven-sync/history.jsonl`；
2. 更新 `library.db`：`shown_count`、`first_shown_at`、`last_shown_at`、
   `display_history`。SQLite 写超时 1 秒，任何失败都只 warning。

```json
{"at":"2026-11-21T02:28:53+00:00","connector":"eDP-1","id":"w5327p","in_pool":true,"path":"...","source":"noctalia"}
```

- 自动轮换、`wallpaper-next`、手动在 Noctalia 里选图都会记录；
- 多显示器按 connector 分别记录；
- 配置重载/重复 apply 同一路径会写历史，但不会重复增加 `shown_count`；
- 非池子路径（临时换的外部壁纸）也会记录，但 `in_pool: false`，不写 DB；
- O(1)、不联网、不读图。

> Noctalia 通过进程级 `setenv` 给 hook 传环境变量，再异步 fork；理论上存在极小的
> 竞态。history 与 DB 计数都按 **best-effort** 记录；轮换状态仍以 Noctalia 的
> `wallpaper_shuffle.json` 为准。

`history` 查看最近记录；`report` / `status` 统计已看/未看、最常展示、来源、色相、
磁盘、已淘汰数量、pin、回收站。

## 5. 滚动窗口（replenish）

每天（以及登录 5 分钟后）运行一次：

```
选旧图（最久没展示、非 pin、非当前壁纸）→ 先补新图到 target+N
→ 只淘汰真正补到的数量 → 移到 -trash/YYYYMM/ → DB 标记 evicted
```

- 默认 `--max-per-day 24`，约 3 GB/月；同一天重复运行不会超量。
- **当前正在显示的壁纸永不淘汰**：通过 `noctalia msg wallpaper-get` 确认；无法确认时
  默认跳过淘汰（`--force` 才继续）。最近 2 小时展示过的也跳过。
- 从未展示过的图默认不淘汰（`--evict-unshown` 可改）。
- 补货失败时一张旧图都不删。
- **硬容量上限 `max_bytes`（默认 3 GiB）**：`sync` 到上限停止下载；`replenish` 发现
  active 超过 target 或池子超过字节上限时，无视每日配额继续淘汰（仍保护当前壁纸、
  pin、最近展示），并把 24h 以上的索引外文件移到回收站。
- 被淘汰的 ID 和 pHash 留在 DB，后续不会重新下载。
- `pin <id>` 的图永不淘汰；`restore <id>` 可从回收站或 Wallhaven 恢复。
- Noctalia 每次挑图都会把 `seen` 与当前目录求交集，所以删掉旧文件不会破坏轮换状态。

## 6. 命令

```bash
wallhaven-sync replenish               # 每日滚动窗口（timer 调用的就是它）
wallhaven-sync replenish --dry-run     # 只显示淘汰计划和候选
wallhaven-sync replenish --max-per-day 12
wallhaven-sync replenish --evict-unshown   # 允许淘汰从未展示的图

wallhaven-sync sync                    # 只补齐到 target（不淘汰）
wallhaven-sync sync --dry-run          # 只列候选，不写索引/不下载
wallhaven-sync sync --retry-rejected   # 调阈值后重新评估硬拒绝
wallhaven-sync sync --retry-duplicates # 重新评估 pHash/SHA 重复
wallhaven-sync sync --force-prune      # 池子大量缺失时仍继续（危险）

wallhaven-sync status [--json]         # 数量、磁盘、色相、DB、回收站、轮换进度
wallhaven-sync doctor                  # key/pool/DB/钩子/Noctalia/Umbriel/最近日志
wallhaven-sync backfill                # 回填旧索引缺少的 URL/收藏数/标签
wallhaven-sync db-checkpoint           # 合并 SQLite WAL（备份前）
wallhaven-sync preview <文件|id>       # 终端色块 + 亮度/饱和度/主色
wallhaven-sync add <id|url> [--force]  # 手动加一张；仍做 SHA/pHash 去重
wallhaven-sync ban <id>                # 删除并永久拉黑
wallhaven-sync pin <id> / unpin <id>   # 保护/取消保护
wallhaven-sync pins                    # 列出 pin 的图
wallhaven-sync restore <id>            # 从回收站或重新下载恢复
wallhaven-sync trash [--purge --days N] # 查看/清理回收站
wallhaven-sync collections --username NAME  # 收藏夹里的图全部 pin（只读）

wallhaven-sync dedupe [--apply]        # pHash 去重；默认只报告
wallhaven-sync dedupe --threshold 8    # 放宽报告阈值
wallhaven-sync dedupe --list-quarantine
wallhaven-sync dedupe --restore <id>
wallhaven-sync history [--limit N]     # 展示历史
wallhaven-sync report                  # 图库统计报告
wallhaven-sync record                  # 给 Noctalia 钩子用，手动跑一般不需要
```

`dedupe` 默认 **只报告**，并顺便把缺失的 pHash 缓存进 `index.json`（元数据，不动图片）。
`--apply` 会把距离 ≤ `--auto`（默认 3）的重复移到
`<池子目录>-quarantine/duplicates/`。只有两边文件都存在时才移动；只有一侧存在时跳过该 pair。
隔离区在池子外面，`recursive=true` 也不会被 Noctalia 扫到。被隔离的 ID 不会再次下载；
`dedupe --restore <id>` 可以恢复。

> **收藏夹限制**：Wallhaven API 没有 `/favorites`（实测 404），星标收藏既不能读也不能写；
> 只有 collections 可读（API 全部只读，不能自动收藏）。列出一个 collection 的内容还需要
> 账号用户名（API 不返回）。所以：
>
> - 设置 `services.wallhaven.username`（或 `WALLHAVEN_USERNAME`）后，每日 `replenish`
>   会自动读取 collections 并 `pin` 其中的图；
> - 手动运行 `wallhaven-sync collections --username <名字>`；
> - 也可以直接用 `pin` 手动保护。
> - **点星标不算保护**，要把图加入 collection。
> - 把图移出 collection 不会自动 `unpin`；需要手动 `wallhaven-sync unpin <id>`。

## 7. Noctalia 侧

采集器不接管轮换；Noctalia 负责：

```toml
[wallpaper]
directory = "/home/user/Pictures/wallhaven"

[wallpaper.automation]
enabled = true
interval_seconds = 3600
order = "random"
recursive = false
pause_on_fullscreen = true
```

Noctalia 5.5 的 `ShuffleState` 是**无放回**洗牌：365 张全部看过才开下一轮。
`pick()` 每次都会 `erase_if(seen, !candidateSet.contains(path))`，所以淘汰旧文件后，
`wallpaper_shuffle.json` 里的死路径会自动消失，新文件会在下一轮进入候选。
所以本项目不需要 `active/` 软链或缓存管理器；池子目录就是轮换队列。

## 8. 存储布局

```
~/.config/wallhaven/apikey                  # 600，不进 git
~/.local/state/wallhaven-sync/
├── index.json                              # active 元数据（含 pHash），权威
├── library.db                              # SQLite：元数据/历史/淘汰/pin
├── library.db-wal / -shm                   # WAL（备份时一起复制）
├── history.jsonl                           # 原始展示历史
└── lock                                    # 同步锁

~/Pictures/wallhaven/                       # 365 张原图
├── wallhaven-abc123.jpg
└── ...

~/Pictures/wallhaven-trash/202611/          # 淘汰回收站，7 天后清理
~/Pictures/wallhaven-quarantine/
└── duplicates/                             # dedupe 隔离的重复图
```

`index.json` 每张 active 记录：

- `file`、`source`、`theme`、`added_at`、`resolution`、`bytes`、`file_type`；
- `width` / `height`、`url` / `thumb_url`、`favorites` / `views`、
  `category` / `purity`、`created_at`；
- `sha256`、`phash`；
- `stats`（亮度/层次/饱和度）、`palette`（primary/hue/chroma）；
- 全局：`rejected`、`duplicates`、`banned`、`quarantine`。

`library.db` 的 `images` 表额外有 `status`、`shown_count`、`first_shown_at`、
`last_shown_at`、`pinned`、`evicted_at`、`evicted_reason`、`trash_path`、`tags`；
`display_history` 存每次展示事件。

## 9. systemd

Home Manager 模块提供：

- `wallhaven-sync.service`（oneshot，`Nice=10`，`TimeoutStartSec=6h`），
  `ExecStart = wallhaven-sync replenish --max-per-day 24 --keep-trash-days 7`；
- `wallhaven-sync.timer`：每天一次 + 登录 5 分钟后一次，`Persistent=true`；
- 首次运行会补满整个池子，之后才是每日滚动；
- 设置了 `keyFile` 时，`ConditionPathExists` 会让没有 key 的服务直接跳过。

## 10. 不做的事

| 不做 | 原因 |
| --- | --- |
| 自动收藏到 Wallhaven 账号 | 官方 API 没有公开的收藏写入接口 |
| 用 `/favorites` 同步收藏 | 端点不存在（404）；只能读 collections，且需要用户名 |
| All-time 排行榜 | API 不支持（`all` 实测退化成 1M） |
| 无门槛 Latest | 质量太低（0 收藏/5 浏览）；已加 ≥100 收藏 / ≥2000 浏览 |
| 50–100 张缓存管理器 | Noctalia 已经无放回轮换 365 张；缓存会打乱 seen 状态 |
| CLIP / 水印 / 构图评分 | 误判成本高，不适合自动删除 |
| 分类池 / 可视化 UI | Noctalia 不是为多分类池设计的 |
| 自动删除 4–7 距离的相似图 | 误删比多留一张更糟 |
| 自动删除当前壁纸 | 会和 Noctalia 抢文件；永远保留 |

## 11. 排障

- **history 为空**：确认 `wallpaper_changed` 钩子已启用；手动跑一次
  `NOCTALIA_WALLPAPER_PATH=... wallhaven-sync record` 看 `history.jsonl`。
- **DB 没有记录**：`wallhaven-sync doctor` 看 `library` 段；跑一次
  `wallhaven-sync sync`（池满也会镜像 index 到 DB）。
- **pHash 重复被拒绝**：被 `sync` 拒绝的图不在池子里，`dedupe` 看不到；
  用 `wallhaven-sync report` 看 `duplicates`（id / of / 距离），或 `sync --retry-duplicates` 重评。
- **dedupe 后池子少一张**：`sync` / `replenish` 会补。
- **`sync` 报“池里缺失超过一半”**：先检查 `WALLHAVEN_POOL_DIR`/挂载；确认是故意换池再加
  `--force-prune`。这是防止路径写错时把 365 条索引清空。
- **replenish 不淘汰**：检查 `noctalia msg wallpaper-get` 是否正常、`pin` 列表、
  今日配额是否用完（`status` 的“今日淘汰”）；`--dry-run` 会打印候选。
- **replenish 淘汰太多**：默认只淘汰已展示过的图；确认没有加 `--evict-unshown`；
  每日上限由 `--max-per-day` / 模块选项 `maxPerDay` 控制。
- **恢复被淘汰的图**：先 `trash --list` 看是否还在回收站，再
  `wallhaven-sync restore <id>`；清掉后会自动重新下载。
- **history 的 in_pool 是 false**：检查 `WALLHAVEN_POOL_DIR` 是否指向真实池子；
  直接跑脚本而不经过 wrapper 时最容易出现。
- **key 权限**：`doctor` 会检查 600。
- **Noctalia 界面覆盖**：界面改的值在 `~/.local/state/noctalia/settings.toml`，
  优先级高于 HM 的 `config.toml`；`doctor` 会提示。
