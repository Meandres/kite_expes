{ pkgs, ... }:

let
  # DuckDB fork (branch: ucache) — built as a shared library.
  duckdbSrc = pkgs.fetchFromGitHub {
    owner = "Meandres";
    repo  = "duckdb";
    rev   = "d5a1b699e90f0f43c7826113149765b9d8650709";
    hash  = "sha256-9JISMLtuwGq4FwWWsVa/v9N+K62ospTXYHjNwmhIPIM=";
  };

  # osv_benchmarks repo — only the duckdb/ subdirectory is used.
  # fetchFromGitHub downloads a plain zip archive, so submodules are ignored.
  benchmarksSrc = pkgs.fetchFromGitHub {
    owner = "Meandres";
    repo  = "osv_benchmarks";
    rev   = "4e6d07424be42fae016663a75e9e704e1e62fc51";
    hash  = "sha256-lhEawLy9UDpPsgc+tKCqD1DORy92REwO4yzGpK4MiKg=";
  };
in

pkgs.stdenv.mkDerivation {
  pname = "duckdb-tpch-bench";
  version = "1.5.2-osv";

  # All sources are referenced by store path below; skip the stdenv unpack/configure steps.
  src = benchmarksSrc;
  unpackPhase = ":";
  configurePhase = ":";

  nativeBuildInputs = with pkgs; [ cmake ninja python3 pkg-config ];

  buildPhase = ''
    echo "--- Building libduckdb ---"
    cmake -S ${duckdbSrc} -B duckdb-build \
      -G Ninja \
      -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_CXX_STANDARD=17 \
      -DDUCKDB_EXPLICIT_PLATFORM=linux_amd64 \
      -DBUILD_UNITTESTS=OFF \
      -DBUILD_SHELL=OFF \
      -DBUILD_BENCHMARKS=OFF
    ninja -C duckdb-build duckdb -j$NIX_BUILD_CORES

    echo "--- Building duckdb_bench ---"
    g++ -std=c++17 -O2 \
      -I${duckdbSrc}/src/include \
      -I${benchmarksSrc}/duckdb \
      -o duckdb_bench \
      ${benchmarksSrc}/duckdb/duckdb_app.cc \
      -Lduckdb-build/src -lduckdb \
      -Wl,-rpath,$out/lib \
      -lpthread -ldl -lm
  '';

  installPhase = ''
    install -Dm755 duckdb_bench               $out/bin/duckdb_bench
    install -Dm755 duckdb-build/src/libduckdb.so $out/lib/libduckdb.so
  '';

  meta = with pkgs.lib; {
    description = "DuckDB TPC-H benchmark runner (Linux, for OSv comparison)";
    homepage    = "https://github.com/Meandres/osv_benchmarks";
    platforms   = [ "x86_64-linux" ];
  };
}
