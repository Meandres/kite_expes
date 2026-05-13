{
    description = "kite_expes";

    inputs =
    {
        nixpkgs-unstable.url = "github:NixOS/nixpkgs/nixos-unstable";
        nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";
        nixpkgs-2211.url = "github:NixOS/nixpkgs/nixos-22.11";
        nixos-generators = {
            url = "github:nix-community/nixos-generators";
            inputs.nixpkgs.follows = "nixpkgs-2211";
        };
        flake-utils.url = "github:numtide/flake-utils";
        nur-niwa.url = "github:Meandres/nur-niwa";
    };

    outputs =
    {
        self
        , nixpkgs
        , nixpkgs-unstable
        , nixpkgs-2211
        , nixos-generators
        , flake-utils
        , nur-niwa
    } @ inputs:
   (flake-utils.lib.eachSystem ["x86_64-linux"](system:
    let
        unstable-pkgs = nixpkgs-unstable.legacyPackages.${system};
        pkgs = import nixpkgs {
            inherit system;
            overlays = [ (import ./nix/overlays.nix {inherit inputs; }) ];
        };
        pkgs-2211 = import nixpkgs-2211 {
            inherit system;
            overlays = [ (import ./nix/overlays.nix {inherit inputs; }) ];
        };
        make-disk-image = import (nixpkgs + "/nixos/lib/make-disk-image.nix");
        selfpkgs = self.packages.x86_64-linux;
        niwa-pkgs = nur-niwa.packages.x86_64-linux;
        kernelPackages = pkgs.linuxKernel.packages.linux_6_1;
      in {
            packages =
            {
                vmcache = (import ./nix/vmcache.nix { inherit pkgs;});
                mmapbench = (import ./nix/mmapbench.nix { inherit pkgs;});
                specificKernelPackages = kernelPackages;

                exmap = (import ./nix/exmap.nix { inherit pkgs; inherit kernelPackages; });

                umap-apps = niwa-pkgs.umap-apps;

                linux-image = make-disk-image {
                    config = self.nixosConfigurations.linux-image.config;
                    inherit (pkgs) lib;
                    inherit pkgs;
                    format = "qcow2";
                };

            };

            devShells = {
                default = (pkgs.mkShell
                {
                    name = "benchmark-devshell";
                    buildInputs =
                    with pkgs;
                    [
                        gdb
                        qemu_full
                        just
                        python3Packages.pandas 
                        python3Packages.matplotlib 
                        python3Packages.seaborn 
                        python3Packages.polars

                        libaio
                        niwa-pkgs.driverctl
			                  postgresql

                        # leanstore
                        gflags
                        gtest
                        libgcrypt
                        gbenchmark
                        postgresql
                        fmt
                        wiredtiger
                        sqlite
                        mysql80
                        libmysqlconnectorcpp

                        unstable-pkgs.duckdb
                        nlohmann_json
			flamegraph
                    ];
                    nativeBuildInputs = with pkgs; [
                    ];
                });
            };
        }
    )
  ) // (let
      pkgs = import nixpkgs {
            system = "x86_64-linux";
            overlays = [ (import ./nix/overlays.nix {inherit inputs; }) ];
        };
        selfpkgs = self.packages.x86_64-linux;
        niwa-pkgs = nur-niwa.packages.x86_64-linux;
        kernelPackages = selfpkgs.specificKernelPackages;
    in {
        nixosConfigurations = {
            linux-image = nixpkgs.lib.nixosSystem {
                system = "x86_64-linux";
                modules = [
                    (import ./nix/image.nix
                    {
                        inherit pkgs;
                        inherit (pkgs) lib;
                        inherit selfpkgs;
                        inherit kernelPackages;
                        extraEnvPackages = [ pkgs.mdadm-44 ];
                    })
                    ./nix/nixos-generators-qcow.nix
                ];
            };
        };
    });
}
