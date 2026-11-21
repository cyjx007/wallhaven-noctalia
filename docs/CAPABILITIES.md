# 能力说明（当前实现）

这份文档描述 `wallhaven-noctalia` **现在已经能做到什么**，以及不做什么。
设计目标是：保持 **365 张本地池 + Noctalia 轮换** 的简单架构，不重复实现轮换器。

## 一条数据流

```
wallhaven API（Toplist 1M/3M/6M/1y）
        │
        ├─ API 初筛：SFW / general / 16:9 / ≥1920x1080 / ≤16 MiB
        │
        ├─ 下载 → SHA-256 去重
        │
        ├─ stats + Noctalia 调色：亮度/层次/饱和度、m3-content 的 primary
        │
        ├─ pHash：≤3 自动拒绝，4–7 留给 dedupe 报告
        │
        ├─ judge：亮度 / 层次 / 饱和度 / 色相配额
        │
        ├─ index.json（365 张元数据）
        │
        └─ 本地池 ~/Pictures/wallhaven/
                    │
              Noctalia wallpaper.automation
              （random，无放回洗牌）
                    │
              wallpaper_changed hook
                    │
              history.jsonl（展示历史）
                    │
              history / report
```

## 1. 采集

- 来源：wallhaven **Toplist**，范围 `1M → 3M → 6M → 1y`，按顺序补货。
- **不支持 All-time**：官方 `topRange` 没有这个值；实测 `all` 会退化成 1M。
- 目标张数默认 365；达到后 `sync` 直接退出，不下载。
- 首次全量约 1.4 GiB / 2 小时；可中断，索引逐张落盘。
- API 用 `X-API-Key` 头，限流 1.4s/次；429 读 `Retry-After`。
- 单个榜单失败只跳过该范围，不影响后面的范围。
- 断点续跑、原子写索引、`fsync`、并发锁。
- 下载失败/过大/无法解析不会中断整次同步。

## 2. 去重

三级：

| 级别 | 依据 | 行为 |
| --- | --- | --- |
| 1 | wallhaven ID | 已存在直接跳过 |
| 2 | SHA-256 | 文件字节完全相同的另一个 ID，记入 `duplicates`，不再重复下载 |
| 3 | pHash | 视觉相似的另一个 ID，≤3 自动拒绝；4–7 在 sync 里接受、由 `dedupe` 报告 |

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
| 文件大小 | ≤ 16 MiB | |
| 格式 | 拒绝 GIF/AVIF | 动图/解码依赖 |

**软拒绝**：`hue-quota` / `muted-quota` 不写永久黑名单；第一遍结束后如果没满，
自动进入宽松模式（只保留亮度/层次/大小硬过滤）再补一遍。

**硬拒绝**：太亮/太暗/太平/太大/格式不支持/pHash 重复，写进 `rejected`/`duplicates`，不再重试。

## 4. 展示历史

`wallpaper_changed` 钩子：

```toml
[hooks]
wallpaper_changed = "wallhaven-sync record"
```

Noctalia 提供：

- `NOCTALIA_WALLPAPER_PATH`
- `NOCTALIA_WALLPAPER_CONNECTOR`

`record` 只做一件事：追加一行 JSONL 到
`~/.local/state/wallhaven-sync/history.jsonl`：

```json
{"at":"2026-11-21T02:28:53+00:00","connector":"eDP-1","id":"w5327p","in_pool":true,"path":"...","source":"noctalia"}
```

- 自动轮换、`wallpaper-next`、手动在 Noctalia 里选图都会记录；
- 多显示器按 connector 分别记录；
- Noctalia 在配置重载/重复 apply 同一路径时也会触发 hook，`report` 会按
  `(connector, path)` 去掉连续重复，原始记录保留在 `history`；
- 非池子路径（例如临时换的外部壁纸）也会记录，但 `in_pool: false`，报告会过滤；
- O(1)、不联网、不读图。

> Noctalia 5.5 通过进程级 `setenv` 给 hook 传环境变量，再异步 fork；理论上存在极小的
> 竞态（env 在 fork 前被 unset，或多显示器串值）。所以 history 按 **best-effort** 记录；
> 真正的轮换状态仍以 Noctalia 的 `wallpaper_shuffle.json` 为准。

`history` 查看最近记录；`report` 统计：

- 已看过 / 从未展示；
- 最常展示；
- 来源分布、色相分布、磁盘占用；
- pHash 覆盖、隔离区、内容重复。

## 5. 命令

