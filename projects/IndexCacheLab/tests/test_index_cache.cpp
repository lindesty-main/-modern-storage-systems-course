#include "index_cache.hpp"

#include <cassert>
#include <cstdint>
#include <iostream>
#include <map>
#include <optional>
#include <string>
#include <utility>
#include <vector>

namespace {

template <typename Index>
void check_index_contract() {
    Index index;
    index.put("b", "two");
    index.put("a", "one");
    index.put("b", "updated");
    assert(index.get("b") == std::optional<std::string>("updated"));
    assert(!index.get("missing"));
    assert(index.erase("a"));
    assert(!index.erase("a"));
    assert((index.range("b", "z") ==
            std::vector<std::pair<std::string, std::string>>{{"b", "updated"}}));
    assert(index.range("z", "a").empty());
}

template <typename Index>
void check_index_against_reference() {
    Index index;
    std::map<std::string, std::string> reference;
    std::uint32_t state = 0x4d3036U;
    for (int operation = 0; operation < 4000; ++operation) {
        state = state * 1664525U + 1013904223U;
        const auto id = state % 257U;
        const std::string key = "key-" + std::to_string(id);
        state = state * 1664525U + 1013904223U;
        switch (state % 3U) {
        case 0: {
            const std::string value = "value-" + std::to_string(state);
            index.put(key, value);
            reference[key] = value;
            break;
        }
        case 1: {
            const bool actual = index.erase(key);
            const bool expected = reference.erase(key) != 0U;
            assert(actual == expected);
            break;
        }
        default: {
            const auto actual = index.get(key);
            const auto found = reference.find(key);
            const std::optional<std::string> expected =
                found == reference.end() ? std::nullopt : std::optional<std::string>(found->second);
            assert(actual == expected);
            break;
        }
        }
    }

    const auto actual = index.range("key-0", "key-999");
    const std::vector<std::pair<std::string, std::string>> expected(reference.begin(), reference.end());
    assert(actual == expected);
    for (const auto &[key, value] : reference) {
        assert(index.get(key) == std::optional<std::string>(value));
    }
}

void check_cache_policy_semantics() {
    const std::vector<std::uint32_t> trace{1U, 2U, 3U, 1U, 4U, 2U};
    const auto lru = m06::simulate_lru(trace, 3U);
    const auto fifo = m06::simulate_fifo(trace, 3U);
    assert(lru.hits == 1U && lru.misses == 5U);
    assert(fifo.hits == 2U && fifo.misses == 4U);

    const auto empty_lru = m06::simulate_lru(trace, 0U);
    const auto empty_fifo = m06::simulate_fifo(trace, 0U);
    assert(empty_lru.hits == 0U && empty_lru.misses == trace.size());
    assert(empty_fifo.hits == 0U && empty_fifo.misses == trace.size());
}

void check_fixed_trace_reproducibility() {
    const auto first = m06::make_fixed_trace();
    const auto second = m06::make_fixed_trace();
    assert(first == second);
    assert(first.size() == m06::kTraceLength);

    const auto lru = m06::simulate_lru(first, m06::kCacheCapacity);
    const auto fifo = m06::simulate_fifo(first, m06::kCacheCapacity);
    assert(lru.hits + lru.misses == first.size());
    assert(fifo.hits + fifo.misses == first.size());
    assert(m06::trace_checksum(first) == 0x766801a21e475d3aULL);
    assert(lru.hits == 8251U && lru.misses == 3749U);
    assert(fifo.hits == 8243U && fifo.misses == 3757U);
}

} // namespace

int main() {
    check_index_contract<m06::HashIndex>();
    check_index_contract<m06::SkipListIndex>();
    check_index_contract<m06::TreeIndex>();
    check_index_against_reference<m06::HashIndex>();
    check_index_against_reference<m06::SkipListIndex>();
    check_index_against_reference<m06::TreeIndex>();
    check_cache_policy_semantics();
    check_fixed_trace_reproducibility();
    std::cout << "PASS: index correctness, cache semantics, and fixed-trace acceptance\n";
    return 0;
}
