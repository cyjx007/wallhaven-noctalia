# wallhaven-noctalia

[![build](https://github.com/cyjx007/wallhaven-noctalia/actions/workflows/build.yml/badge.svg)](https://github.com/cyjx007/wallhaven-noctalia/actions/workflows/build.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Turn the Wallhaven rankings into an automated desktop theming pipeline.**

English · [中文](README.zh-CN.md)

```text
Wallhaven API → local wallpaper pool → Noctalia rotation
→ Material You palette → system templates
```

The tool manages a **rolling local wallpaper library**: fetch from rankings and
tags, prefilter via the API, deduplicate (SHA-256 + pHash), run quality checks,
keep about 365 active images, evict a batch every day, and keep the metadata in
SQLite. Rotation and palette generation are delegated to
[Noctalia](https://github.com/noctalia-dev/noctalia), which already implements
no-replacement shuffling (`wallpaper_shuffle.json`).

When the wallpaper changes, Noctalia regenerates the Material You palette and
writes templates for GTK, kitty, ghostty, btop, bat, Umbriel (a wlroots
compositor), Zed, opencode, fcitx5 and other applications.

This project was extracted from the author's NixOS configuration. It is MIT
licensed and assumes a NixOS + Home Manager + Noctalia setup.

## Project layout

```text
wallhaven_sync/         package: config/api/quality/db/state/sources/sync/rolling/commands/cli
package.nix             Nix package; runs the unit tests at build time
module.nix              Home Manager module: package, daily replenish service/timer
fcitx5-templates/       optional fcitx5 Noctalia templates
docs/                   capabilities and architecture notes
.github/workflows/      CI (nix build .#wallhaven-sync)
flake.nix               packages / homeManagerModules outputs
```

## Requirements

- Linux (the package declares `platforms.linux`), with Nix flakes and Home
  Manager.
- Noctalia 5.5 or newer for no-replacement wallpaper rotation and palette
  templates.
- ImageMagick, used for thumbnails and quality checks.
- A Wallhaven API key.
- Python 3 is provided by the Nix package; no manual Python setup is needed.

## Install

Add the flake input:

```nix
# flake.nix
inputs.wallhaven-noctalia.url = "github:cyjx007/wallhaven-noctalia";
```

Import the Home Manager module:

```nix
imports = [ inputs.wallhaven-noctalia.homeManagerModules.default ];

services.wallhaven = {
  enable = true;
  target = 365;
  poolDir = "${config.home.homeDirectory}/Pictures/wallhaven";
  noctaliaPackage = config.programs.noctalia.package; # optional, for palette checks
  maxPerDay = 24;    # images evicted and replaced per day (0 = top up only)
  trashDays = 7;     # keep evicted images in <poolDir>-trash/ for N days
  username = "alice"; # optional: read collections for a pin list
  maxBytes = 3 * 1024 * 1024 * 1024; # hard cap for the active pool
};
```

| Option | Default | Meaning |
| --- | --- | --- |
| `enable` | `false` | Install the collector and its systemd units. |
| `target` | `365` | Active pool size: first fill tops up to this, later runs evict back down to it. |
| `poolDir` | `~/Pictures/wallhaven` | Pool directory. |
| `keyFile` | `$XDG_CONFIG_HOME/wallhaven/apikey` | File containing the API key. |
| `noctaliaPackage` | `null` | Put Noctalia's `noctalia` binary on the collector's PATH. |
| `maxPerDay` | `24` | Daily eviction/replacement budget. `0` disables eviction. |
| `trashDays` | `7` | Retention for evicted images; `0` keeps them forever. |
| `username` | `null` | Read the account's collections and pin those images. |
| `maxBytes` | 3 GiB | Hard byte cap for the active pool; `0` means unlimited. |

Create the API key file (never commit it):

```bash
mkdir -p ~/.config/wallhaven
printf '%s\n' 'YOUR_WALLHAVEN_API_KEY' > ~/.config/wallhaven/apikey
chmod 600 ~/.config/wallhaven/apikey
```

Then fill the pool:

```bash
wallhaven-sync sync --dry-run  # inspect candidates first
wallhaven-sync sync            # first fill; safe to interrupt and resume
wallhaven-sync status
wallhaven-sync doctor
```

Afterwards systemd runs `replenish` once a day and again five minutes after
login. The first run fills all 365 slots; later runs download replacements and
then evict the same number of old images. When the pool is full the job does not
exit immediately — it performs the daily rotation instead. Use
`wallhaven-sync replenish --dry-run` to preview it.

## Noctalia configuration

The collector does not modify Noctalia settings; declare them yourself:

```nix
programs.noctalia.settings = {
  wallpaper = {
    directory = "${config.home.homeDirectory}/Pictures/wallhaven";
    fill_mode = "crop";
    automation = {
      enabled = true;
      interval_seconds = 3600;   # change hourly
      order = "random";          # no-replacement shuffle
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

`wallpaper.directory` may be left unset; Noctalia then falls back to
`~/Pictures`. Changing settings through the Noctalia UI writes `settings.toml`
and overrides these declarative values; `wallhaven-sync doctor` warns about it.

### Why there is no `pool/` + `active/` split

Noctalia 5.5 already implements no-replacement shuffling in
`src/shell/wallpaper/wallpaper_shuffle_state.cpp`. The state lives in
`~/.local/state/noctalia/wallpaper_shuffle.json`, and the directory is rescanned
every round. Point Noctalia directly at the pool; no symlinks or custom rotation
daemon are needed.

## Quality control

Material You guarantees text contrast (measured `on_surface/surface` is about
14:1), so the collector only filters things that visibly hurt:

| Check | Threshold | Rejection reason |
| --- | --- | --- |
| Mean brightness (grayscale thumbnail) | `0.03 – 0.70` | `too-dark` / `too-bright` |
| Detail (standard deviation) | `≥ 0.03` | `too-flat` |
| Saturation / dominant chroma | at most 10% muted images | `muted-quota` |
| Hue distribution | one 30° bucket; about `target/12*1.5` per bucket | `hue-quota` |
| File size | `≤ 16 MiB` | `too-large` |

`hue-quota` and `muted-quota` are soft rejections: they are not written to a
permanent blocklist. If the pool is not full after the first pass, the collector
relaxes those quotas and tries again. Hard rejections (too dark, too bright, too
flat, too large) are skipped permanently.

## Rolling window

### First fill vs daily maintenance

| Phase | Trigger | Behaviour |
| --- | --- | --- |
| First fill | manual `sync`, or the timer five minutes after login | top up to `target` (365) without evicting |
| Daily | systemd timer, daily and five minutes after login | `replenish`: download replacements, then evict the same number of old images |

### `sync` vs `replenish`

| Command | Purpose | Evicts images? | Daily quota? | Timer uses it? |
| --- | --- | --- | --- | --- |
| `sync` | manually top up / repair a gap | no | no | no |
| `replenish` | daily rotation | yes, to `<poolDir>-trash/` | yes, except when over the hard cap | yes |

Use `add` to add one image, `sync` to top up, `replenish` to rotate.

Once the pool reaches 365 images, `replenish` no longer exits at "pool full":

```text
pick the least recently shown images → download replacements first
→ evict only as many as were actually downloaded
→ move them to <poolDir>-trash/YYYYMM/
→ mark them evicted in SQLite → delete files after 7 days
```

- Default 24 images/day, about 3 GB/month. Change with `--max-per-day` or the
  `maxPerDay` option.
- **The currently displayed wallpaper is never deleted.** The collector checks
  `noctalia msg wallpaper-get`; if that fails it skips eviction.
- Images that have never been shown are not evicted unless `--evict-unshown` is
  passed.
- Downloads happen before deletions: if the network fails, no old image is lost.
- **Hard capacity cap** (3 GiB by default): `sync` stops at the cap, and
  `replenish` evicts beyond the daily quota when the pool is over it, while still
  protecting the current wallpaper, pins and recently shown images.
- Downloads go to `.incoming-*.part`, are verified and `fsync`ed, then renamed
  atomically.
- Evicted IDs and pHashes stay in SQLite so they are not downloaded again.
- `pin <id>` prevents eviction; `restore <id>` restores from trash or re-downloads.

```bash
wallhaven-sync trash                 # inspect the 7-day trash
wallhaven-sync restore <id>          # restore from trash or re-download
wallhaven-sync restore <id> --force  # restore even if over maxBytes
wallhaven-sync pin <id>              # never auto-evict
wallhaven-sync unpin <id>
wallhaven-sync pins
```

The first run still fills all 365 slots. After that systemd calls `replenish`
daily and five minutes after login, but never more than `maxPerDay` on the same
day.

## History and metadata

Add a Noctalia hook:

```toml
[hooks]
wallpaper_changed = "wallhaven-sync record"
```

Noctalia passes `NOCTALIA_WALLPAPER_PATH` and
`NOCTALIA_WALLPAPER_CONNECTOR`; `record` appends a line to `history.jsonl` and
updates `library.db` (`shown_count`, `first_shown_at`, `last_shown_at`,
`display_history`). Automatic rotation, `wallpaper-next` and manual selection are
all recorded; multi-monitor setups are tracked per connector.

```bash
wallhaven-sync history --limit 30
wallhaven-sync report
sqlite3 ~/.local/state/wallhaven-sync/library.db \
  "select status,count(*) from images group by status;"
```

`report` shows seen/unseen counts, most-shown images, source and hue
distribution, pHash coverage and quarantined images.

## pHash deduplication

SHA-256 only catches byte-identical files. `dedupe` uses pHash to find visual
duplicates (recompression, resizing, slight colour changes):

```bash
wallhaven-sync dedupe             # report only; cache missing pHashes in the index
wallhaven-sync dedupe --apply     # quarantine duplicates at distance ≤ 3
```

- 16×16 grayscale → DCT → 8×8 → 64-bit fingerprint, pure Python, no NumPy.
- Quarantined files go to `<poolDir>-quarantine/duplicates/`; nothing is deleted.
- Quarantined IDs are not downloaded again; `dedupe --restore <id>` restores one.
- `sync` rejects distance ≤ 3; distances 4–7 are accepted and only reported by
  `dedupe`.

## Commands

```bash
wallhaven-sync replenish                 # daily rolling window: evict + replace 24
wallhaven-sync replenish --dry-run       # show the plan
wallhaven-sync replenish --max-bytes 3G  # hard pool cap
wallhaven-sync sync                      # top up to target
wallhaven-sync sync --dry-run            # list candidates without writing the index
wallhaven-sync sync --retry-rejected
wallhaven-sync sync --retry-duplicates
wallhaven-sync status [--json]           # counts, disk, hue, DB, trash, rotation progress
wallhaven-sync doctor                    # key, DB, hooks, Noctalia, Umbriel, recent logs
wallhaven-sync backfill                  # fill missing URL/favourites/tags in old indexes
wallhaven-sync rehydrate                 # re-download active images by ID
wallhaven-sync preview <file|id>         # terminal colour blocks + brightness
wallhaven-sync add <id|url>              # add one image (--force skips QC)
wallhaven-sync ban <id>                  # delete and blocklist
wallhaven-sync pin <id>                  # never auto-evict
wallhaven-sync unpin <id>
wallhaven-sync pins                      # list pins
wallhaven-sync restore <id>              # restore from trash or re-download
wallhaven-sync trash [--purge]           # inspect/clean trash
wallhaven-sync collections --username NAME  # read-only collection pin list
wallhaven-sync dedupe [--apply]          # pHash deduplication
wallhaven-sync dedupe --list-quarantine
wallhaven-sync dedupe --restore <id>
wallhaven-sync history [--limit N]
wallhaven-sync report
wallhaven-sync record                    # for the Noctalia hook
```

See [docs/CAPABILITIES.md](docs/CAPABILITIES.md) for the full capability list
and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the architecture and
invariants.

The state directory `~/.local/state/wallhaven-sync/` contains `index.json`
(active metadata), `library.db` (history, evictions, pins) and `history.jsonl`.
**Back these up**; images can be downloaded again. Add them to restic, Borg or
rsync.

> The Wallhaven API has no `/favorites` endpoint (tested: 404), so starred
> favourites cannot be read or written. Only collections are readable, and they
> require the account username. With `services.wallhaven.username` set, daily
> replenishment pins collection images automatically. A star is not protection;
> add the image to a collection.

**Moving machines:** back up the state directory and the API key; images are
not required. After restoring, run `wallhaven-sync rehydrate` to re-download the
active pool, or copy the pool directory. Do not share one `index.json` /
`library.db` between two machines in real time: SQLite and `flock` assume a local
filesystem, and concurrent `replenish` runs overwrite each other. Use a separate
state directory per machine and collections as the shared pin list.

The API key is sent in the `X-API-Key` header; requests are rate-limited to one
every 1.4 seconds. The index and images stay local and are not committed to Git.

## fcitx5 (optional)

`fcitx5-noctalia-templates` copies the rounded SVGs from mellow-youlan-dark and
replaces only the colours with `{{ colors.* }}` tokens, keeping the shapes:

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

The mellow SVG shapes come from
[fcitx5-mellow-themes](https://github.com/sanweiya/fcitx5-mellow-themes)
(GPL-3.0); this repository references them at build time and does not bundle
their assets.

## NixOS file ownership

Noctalia templates write to `~/.config`, but Home Manager creates read-only
store symlinks. Pre-declare includes/imports so templates only write generated
files:

| Target | Approach |
| --- | --- |
| Umbriel `noctalia.toml` | `[include.optional] files = ["theme-fallback.toml", "noctalia.toml"]`; the main file overrides includes, so do not put dynamic colours there. |
| kitty `themes/noctalia.conf` | `include themes/noctalia.conf` in `kitty.conf` (or a custom user template to avoid the built-in `apply.sh` touching a read-only file). |
| GTK `noctalia.css` | `@import url("file:///.../noctalia.css")` in `gtk.css`. |
| ghostty | Set `theme = noctalia` in Home Manager; a custom user template only writes `themes/noctalia`. |

## Not themed by Noctalia

- **Qt / Kvantum** — Kvantum does not read the Noctalia palette; switch to qt6ct
  + Fusion if you need it.
- **Firefox** — needs Noctalia's Pywalfox integration plus the browser extension.
- Telegram, Steam, mpv and similar applications need their own theming hooks.

## Development and tests

```bash
nix build .#                 # builds the package and runs test_wallhaven_sync.py
nix run .# -- status         # run from the working tree
python3 -m wallhaven_sync status  # run the package directly
```

The package's `checkPhase` runs the unit tests with `python -m unittest`; CI
builds `.#wallhaven-sync`, so the same tests run there.

## License

MIT. See [LICENSE](LICENSE).
