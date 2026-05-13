#include "duckdb.hpp"
#include <nlohmann/json.hpp>

#include <atomic>
#include <chrono>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <limits>
#include <map>
#include <mutex>
#include <optional>
#include <random>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

using json    = nlohmann::json;
using clock_t_ = std::chrono::steady_clock;

// ---------------------------------------------------------------------------
// Data structures
// ---------------------------------------------------------------------------

struct InterQueryDelay {
    enum class Dist { Immediate, Fixed, Uniform, Exponential } dist = Dist::Immediate;
    double value_ms = 0;
    double min_ms   = 0;
    double max_ms   = 0;
    double mean_ms  = 0;
};

struct ClientType {
    std::string id;
    std::vector<std::string> queries;
    enum class Order { Sequential, Random } order = Order::Sequential;
    InterQueryDelay delay;
    int rounds = 1;  // 0 = run until stop_flag
};

struct QueryDef {
    std::string sql_file;
    std::map<std::string, std::string> variables;
};

struct ClientSpec {
    std::string type;
    int count = 1;
};

struct SimConfig {
    std::vector<ClientSpec> clients;
    std::optional<uint64_t> seed;
    std::optional<int>      max_duration_s;
};

struct TraceConfig {
    std::map<std::string, QueryDef>   queries;
    std::map<std::string, ClientType> client_types;
    SimConfig                         simulation;
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

static std::string load_sql(const std::string& path,
                             const std::map<std::string, std::string>& vars)
{
    std::ifstream f(path);
    if (!f) throw std::runtime_error("Cannot open SQL file: " + path);

    std::string sql(std::istreambuf_iterator<char>{f}, {});

    for (auto& [key, val] : vars) {
        std::string ph = "{" + key + "}";
        for (size_t pos = 0; (pos = sql.find(ph, pos)) != std::string::npos; )
            sql.replace(pos, ph.size(), val);
    }
    return sql;
}

static double ms_since(clock_t_::time_point t0)
{
    return std::chrono::duration<double, std::milli>(clock_t_::now() - t0).count();
}

static double sample_delay(const InterQueryDelay& d, std::mt19937_64& rng)
{
    switch (d.dist) {
    case InterQueryDelay::Dist::Immediate:    return 0.0;
    case InterQueryDelay::Dist::Fixed:        return d.value_ms;
    case InterQueryDelay::Dist::Uniform: {
        std::uniform_real_distribution<double> ud(d.min_ms, d.max_ms);
        return ud(rng);
    }
    case InterQueryDelay::Dist::Exponential: {
        std::exponential_distribution<double> ed(1.0 / d.mean_ms);
        return ed(rng);
    }
    }
    return 0.0;
}

static void sleep_interruptible(double ms, const std::atomic<bool>& stop)
{
    auto wake = clock_t_::now() + std::chrono::duration<double, std::milli>(ms);
    while (!stop.load() && clock_t_::now() < wake)
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
}

// ---------------------------------------------------------------------------
// Trace parsing
// ---------------------------------------------------------------------------

static InterQueryDelay parse_delay(const json& j)
{
    InterQueryDelay d;
    auto dist = j.at("distribution").get<std::string>();
    if (dist == "immediate") {
        d.dist = InterQueryDelay::Dist::Immediate;
    } else if (dist == "fixed") {
        d.dist     = InterQueryDelay::Dist::Fixed;
        d.value_ms = j.at("value_ms").get<double>();
    } else if (dist == "uniform") {
        d.dist   = InterQueryDelay::Dist::Uniform;
        d.min_ms = j.at("min_ms").get<double>();
        d.max_ms = j.at("max_ms").get<double>();
    } else if (dist == "exponential") {
        d.dist    = InterQueryDelay::Dist::Exponential;
        d.mean_ms = j.at("mean_ms").get<double>();
    } else {
        throw std::runtime_error("Unknown delay distribution: " + dist);
    }
    return d;
}

static TraceConfig parse_trace(const std::string& path)
{
    std::ifstream f(path);
    if (!f) throw std::runtime_error("Cannot open trace file: " + path);
    json j = json::parse(f, nullptr, /*exceptions=*/true, /*ignore_comments=*/true);

    TraceConfig cfg;

    for (auto& [id, qj] : j.at("queries").items()) {
        QueryDef q;
        q.sql_file = qj.at("sql_file").get<std::string>();
        if (qj.contains("variables"))
            for (auto& [k, v] : qj.at("variables").items())
                q.variables[k] = v.get<std::string>();
        cfg.queries[id] = std::move(q);
    }

    for (auto& [id, cj] : j.at("client_types").items()) {
        ClientType ct;
        ct.id      = id;
        ct.queries  = cj.at("queries").get<std::vector<std::string>>();
        auto ord    = cj.at("order").get<std::string>();
        ct.order    = (ord == "random") ? ClientType::Order::Random
                                        : ClientType::Order::Sequential;
        ct.delay    = parse_delay(cj.at("inter_query_delay"));
        ct.rounds   = cj.at("rounds").get<int>();
        cfg.client_types[id] = std::move(ct);
    }

    auto& sim = j.at("simulation");
    for (auto& ce : sim.at("clients"))
        cfg.simulation.clients.push_back({ce.at("type").get<std::string>(),
                                          ce.at("count").get<int>()});

    if (sim.contains("seed"))
        cfg.simulation.seed = sim.at("seed").get<uint64_t>();
    if (sim.contains("max_duration_s"))
        cfg.simulation.max_duration_s = sim.at("max_duration_s").get<int>();

    return cfg;
}

// ---------------------------------------------------------------------------
// Client thread
// ---------------------------------------------------------------------------

static void run_client(int                                      client_id,
                       const ClientType&                        ct,
                       const std::map<std::string, QueryDef>&   query_defs,
                       duckdb::DuckDB&                          db,
                       std::mt19937_64                          rng,
                       const std::atomic<bool>&                 stop,
                       clock_t_::time_point                     t_start,
                       std::ofstream&                           csv,
                       std::mutex&                              csv_mtx)
{
    duckdb::Connection con(db);

    int max_iters = (ct.rounds == 0) ? std::numeric_limits<int>::max() : ct.rounds;

    std::uniform_int_distribution<size_t> pick(0, ct.queries.size() - 1);

    for (int iter = 0; iter < max_iters && !stop.load(); ++iter) {
        size_t idx = (ct.order == ClientType::Order::Random)
                   ? pick(rng)
                   : static_cast<size_t>(iter) % ct.queries.size();

        const std::string& qid  = ct.queries[idx];
        const QueryDef&    qdef = query_defs.at(qid);

        std::string sql;
        try {
            sql = load_sql(qdef.sql_file, qdef.variables);
        } catch (std::exception& e) {
            std::cerr << "client " << client_id << " (" << ct.id << ")"
                      << " iter " << iter << ": error loading " << qid
                      << ": " << e.what() << "\n";
            continue;
        }

        double start_ms = ms_since(t_start);
        auto   res      = con.Query(sql);
        double end_ms   = ms_since(t_start);

        if (res->HasError()) {
            std::cerr << "client " << client_id << " (" << ct.id << ")"
                      << " iter " << iter << " " << qid
                      << ": query error: " << res->GetError() << "\n";
        } else {
            std::lock_guard<std::mutex> lk(csv_mtx);
            csv << client_id    << ','
                << ct.id        << ','
                << iter         << ','
                << qid          << ','
                << start_ms     << ','
                << end_ms       << ','
                << (end_ms - start_ms) << '\n';
        }

        double delay = sample_delay(ct.delay, rng);
        if (delay > 0)
            sleep_interruptible(delay, stop);
    }
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

int main(int argc, char* argv[])
{
    if (argc < 2) {
        std::cerr << "Usage: " << argv[0] << " <trace.json> [--threads N] [--memory SIZE]\n"
                  << "  SIZE examples: 4GB, 512MB, 1024KB\n";
        return 1;
    }

    std::optional<int>      num_threads;
    std::optional<uint64_t> memory_limit_bytes;

    for (int i = 2; i < argc - 1; ++i) {
        std::string_view arg{argv[i]};
        if (arg == "--threads") {
            num_threads = std::stoi(argv[++i]);
        } else if (arg == "--memory") {
            std::string s{argv[++i]};
            // parse optional suffix: B, KB, MB, GB, TB (case-insensitive)
            uint64_t multiplier = 1;
            std::string upper;
            for (char c : s) upper += static_cast<char>(std::toupper(c));
            if      (upper.size() > 2 && upper.substr(upper.size()-2) == "KB") { multiplier = 1ULL<<10; s.resize(s.size()-2); }
            else if (upper.size() > 2 && upper.substr(upper.size()-2) == "MB") { multiplier = 1ULL<<20; s.resize(s.size()-2); }
            else if (upper.size() > 2 && upper.substr(upper.size()-2) == "GB") { multiplier = 1ULL<<30; s.resize(s.size()-2); }
            else if (upper.size() > 2 && upper.substr(upper.size()-2) == "TB") { multiplier = 1ULL<<40; s.resize(s.size()-2); }
            else if (upper.size() > 1 && upper.back() == 'B')                  { multiplier = 1;        s.resize(s.size()-1); }
            memory_limit_bytes = std::stoull(s) * multiplier;
        }
    }

    TraceConfig cfg;
    try {
        cfg = parse_trace(argv[1]);
    } catch (std::exception& e) {
        std::cerr << "Failed to parse trace: " << e.what() << "\n";
        return 1;
    }

    // Validate all query references
    for (auto& [tid, ct] : cfg.client_types) {
        for (auto& qid : ct.queries) {
            if (!cfg.queries.count(qid)) {
                std::cerr << "Client type '" << tid
                          << "' references unknown query '" << qid << "'\n";
                return 1;
            }
        }
    }
    for (auto& spec : cfg.simulation.clients) {
        if (!cfg.client_types.count(spec.type)) {
            std::cerr << "Simulation references unknown client type '"
                      << spec.type << "'\n";
            return 1;
        }
    }

    std::ofstream csv("trace_result.csv");
    if (!csv) {
        std::cerr << "Cannot open trace_result.csv for writing\n";
        return 1;
    }
    csv << "client_id,client_type,iteration,query_id,start_ms,end_ms,elapsed_ms\n";

    uint64_t base_seed = cfg.simulation.seed.value_or(
        static_cast<uint64_t>(clock_t_::now().time_since_epoch().count()));

    duckdb::DBConfig db_config;
    if (num_threads)
        db_config.options.maximum_threads = static_cast<uint64_t>(*num_threads);
    if (memory_limit_bytes)
        db_config.options.maximum_memory = *memory_limit_bytes;
    duckdb::DuckDB db(nullptr, &db_config);

    std::atomic<bool>    stop{false};
    std::mutex           csv_mtx;
    std::vector<std::thread> threads;

    auto t_start = clock_t_::now();

    int client_id = 0;
    for (auto& spec : cfg.simulation.clients) {
        const ClientType& ct = cfg.client_types.at(spec.type);
        for (int i = 0; i < spec.count; ++i, ++client_id) {
            std::mt19937_64 rng(base_seed ^ (static_cast<uint64_t>(client_id)
                                             * 6364136223846793005ULL + 1442695040888963407ULL));
            threads.emplace_back(run_client,
                client_id, std::cref(ct), std::cref(cfg.queries),
                std::ref(db), std::move(rng), std::cref(stop), t_start,
                std::ref(csv), std::ref(csv_mtx));
        }
    }

    // Optional wall-clock limit — polls stop so it exits promptly when clients finish
    std::thread timer;
    if (cfg.simulation.max_duration_s) {
        int limit = *cfg.simulation.max_duration_s;
        timer = std::thread([&stop, limit] {
            auto deadline = clock_t_::now() + std::chrono::seconds(limit);
            while (!stop.load() && clock_t_::now() < deadline)
                std::this_thread::sleep_for(std::chrono::milliseconds(50));
            stop.store(true);
        });
    }

    for (auto& t : threads) t.join();
    stop.store(true);
    if (timer.joinable()) timer.join();

    csv.close();
    std::cerr << "Results written to trace_result.csv\n";

    return 0;
}
