DUCKDB_DIR ?= ../../duckdb_samuel # TODO replace this with your actual path to DuckDB

INCLUDE_DIR := $(DUCKDB_DIR)/src/include
LIB_DIR     := $(DUCKDB_DIR)/build/release/src

JSON_CFLAGS := $(shell pkg-config --cflags nlohmann_json 2>/dev/null)

CXX      := g++
CXXFLAGS := -std=c++17 -O3 -DNDEBUG -I$(INCLUDE_DIR) $(JSON_CFLAGS)
LDFLAGS  := -L$(LIB_DIR) -lduckdb -Wl,-rpath,$(abspath $(LIB_DIR))

all: run_bench

run_bench: run_bench.cpp
	$(CXX) $(CXXFLAGS) $< -o $@ $(LDFLAGS)

clean:
	rm -f run_bench
