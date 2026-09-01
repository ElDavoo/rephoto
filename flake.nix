{
  description = "rephoto — Google Photos requota migration (download + re-upload with metadata)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" "x86_64-darwin" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      devShells = forAllSystems (pkgs:
        let
          # gpmc's runtime deps (bbpb -> imports as `blackboxprotobuf`), plus requests
          # used directly by requota_migration.py. The vendored gpsoauth submodule
          # (used by --gpsoauth auth) needs pycryptodomex + requests; urllib3 comes in
          # transitively via requests. gpmc and gpsoauth are both pure-Python and are
          # put on sys.path by the scripts, so they need no packaging here.
          python = pkgs.python3.withPackages (ps: with ps; [ bbpb rich requests pycryptodomex ]);
        in
        {
          default = pkgs.mkShell {
            packages = [
              python
              pkgs.android-tools # adb, for --adb-token auth
            ];
            shellHook = ''
              echo "rephoto devshell ready:"
              echo "  python  : $(python3 --version 2>&1)  (gpmc deps: bbpb/blackboxprotobuf, rich, requests)"
              echo "  adb     : $(command -v adb)"
              echo "  gpsoauth: vendored submodule (deps: pycryptodomex, requests) for --gpsoauth auth"
              echo ""
              echo "Dry run (ADB):      python requota_migration.py --adb-token --download-only --limit 5"
              echo "Device-free login:  python gpmc_gpsoauth_auth.py login --email you@gmail.com"
              echo "Dry run (gpsoauth): python requota_migration.py --gpsoauth --download-only --limit 5"
            '';
          };
        });
    };
}
