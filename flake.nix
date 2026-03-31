{
  description = "Rephoto: download Google Photos storage items and re-upload via phone";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.05";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };

        pythonEnv = pkgs.python312.withPackages (ps: with ps; [
          playwright
        ]);

        runtimeTools = with pkgs; [
          android-tools
          exiftool
          gnutar
          playwright-driver
          unzip
          zip
        ];

        rephotoBin = pkgs.writeShellApplication {
          name = "rephoto";
          runtimeInputs = [ pythonEnv ] ++ runtimeTools;
          text = ''
            export PLAYWRIGHT_BROWSERS_PATH="${pkgs.playwright-driver.browsers}"
            export PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=true
            export PLAYWRIGHT_NODEJS_PATH="${pkgs.nodejs}/bin/node"
            export PYTHONPATH="${toString ./.}:''${PYTHONPATH:-}"
            exec ${pythonEnv}/bin/python -m rephoto.cli "$@"
          '';
        };
      in {
        packages.default = rephotoBin;
        packages.rephoto = rephotoBin;

        apps.default = {
          type = "app";
          program = "${rephotoBin}/bin/rephoto";
        };

        devShells.default = pkgs.mkShell {
          packages = [ pythonEnv ] ++ runtimeTools;
          shellHook = ''
            export PLAYWRIGHT_BROWSERS_PATH="${pkgs.playwright-driver.browsers}"
            export PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=true
            export PLAYWRIGHT_NODEJS_PATH="${pkgs.nodejs}/bin/node"
            export PYTHONPATH="${toString ./.}:''${PYTHONPATH:-}"
          '';
        };

        checks.python-compile = pkgs.runCommand "rephoto-python-compile" {
          nativeBuildInputs = [ pythonEnv ];
        } ''
          tmp="$TMPDIR/rephoto-src"
          cp -r ${./rephoto} "$tmp"
          chmod -R u+w "$tmp"
          ${pythonEnv}/bin/python -m compileall -q "$tmp"
          touch "$out"
        '';
      });
}
