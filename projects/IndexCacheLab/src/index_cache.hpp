#pragma once

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <list>
#include <map>
#include <optional>
#include <random>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace m06 {

using Entry = std::pair<std::string, std::string>;

class HashIndex {
public:
    HashIndex() : buckets_(kInitialBuckets) {}

    void put(const std::string &key, const std::string &value) {
        auto &bucket = buckets_[bucket_for(key, buckets_.size())];
        for (auto &entry : bucket) {
            if (entry.first == key) {
                entry.second = value;
                return;
            }
        }
        bucket.emplace_back(key, value);
        ++size_;
        if (size_ * 4U > buckets_.size() * 3U) {
            rehash(buckets_.size() * 2U);
        }
    }

    [[nodiscard]] std::optional<std::string> get(const std::string &key) const {
        const auto &bucket = buckets_[bucket_for(key, buckets_.size())];
        for (const auto &entry : bucket) {
            if (entry.first == key) return entry.second;
        }
        return std::nullopt;
    }

    [[nodiscard]] bool erase(const std::string &key) {
        auto &bucket = buckets_[bucket_for(key, buckets_.size())];
        const auto found = std::find_if(bucket.begin(), bucket.end(),
                                        [&key](const Entry &entry) { return entry.first == key; });
        if (found == bucket.end()) return false;
        bucket.erase(found);
        --size_;
        return true;
    }

    [[nodiscard]] std::vector<Entry> range(const std::string &lower,
                                           const std::string &upper) const {
        std::vector<Entry> result;
        if (!(lower < upper)) return result;
        for (const auto &bucket : buckets_) {
            for (const auto &entry : bucket) {
                if (lower <= entry.first && entry.first < upper) result.push_back(entry);
            }
        }
        std::sort(result.begin(), result.end(),
                  [](const Entry &left, const Entry &right) { return left.first < right.first; });
        return result;
    }

    [[nodiscard]] std::size_t size() const noexcept { return size_; }

private:
    static constexpr std::size_t kInitialBuckets = 16U;
    std::vector<std::vector<Entry>> buckets_;
    std::size_t size_ = 0U;

    [[nodiscard]] static std::size_t bucket_for(const std::string &key,
                                                std::size_t bucket_count) noexcept {
        // FNV-1a is used only for a deterministic teaching example, not cryptography.
        std::uint64_t hash = 14695981039346656037ULL;
        for (const char character : key) {
            const auto byte = static_cast<unsigned char>(character);
            hash ^= static_cast<std::uint64_t>(byte);
            hash *= 1099511628211ULL;
        }
        return static_cast<std::size_t>(hash % static_cast<std::uint64_t>(bucket_count));
    }

    void rehash(std::size_t new_bucket_count) {
        std::vector<std::vector<Entry>> replacement(new_bucket_count);
        for (auto &bucket : buckets_) {
            for (auto &entry : bucket) {
                const auto slot = bucket_for(entry.first, new_bucket_count);
                replacement[slot].push_back(std::move(entry));
            }
        }
        buckets_ = std::move(replacement);
    }
};

class SkipListIndex {
public:
    SkipListIndex() : head_(std::string{}, std::string{}, kMaxLevels), random_(kSeed) {}
    ~SkipListIndex() { clear(); }

    SkipListIndex(const SkipListIndex &) = delete;
    SkipListIndex &operator=(const SkipListIndex &) = delete;

    void put(const std::string &key, const std::string &value) {
        std::array<Node *, kMaxLevels> update{};
        Node *current = &head_;
        for (std::size_t level = current_level_; level-- > 0U;) {
            while (current->next[level] != nullptr && current->next[level]->key < key) {
                current = current->next[level];
            }
            update[level] = current;
        }

        current = current->next[0];
        if (current != nullptr && current->key == key) {
            current->value = value;
            return;
        }

        const std::size_t node_level = random_level();
        if (node_level > current_level_) {
            for (std::size_t level = current_level_; level < node_level; ++level) {
                update[level] = &head_;
            }
            current_level_ = node_level;
        }
        auto *node = new Node(key, value, node_level);
        for (std::size_t level = 0U; level < node_level; ++level) {
            node->next[level] = update[level]->next[level];
            update[level]->next[level] = node;
        }
        ++size_;
    }

