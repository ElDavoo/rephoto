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
          # used directly by requota_migration.py. gpmc itself is pure-Python and is
          # put on sys.path by the script, so it needs no packaging here.
          python = pkgs.python3.withPackages (ps: with ps; [ bbpb rich requests ]);
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
              echo ""
              echo "Dry run:  python requota_migration.py --adb-token --download-only --limit 5"
            '';
          };
        });
    };
}