```bash
wallhaven-sync sync                    # 补齐到 target
wallhaven-sync sync --dry-run          # 只列候选，不写索引/不下载
wallhaven-sync sync --retry-rejected   # 调阈值后重新评估硬拒绝
wallhaven-sync status [--json]         # 数量、磁盘、色相、pHash、展示历史、轮换进度
wallhaven-sync doctor                  # key/pool/Noctalia/Umbriel/最近日志
wallhaven-sync preview <文件|id>       # 终端色块 + 亮度/饱和度/主色
wallhaven-sync add <id|url> [--force]  # 手动加一张；仍做 SHA/pHash 去重
wallhaven-sync ban <id>                # 删除并永久拉黑
wallhaven-sync dedupe [--apply]        # pHash 去重；默认只报告
wallhaven-sync dedupe --threshold 8    # 放宽报告阈值
wallhaven-sync dedupe --list-quarantine
wallhaven-sync dedupe --restore <id>   # 移回池子并重建索引
wallhaven-sync sync --retry-duplicates # 清空 duplicates 重新评估
wallhaven-sync history [--limit N]     # 展示历史
wallhaven-sync report                  # 图库统计
wallhaven-sync record                  # 给 Noctalia 钩子用，手动跑一般不需要
```

`dedupe` 默认 **只报告**，并顺便把缺失的 pHash 缓存进 `index.json`（元数据，不动图片）。
`--apply` 会把距离 ≤ `--auto`（默认 3）的重复移到（只有两边文件都存在时才移动；

```
<池子目录>-quarantine/duplicates/
```

例如 `<池子目录>-quarantine/duplicates/`。隔离区在池子外面，`recursive=true`
也不会被 Noctalia 扫到。被隔离的 ID 不会再次下载；`dedupe --restore <id>` 可以恢复。

## 6. Noctalia 侧

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
所以本项目不需要 `active/` 软链或缓存管理器；池子目录就是轮换队列。

## 7. 存储布局

```
~/.config/wallhaven/apikey                  # 600，不进 git
~/.local/state/wallhaven-sync/
├── index.json                              # 元数据（含 pHash）
├── history.jsonl                           # 展示历史
└── lock                                    # 同步锁

~/Pictures/wallhaven/                       # 365 张原图
├── wallhaven-abc123.jpg
└── ...

~/Pictures/wallhaven-quarantine/
└── duplicates/                             # dedupe 隔离的重复图
```

`index.json` 每张记录：

- `file`、`source`、`added_at`、`resolution`、`bytes`；
- `sha256`、`phash`；
- `stats`（亮度/层次/饱和度）、`palette`（primary/hue/chroma）；
- 全局：`rejected`、`duplicates`、`banned`、`quarantine`。

## 8. systemd

Home Manager 模块提供：

- `wallhaven-sync.service`（oneshot，`Nice=10`，`TimeoutStartSec=6h`）；
- `wallhaven-sync.timer`：每天一次 + 登录 5 分钟后一次，`Persistent=true`；
- 设置了 `keyFile` 时，`ConditionPathExists` 会让没有 key 的服务直接跳过。

## 9. 不做的事

| 不做 | 原因 |
| --- | --- |
| 自动收藏到 Wallhaven 账号 | 官方 API 没有公开的收藏写入接口；只能手动收藏 |
| All-time 排行榜 | API 不支持（`all` 实测退化成 1M） |
| Latest 源 | 质量太低（0 收藏/5 浏览）；等需要时再加，并配收藏数门槛 |
| 50–100 张缓存管理器 | Noctalia 已经无放回轮换 365 张；缓存会打乱 seen 状态 |
| SQLite | 365 张 + 几千条历史用 JSON/JSONL 足够；量级上来再迁 |
| CLIP / 水印 / 构图评分 | 误判成本高，不适合自动删除 |
| 分类池 / 可视化 UI | 当前需求不需要；Noctalia 也不是为多分类池设计的 |
| 自动删除 4–7 距离的相似图 | 误删比多留一张更糟 |

## 10. 排障

- **history 为空**：确认 `wallpaper_changed` 钩子已启用；手动跑一次
  `NOCTALIA_WALLPAPER_PATH=... wallhaven-sync record` 看 `history.jsonl`。
- **pHash 重复被拒绝**：被 `sync` 拒绝的图不在池子里，`dedupe` 看不到；
  用 `wallhaven-sync report` 看 `duplicates`（id / of / 距离），或 `sync --retry-duplicates` 重评。
- **dedupe 后池子少一张**：`sync` 会补；或者等第二天的 timer。
- **history 的 in_pool 是 false**：检查 `WALLHAVEN_POOL_DIR` 是否指向真实池子；
  直接跑脚本而不经过 wrapper 时最容易出现。
- **key 权限**：`doctor` 会检查 600。
- **Noctalia 界面覆盖**：界面改的值在 `~/.local/state/noctalia/settings.toml`，
  优先级高于 HM 的 `config.toml`；`doctor` 会提示。
