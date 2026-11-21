# Home Manager module for wallhaven-sync.
#
# It only installs the collector + systemd units.  Rotation, palette generation
# and app templates belong to Noctalia; see README for the settings snippet.
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.services.wallhaven;

  package = pkgs.callPackage ./package.nix {
    inherit (cfg) target poolDir keyFile;
    noctaliaCli = cfg.noctaliaPackage;
  };
in
{
  options.services.wallhaven = {
    enable = lib.mkEnableOption "wallhaven wallpaper pool";

    target = lib.mkOption {
      type = lib.types.ints.positive;
      default = 365;
      description = "Stop downloading once the pool reaches this many images.";
    };

    poolDir = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "/home/alice/Pictures/wallhaven";
      description = ''
        Pool directory.  `null` uses the script default
        (`~/Pictures/wallhaven`).
      '';
    };

    keyFile = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "/home/alice/.config/wallhaven/apikey";
      description = ''
        File containing the wallhaven API key (chmod 600).  `null` uses the
        script default (`$XDG_CONFIG_HOME/wallhaven/apikey`).
      '';
    };

    noctaliaPackage = lib.mkOption {
      type = lib.types.nullOr lib.types.package;
      default = null;
      example = lib.literalExpression "config.programs.noctalia.package";
      description = ''
        Optional Noctalia package.  When set, its `noctalia` binary is put on
        the collector's PATH so palette/hue quality checks always use the same
        engine as the shell.
      '';
    };

    maxPerDay = lib.mkOption {
      type = lib.types.ints.unsigned;
      default = 24;
      description = ''
        How many images the daily rolling window may evict (and replace) per
        day.  `0` disables eviction and only tops up the pool.  The first run
        still fills the whole pool; later runs keep the pool at `target` while
        rotating roughly `maxPerDay` images per day.
      '';
    };

    trashDays = lib.mkOption {
      type = lib.types.ints.unsigned;
      default = 7;
      description = ''
        Evicted images are moved to `<poolDir>-trash/YYYYMM/` and deleted after
        this many days (`0` keeps them forever).  `wallhaven-sync restore <id>`
        can bring them back before they are purged.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    home.packages = [ package ];

    systemd.user.services.wallhaven-sync = {
      Unit = {
        Description = "Replenish the wallhaven wallpaper pool (daily rolling window)";
      } // lib.optionalAttrs (cfg.keyFile != null) {
        ConditionPathExists = cfg.keyFile;
      };
      Service = {
        Type = "oneshot";
        ExecStart = "${package}/bin/wallhaven-sync replenish --max-per-day ${toString cfg.maxPerDay} --keep-trash-days ${toString cfg.trashDays}";
        Environment = [ "PYTHONUNBUFFERED=1" ];
        Nice = 10;
        IOSchedulingClass = "idle";
        TimeoutStartSec = "6h";
      };
    };

    systemd.user.timers.wallhaven-sync = {
      Unit.Description = "Daily wallhaven pool top-up";
      Timer = {
        OnCalendar = "daily";
        OnActiveSec = "5min";
        Persistent = true;
        RandomizedDelaySec = "1h";
      };
      Install.WantedBy = [ "timers.target" ];
    };
  };
}
