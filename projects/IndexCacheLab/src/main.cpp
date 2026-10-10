#include "index_cache.hpp"

#include <iomanip>
#include <iostream>
#include <map>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace {

std::string key_for(std::size_t value) {
    std::string digits = std::to_string(value);
    return "k" + std::string(6U - digits.size(), '0') + digits;
}

void report_index_correctness() {
    constexpr std::size_t entries = 1024U;
    m06::HashIndex hash;
    m06::SkipListIndex skiplist;
    m06::TreeIndex tree;
    std::map<std::string, std::string> reference;

    for (std::size_t index = 0U; index < entries; ++index) {
        const std::string key = key_for(index);
        const std::string value = "value-" + std::to_string(index);
        hash.put(key, value);
        skiplist.put(key, value);
        tree.put(key, value);
        reference[key] = value;
    }

    for (std::size_t index = 0U; index < entries; ++index) {
        const std::string key = key_for(index);
        const auto expected = reference.at(key);
        if (hash.get(key) != expected || skiplist.get(key) != expected || tree.get(key) != expected) {
            throw std::runtime_error("index point lookup mismatch");
        }
    }

    const std::string lower = key_for(100U);
    const std::string upper = key_for(120U);
    std::vector<m06::Entry> expected_range;
    for (auto current = reference.lower_bound(lower);
         current != reference.end() && current->first < upper; ++current) {
        expected_range.push_back(*current);
    }
    if (hash.range(lower, upper) != expected_range ||
        skiplist.range(lower, upper) != expected_range ||
        tree.range(lower, upper) != expected_range) {
        throw std::runtime_error("index range lookup mismatch");
    }

    std::cout << "index_workload: entries=" << entries << " puts=" << entries
              << " point_gets=" << entries << " range=[" << lower << ',' << upper
              << ") matches=" << expected_range.size()
              << " correctness=PASS (hash/skip-list/tree vs ordered reference)\n";
}

void report_cache_comparison() {
    const auto trace = m06::make_fixed_trace();
    const auto lru = m06::simulate_lru(trace, m06::kCacheCapacity);
    const auto fifo = m06::simulate_fifo(trace, m06::kCacheCapacity);

    std::cout << "trace: seed=0x" << std::hex << m06::kTraceSeed << std::dec
              << " accesses=" << trace.size() << " checksum_fnv1a64=0x"
              << std::hex << m06::trace_checksum(trace) << std::dec
              << " hot_keys=24 scan_keys=128 broad_keys=256\n";
    std::cout << "cache: capacity=" << m06::kCacheCapacity << " entries; cold start; same trace\n";
    std::cout << std::fixed << std::setprecision(4);
    std::cout << "LRU:  hits=" << lru.hits << " misses=" << lru.misses
              << " hit_rate=" << lru.hit_rate() * 100.0 << "%\n";
    std::cout << "FIFO: hits=" << fifo.hits << " misses=" << fifo.misses
              << " hit_rate=" << fifo.hit_rate() * 100.0 << "%\n";
}

} // namespace

int main(int argc, char **argv) {
    if (argc > 2 || (argc == 2 && std::string_view(argv[1]) != "--report")) {
        std::cerr << "usage: index_cache_lab [--report]\n";
        return 2;
    }

    std::cout << "IndexCacheLab: deterministic correctness and cache-policy comparison\n";
    std::cout << "compiler: " << __VERSION__ << " (C++ standard level " << __cplusplus << ")\n";
    report_index_correctness();
    report_cache_comparison();
    std::cout << "limits: synthetic in-memory operations only; no elapsed-time benchmark, IO, or RSS claim\n";
    return 0;
}
