{
  description = "Wallhaven wallpaper pool + Noctalia dynamic theming";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAll (pkgs: {
        default = pkgs.callPackage ./package.nix { };
        wallhaven-sync = pkgs.callPackage ./package.nix { };
        fcitx5-noctalia-templates = pkgs.callPackage ./fcitx5-templates { };
      });

      homeManagerModules = {
        default = import ./module.nix;
        wallhaven = import ./module.nix;
      };
    };
}
