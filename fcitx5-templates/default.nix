{ lib
, stdenv
, fcitx5-mellow-themes
, python3
}:

# Turns the mellow-youlan-dark fcitx5 skin into Noctalia template inputs:
# shapes/margins stay, colours become {{ colors.* }} tokens.
stdenv.mkDerivation {
  pname = "fcitx5-noctalia-templates";
  version = "1";

  dontUnpack = true;
  nativeBuildInputs = [ python3 ];

  installPhase = ''
    runHook preInstall
    mkdir -p $out
    cp -rL ${fcitx5-mellow-themes}/share/fcitx5/themes/mellow-youlan-dark/. $out/
    chmod -R u+w $out
    python3 ${./render.py} $out
    runHook postInstall
  '';

  meta = {
    description = "Mellow fcitx5 skin with Noctalia palette placeholders";
    homepage = "https://github.com/cyjx007/wallhaven-noctalia";
    license = lib.licenses.gpl3Plus;
    platforms = lib.platforms.linux;
  };
}