    [[nodiscard]] std::optional<std::string> get(const std::string &key) const {
        const Node *current = find_predecessor(key);
        current = current->next[0];
        if (current != nullptr && current->key == key) return current->value;
        return std::nullopt;
    }

    [[nodiscard]] bool erase(const std::string &key) {
        std::array<Node *, kMaxLevels> update{};
        Node *current = &head_;
        for (std::size_t level = current_level_; level-- > 0U;) {
            while (current->next[level] != nullptr && current->next[level]->key < key) {
                current = current->next[level];
            }
            update[level] = current;
        }
        current = current->next[0];
        if (current == nullptr || current->key != key) return false;

        for (std::size_t level = 0U; level < current->next.size(); ++level) {
            update[level]->next[level] = current->next[level];
        }
        delete current;
        --size_;
        while (current_level_ > 1U && head_.next[current_level_ - 1U] == nullptr) {
            --current_level_;
        }
        return true;
    }

    [[nodiscard]] std::vector<Entry> range(const std::string &lower,
                                           const std::string &upper) const {
        std::vector<Entry> result;
        if (!(lower < upper)) return result;
        const Node *current = find_predecessor(lower)->next[0];
        while (current != nullptr && current->key < upper) {
            result.emplace_back(current->key, current->value);
            current = current->next[0];
        }
        return result;
    }

    [[nodiscard]] std::size_t size() const noexcept { return size_; }

private:
    static constexpr std::size_t kMaxLevels = 16U;
    static constexpr std::uint32_t kSeed = 0x4d3036U;

    struct Node {
        std::string key;
        std::string value;
        std::vector<Node *> next;

        Node(std::string node_key, std::string node_value, std::size_t levels)
            : key(std::move(node_key)), value(std::move(node_value)), next(levels, nullptr) {}
    };

    Node head_;
    std::size_t current_level_ = 1U;
    std::size_t size_ = 0U;
    std::mt19937 random_;

    [[nodiscard]] std::size_t random_level() {
        std::size_t level = 1U;
        while (level < kMaxLevels && (static_cast<std::uint32_t>(random_()) & 1U) != 0U) {
            ++level;
        }
        return level;
    }

    [[nodiscard]] const Node *find_predecessor(const std::string &key) const {
        const Node *current = &head_;
        for (std::size_t level = current_level_; level-- > 0U;) {
            while (current->next[level] != nullptr && current->next[level]->key < key) {
                current = current->next[level];
            }
        }
        return current;
    }

    void clear() noexcept {
        Node *current = head_.next[0];
        while (current != nullptr) {
            Node *next = current->next[0];
            delete current;
            current = next;
        }
        for (Node *&next : head_.next) next = nullptr;
        size_ = 0U;
        current_level_ = 1U;
    }
};

class TreeIndex {
public:
    void put(const std::string &key, const std::string &value) { values_[key] = value; }

    [[nodiscard]] std::optional<std::string> get(const std::string &key) const {
        const auto found = values_.find(key);
        if (found == values_.end()) return std::nullopt;
        return found->second;
    }

    [[nodiscard]] bool erase(const std::string &key) { return values_.erase(key) != 0U; }

    [[nodiscard]] std::vector<Entry> range(const std::string &lower,
                                           const std::string &upper) const {
        std::vector<Entry> result;
        if (!(lower < upper)) return result;
        for (auto current = values_.lower_bound(lower);
             current != values_.end() && current->first < upper; ++current) {
            result.push_back(*current);
        }
        return result;
    }

    [[nodiscard]] std::size_t size() const noexcept { return values_.size(); }

private:
    std::map<std::string, std::string> values_;
};

struct CacheStats {
    std::size_t hits = 0U;
    std::size_t misses = 0U;

