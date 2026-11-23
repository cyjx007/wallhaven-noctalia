{ lib
, stdenv
, python3
, makeWrapper
, imagemagick
, noctaliaCli ? null
, poolDir ? null
, keyFile ? null
, target ? 365
}:

let
  runtimePath = lib.makeBinPath (
    [ python3 imagemagick ] ++ lib.optional (noctaliaCli != null) noctaliaCli
  );

  extraDefaults = lib.concatStringsSep " " (
    lib.optional (poolDir != null)
      "--set-default WALLHAVEN_POOL_DIR ${lib.escapeShellArg poolDir}"
    ++ lib.optional (keyFile != null)
      "--set-default WALLHAVEN_API_KEY_FILE ${lib.escapeShellArg keyFile}"
  );
in
stdenv.mkDerivation {
  pname = "wallhaven-sync";
  version = "1.2.1";

  src = ./.;

  nativeBuildInputs = [ python3 makeWrapper ];

  dontConfigure = true;
  dontBuild = true;

  doCheck = true;
  checkPhase = ''
    runHook preCheck
    ${python3.interpreter} -m unittest discover -s . -p 'test_*.py'
    runHook postCheck
  '';

  installPhase = ''
    runHook preInstall
    install -d $out/lib/wallhaven-sync
    cp -r wallhaven_sync $out/lib/wallhaven-sync/
    find $out/lib/wallhaven-sync -name __pycache__ -type d -prune -exec rm -rf {} +
    makeWrapper ${python3.interpreter} $out/bin/wallhaven-sync \
      --add-flags "-m wallhaven_sync" \
      --prefix PYTHONPATH : $out/lib/wallhaven-sync \
      --prefix PATH : ${runtimePath} \
      --set-default WALLHAVEN_TARGET ${toString target} ${extraDefaults}
    runHook postInstall
  '';

  meta = {
    description = "Manage a rolling wallhaven wallpaper pool for Noctalia";
    homepage = "https://github.com/cyjx007/wallhaven-noctalia";
    license = lib.licenses.mit;
    mainProgram = "wallhaven-sync";
    platforms = lib.platforms.linux;
  };
}
