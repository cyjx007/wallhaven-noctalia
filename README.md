# wallhaven-noctalia

把 **wallhaven 排行榜**变成一条自动化的桌面主题流水线：

```
wallhaven API → 本地壁纸池 → Noctalia 轮换 → Material You 调色 → 全系统模板
```

- 采集器只做一件事：按排行榜挑图、去重、品控、填满池子（默认 365 张）。
- 轮换和调色全部交给 [Noctalia](https://github.com/noctalia-dev/noctalia)：它自带
  **无放回洗牌**（`wallpaper_shuffle.json`），365 张全部看过才开下一轮。
- 换壁纸时 Noctalia 重新生成调色板，并通过模板写 GTK、kitty、ghostty、btop、bat、
  Umbriel（wlroots 合成器）、Zed、opencode、fcitx5 等配置。

本项目是从作者的 NixOS 配置里抽出来的独立实现，MIT。

## 组成

| 文件 | 作用 |
| --- | --- |
| `wallhaven_sync.py` | 采集器（纯 Python 标准库 + ImageMagick + `noctalia theme`） |
| `package.nix` | 打包（含单元测试） |
| `module.nix` | Home Manager 模块：包 + systemd service/timer |
| `fcitx5-templates/` | 把 mellow 圆角皮肤转成 Noctalia 模板（可选） |
| `flake.nix` | `packages` / `homeManagerModules` |

## 安装（NixOS + Home Manager）

```nix
# flake.nix
inputs.wallhaven-noctalia.url = "github:cyjx007/wallhaven-noctalia";
```

```nix
# home-manager 模块
imports = [ inputs.wallhaven-noctalia.homeManagerModules.default ];

services.wallhaven = {
  enable = true;
  target = 365;
  poolDir = "${config.home.homeDirectory}/Pictures/wallhaven";
  noctaliaPackage = config.programs.noctalia.package; # 可选，用于调色板品控
};
```

创建 API key（**不要提交到 git**）：

```bash
mkdir -p ~/.config/wallhaven
printf '%s\n' 'YOUR_WALLHAVEN_API_KEY' > ~/.config/wallhaven/apikey
chmod 600 ~/.config/wallhaven/apikey
```

然后：

```bash
wallhaven-sync sync        # 首次填满池子（可中断续跑）
wallhaven-sync status
wallhaven-sync doctor
```

systemd 会每天补货，登录 5 分钟后也会补一次；池子满时秒退。

## Noctalia 侧配置

采集器不碰 Noctalia 的设置，需要你自己声明（NixOS 示例）：

```nix
programs.noctalia.settings = {
  wallpaper = {
    directory = "${config.home.homeDirectory}/Pictures/wallhaven";
    fill_mode = "crop";
    automation = {
      enabled = true;
      interval_seconds = 3600;   # 每小时换一张
      order = "random";          # 无放回洗牌
      recursive = false;
      pause_on_fullscreen = true;
    };
  };

  theme.templates = {
    enable_builtin_templates = true;
    enable_community_templates = true;
    builtin_ids = [ "umbriel" "gtk3" "gtk4" "btop" "qt" ];
    community_ids = [ "thunar" "bat" "opencode" "zed" "thunderbird" "pywalfox" ];
  };

  hooks.colors_changed = [
    "umbriel msg config-reload"
    "noctalia msg greeter-sync"
  ];
};
```

> `wallpaper.directory` 也可以不设，Noctalia 会回退到 `~/Pictures`；
> 用 `settings.toml`（界面改设置）会覆盖这里的声明式值，`wallhaven-sync doctor`
> 会提示。

### 为什么没有 `pool/` + `active/` 两层目录

Noctalia 5.5 的 `src/shell/wallpaper/wallpaper_shuffle_state.cpp` 已经实现了无放回洗牌，
状态存在 `~/.local/state/noctalia/wallpaper_shuffle.json`，并且每轮重新扫描目录。
直接指向池子即可，不需要额外软链或自定义轮换 daemon。

## 品控

Material You 本身保证文本对比度（实测 `on_surface/surface` 约 14:1），所以采集器只过滤
真正影响观感的东西：

| 检查 | 阈值 | 拒绝原因 |
| --- | --- | --- |
| 平均亮度（灰度缩略） | `0.03 ~ 0.70` | `too-dark` / `too-bright` |
| 层次（标准差） | `≥ 0.03` | `too-flat` |
| 饱和度 / 主色 chroma | 闷图最多 10% | `muted-quota` |
| 色相分布 | 每 30° 一格，单格上限约 `target/12*1.5` | `hue-quota` |
| 文件大小 | `≤ 16 MiB` | `too-large` |

`hue-quota` / `muted-quota` 是**软拒绝**：不写永久黑名单，第一遍结束后若没满，
自动进入宽松模式再补一遍。硬拒绝（太亮/太暗/太平/太大）才会永久跳过。

## 展示历史

在 Noctalia 配置里加一条钩子：

```toml
[hooks]
wallpaper_changed = "wallhaven-sync record"
```

Noctalia 会传 `NOCTALIA_WALLPAPER_PATH` / `NOCTALIA_WALLPAPER_CONNECTOR`，
`record` 往 `history.jsonl` 追加一行。自动轮换、`wallpaper-next`、手动选图都会记录；
多显示器按 connector 分开。然后：

```bash
wallhaven-sync history --limit 30
wallhaven-sync report
```

`report` 会给出已看过/从未展示、最常展示、来源/色相分布、pHash 覆盖和隔离区。

## pHash 去重

SHA-256 只能抓字节完全相同的文件。`dedupe` 用 pHash 找视觉重复（重新压缩、缩放、轻微调色）：

```bash
wallhaven-sync dedupe                 # 只报告，并把缺失的 pHash 缓存进 index
wallhaven-sync dedupe --apply         # 隔离距离 ≤3 的重复（4–7 只报告）
```

- 16×16 灰度 → DCT → 8×8 → 64 位指纹；纯 Python，无 numpy。
- 隔离到 `<池子目录>-quarantine/duplicates/`，不删除。
- 被隔离的 ID 不会再次下载；`dedupe --restore <id>` 可以恢复。
- 初始 365 张实测：1 对距离 2 的真重复（不同压缩，已隔离）、1 对距离 4 和 1 对距离 6
  的相似图；后者只标记，不自动删。
- `sync` 只拒绝距离 ≤3；4–7 正常入池，由 `dedupe` 报告。

## 命令

```bash
wallhaven-sync sync                 # 补齐到 target
wallhaven-sync sync --dry-run       # 只列候选，不写索引
wallhaven-sync sync --retry-rejected
wallhaven-sync sync --retry-duplicates
wallhaven-sync status [--json]      # 数量、磁盘、色相、pHash、历史、轮换进度
wallhaven-sync doctor               # key/pool/Noctalia/Umbriel/最近日志
wallhaven-sync preview <文件|id>    # 终端色块 + 亮度
wallhaven-sync add <id|url>         # 手动加一张（--force 跳过品控）
wallhaven-sync ban <id>             # 删除并拉黑
wallhaven-sync dedupe [--apply]     # pHash 去重
wallhaven-sync dedupe --list-quarantine
wallhaven-sync dedupe --restore <id>
wallhaven-sync history [--limit N]  # 展示历史
wallhaven-sync report               # 统计报告
wallhaven-sync record               # 给 Noctalia 钩子用
```

完整能力见 [docs/CAPABILITIES.md](docs/CAPABILITIES.md)。

API 用 `X-API-Key` 请求头，限流 1.4s/次；索引与图片都在本地，不进 git。

## fcitx5（可选）

`fcitx5-noctalia-templates` 会把 mellow-youlan-dark 的圆角 SVG 复制出来，只把颜色替换成
`{{ colors.* }}` token，保留原来的形状：

```nix
let fcitx5Templates = inputs.wallhaven-noctalia.packages.${pkgs.stdenv.hostPlatform.system}.fcitx5-noctalia-templates;
in {
  programs.noctalia.settings.theme.templates.user = {
    fcitx5-panel = { input_path = "${fcitx5Templates}/panel.svg"; output_path = "$XDG_DATA_HOME/fcitx5/themes/noctalia/panel.svg"; index = 0; };
    fcitx5-highlight = { input_path = "${fcitx5Templates}/highlight.svg"; output_path = "$XDG_DATA_HOME/fcitx5/themes/noctalia/highlight.svg"; index = 1; };
    fcitx5-theme = {
      input_path = "${fcitx5Templates}/theme.conf";
      output_path = "$XDG_DATA_HOME/fcitx5/themes/noctalia/theme.conf";
      index = 2;
      hook_async = false;
      post_hook = "busctl --user call org.fcitx.Fcitx5 /controller org.fcitx.Fcitx.Controller1 ReloadAddonConfig s classicui 2>/dev/null || fcitx5-remote -r 2>/dev/null || true";
    };
  };
}
```

mellow 的 SVG 形状来自 [fcitx5-mellow-themes](https://github.com/sanweiya/fcitx5-mellow-themes)
（GPL-3.0）；本仓库只在构建时引用它，不打包其资源。

## NixOS 文件所有权

Noctalia 模板会写 `~/.config`，但 Home Manager 生成的是只读 store 软链。需要在这些文件里
**预先声明 include/import**，让模板只写生成文件：

| 目标 | 做法 |
| --- | --- |
| Umbriel `noctalia.toml` | `[include.optional] files = ["theme-fallback.toml", "noctalia.toml"]`（主文件会覆盖 include，动态颜色不要写在主文件） |
| kitty `themes/noctalia.conf` | `kitty.conf` 里 `include themes/noctalia.conf`（或自定义 user 模板，避免内置 apply.sh `touch` 只读文件） |
| GTK `noctalia.css` | `gtk.css` 里 `@import url("file:///.../noctalia.css")` |
| ghostty | HM 设置 `theme = noctalia`，自定义 user 模板只写 `themes/noctalia` |

## 仍然不变的部分

- **Qt / Kvantum**：Kvantum 不读 Noctalia 调色板；要变需改用 qt6ct + Fusion。
- **Firefox**：需要 Noctalia 的 Pywalfox 集成 + 浏览器扩展。
- Telegram / Steam / mpv 等需要各自的主题机制。

## License

MIT。见 [LICENSE](LICENSE)。