    [[nodiscard]] double hit_rate() const noexcept {
        const std::size_t accesses = hits + misses;
        if (accesses == 0U) return 0.0;
        return static_cast<double>(hits) / static_cast<double>(accesses);
    }
};

class LruCache {
public:
    explicit LruCache(std::size_t capacity) : capacity_(capacity) {}

    [[nodiscard]] bool access(std::uint32_t key) {
        const auto found = positions_.find(key);
        if (found != positions_.end()) {
            recency_.splice(recency_.begin(), recency_, found->second);
            return true;
        }
        if (capacity_ == 0U) return false;
        if (positions_.size() == capacity_) {
            positions_.erase(recency_.back());
            recency_.pop_back();
        }
        recency_.push_front(key);
        positions_.emplace(key, recency_.begin());
        return false;
    }

    [[nodiscard]] std::size_t size() const noexcept { return positions_.size(); }

private:
    std::size_t capacity_;
    std::list<std::uint32_t> recency_;
    std::unordered_map<std::uint32_t, std::list<std::uint32_t>::iterator> positions_;
};

class FifoCache {
public:
    explicit FifoCache(std::size_t capacity) : capacity_(capacity) {}

    [[nodiscard]] bool access(std::uint32_t key) {
        if (resident_.contains(key)) return true;
        if (capacity_ == 0U) return false;
        if (resident_.size() == capacity_) {
            resident_.erase(insertion_order_.front());
            insertion_order_.pop_front();
        }
        insertion_order_.push_back(key);
        resident_.insert(key);
        return false;
    }

    [[nodiscard]] std::size_t size() const noexcept { return resident_.size(); }

private:
    std::size_t capacity_;
    std::list<std::uint32_t> insertion_order_;
    std::unordered_set<std::uint32_t> resident_;
};

inline constexpr std::uint32_t kTraceSeed = 0x4d3036U;
inline constexpr std::size_t kTraceLength = 12000U;
inline constexpr std::size_t kCacheCapacity = 32U;

[[nodiscard]] inline std::vector<std::uint32_t> make_fixed_trace() {
    std::mt19937 random(kTraceSeed);
    std::vector<std::uint32_t> trace;
    trace.reserve(kTraceLength);
    for (std::size_t index = 0U; index < kTraceLength; ++index) {
        const std::size_t phase = index % 1000U;
        const std::uint32_t draw = static_cast<std::uint32_t>(random());
        if (phase < 700U) {
            trace.push_back(draw % 24U);                    // hot set
        } else if (phase < 900U) {
            trace.push_back(1000U + static_cast<std::uint32_t>(index % 128U)); // scan set
        } else {
            trace.push_back(draw % 256U);                   // broad random set
        }
    }
    return trace;
}

[[nodiscard]] inline std::uint64_t trace_checksum(const std::vector<std::uint32_t> &trace) noexcept {
    std::uint64_t hash = 14695981039346656037ULL;
    for (const std::uint32_t item : trace) {
        for (unsigned int byte_index = 0U; byte_index < 4U; ++byte_index) {
            const auto byte = static_cast<std::uint8_t>((item >> (byte_index * 8U)) & 0xffU);
            hash ^= static_cast<std::uint64_t>(byte);
            hash *= 1099511628211ULL;
        }
    }
    return hash;
}

template <typename Cache>
[[nodiscard]] CacheStats simulate(const std::vector<std::uint32_t> &trace,
                                  std::size_t capacity) {
    Cache cache(capacity);
    CacheStats stats;
    for (const std::uint32_t key : trace) {
        if (cache.access(key)) {
            ++stats.hits;
        } else {
            ++stats.misses;
        }
    }
    return stats;
}

[[nodiscard]] inline CacheStats simulate_lru(const std::vector<std::uint32_t> &trace,
                                             std::size_t capacity) {
    return simulate<LruCache>(trace, capacity);
}

[[nodiscard]] inline CacheStats simulate_fifo(const std::vector<std::uint32_t> &trace,
                                              std::size_t capacity) {
    return simulate<FifoCache>(trace, capacity);
}

} // namespace m06
