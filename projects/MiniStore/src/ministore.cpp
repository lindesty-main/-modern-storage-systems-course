#include <algorithm>
#include <array>
#include <cerrno>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <iomanip>
#include <iostream>
#include <map>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <fcntl.h>
#include <unistd.h>
#include <utility>
#include <vector>

namespace {

constexpr std::size_t kMaxKeyBytes = 128;
constexpr std::size_t kMaxValueBytes = 1024U * 1024U;
constexpr std::size_t kMaxWalBytes = 64U * 1024U * 1024U;
constexpr std::size_t kMaxTableBytes = 256U * 1024U * 1024U;
constexpr std::size_t kFormatSize = 16;
constexpr std::size_t kTableHeaderSize = 48;
constexpr std::size_t kWalHeaderSize = 19;
constexpr std::size_t kWalTrailerSize = 4;
constexpr std::uint32_t kFormatVersion = 1;
constexpr std::string_view kFormatMagic = "MSTOR001";
constexpr std::string_view kTableMagic = "MSTBL001";
constexpr std::string_view kWalMagic = "MWR1";
constexpr std::uint8_t kPut = 1;
constexpr std::uint8_t kDelete = 2;

class StoreError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

class UsageError : public StoreError {
public:
    using StoreError::StoreError;
};

class NotFound : public StoreError {
public:
    using StoreError::StoreError;
};

[[noreturn]] void system_error(std::string_view operation) {
    const int saved_errno = errno;
    throw StoreError(std::string(operation) + ": " + std::strerror(saved_errno));
}

bool test_failpoint_requested(std::string_view name) {
#ifdef MINISTORE_ENABLE_TEST_FAILPOINTS
    const char* requested = std::getenv("MINISTORE_TEST_FAILPOINT");
    return requested != nullptr && name == requested;
#else
    (void)name;
    return false;
#endif
}

void pause_at_test_failpoint(std::string_view name) {
#ifdef MINISTORE_ENABLE_TEST_FAILPOINTS
    const std::string marker = "TEST_FAILPOINT_REACHED:" + std::string(name) + "\n";
    (void)::write(STDERR_FILENO, marker.data(), marker.size());
    for (;;) {
        (void)::pause();
    }
#else
    (void)name;
#endif
}

void maybe_pause_at_test_failpoint(std::string_view name) {
    if (test_failpoint_requested(name)) {
        pause_at_test_failpoint(name);
    }
}

class Fd {
public:
    explicit Fd(int value = -1) : value_(value) {}
    ~Fd() {
        if (value_ >= 0) {
            (void)::close(value_);
        }
    }
    Fd(const Fd&) = delete;
    Fd& operator=(const Fd&) = delete;
    Fd(Fd&& other) noexcept : value_(std::exchange(other.value_, -1)) {}
    Fd& operator=(Fd&& other) noexcept {
        if (this != &other) {
            if (value_ >= 0) {
                (void)::close(value_);
            }
            value_ = std::exchange(other.value_, -1);
        }
        return *this;
    }
    [[nodiscard]] int get() const { return value_; }

private:
    int value_;
};

void write_all(int fd, const std::uint8_t* data, std::size_t size) {
    std::size_t done = 0;
    while (done < size) {
        const ssize_t written = ::write(fd, data + done, size - done);
        if (written < 0) {
            if (errno == EINTR) {
                continue;
            }
            system_error("write");
        }
        if (written == 0) {
            throw StoreError("write: made no progress");
        }
        done += static_cast<std::size_t>(written);
    }
}

std::size_t pread_available(int fd, std::uint8_t* data, std::size_t size, off_t offset) {
    std::size_t done = 0;
    while (done < size) {
        const ssize_t got = ::pread(fd, data + done, size - done,
                                    offset + static_cast<off_t>(done));
        if (got < 0) {
            if (errno == EINTR) {
                continue;
            }
            system_error("pread");
        }
        if (got == 0) {
            break;
        }
        done += static_cast<std::size_t>(got);
    }
    return done;
}

void sync_data(int fd, std::string_view label) {
#ifdef MINISTORE_ENABLE_TEST_FAILPOINTS
    if (label == "commit WAL record" &&
        test_failpoint_requested("inject_wal_fdatasync_error")) {
        throw StoreError(std::string(label) +
                         ": fdatasync failed (write outcome may be uncertain): "
                         "injected test-only EIO before the syscall");
    }
#endif
    while (::fdatasync(fd) != 0) {
        if (errno == EINTR) {
            continue;
        }
        const int saved_errno = errno;
        throw StoreError(std::string(label) + ": fdatasync failed (write outcome may be uncertain): " +
                         std::strerror(saved_errno));
    }
}

void sync_directory(int fd) {
    while (::fsync(fd) != 0) {
        if (errno == EINTR) {
            continue;
        }
        system_error("fsync database directory");
    }
}

void require_private_regular(int fd, std::string_view label) {
    struct stat st {};
    if (::fstat(fd, &st) != 0) {
        system_error(std::string("fstat ") + std::string(label));
    }
    if (!S_ISREG(st.st_mode) || st.st_uid != ::geteuid() || st.st_nlink != 1 ||
        (st.st_mode & 0077) != 0) {
        throw StoreError(std::string(label) +
                         " must be a private, single-link regular file owned by this user");
    }
}

std::uint32_t crc32(const std::uint8_t* data, std::size_t size) {
    std::uint32_t crc = 0xffffffffU;
    for (std::size_t i = 0; i < size; ++i) {
        crc ^= data[i];
        for (int bit = 0; bit < 8; ++bit) {
            const std::uint32_t mask = 0U - (crc & 1U);
            crc = (crc >> 1U) ^ (0xedb88320U & mask);
        }
    }
    return ~crc;
}

void append_u16(std::vector<std::uint8_t>& out, std::uint16_t value) {
    out.push_back(static_cast<std::uint8_t>(value & 0xffU));
    out.push_back(static_cast<std::uint8_t>((value >> 8U) & 0xffU));
}

void append_u32(std::vector<std::uint8_t>& out, std::uint32_t value) {
    for (unsigned int shift = 0; shift < 32; shift += 8) {
        out.push_back(static_cast<std::uint8_t>((value >> shift) & 0xffU));
    }
}

void append_u64(std::vector<std::uint8_t>& out, std::uint64_t value) {
    for (unsigned int shift = 0; shift < 64; shift += 8) {
        out.push_back(static_cast<std::uint8_t>((value >> shift) & 0xffU));
    }
}

std::uint16_t read_u16(const std::uint8_t* data) {
    return static_cast<std::uint16_t>(data[0]) |
           static_cast<std::uint16_t>(static_cast<std::uint16_t>(data[1]) << 8U);
}

std::uint32_t read_u32(const std::uint8_t* data) {
    std::uint32_t value = 0;
    for (unsigned int i = 0; i < 4; ++i) {
        value |= static_cast<std::uint32_t>(data[i]) << (i * 8U);
    }
    return value;
}

std::uint64_t read_u64(const std::uint8_t* data) {
    std::uint64_t value = 0;
    for (unsigned int i = 0; i < 8; ++i) {
        value |= static_cast<std::uint64_t>(data[i]) << (i * 8U);
    }
    return value;
}

void append_string(std::vector<std::uint8_t>& out, std::string_view value) {
    out.insert(out.end(), value.begin(), value.end());
}

bool valid_key(std::string_view key) {
    if (key.empty() || key.size() > kMaxKeyBytes) {
        return false;
    }
    for (const unsigned char ch : key) {
        const bool alpha = (ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z');
        const bool digit = ch >= '0' && ch <= '9';
        if (!(alpha || digit || ch == '.' || ch == '_' || ch == '-')) {
            return false;
        }
    }
    return true;
}

void require_key(std::string_view key) {
    if (!valid_key(key)) {
        throw UsageError("key must be 1..128 ASCII characters from [A-Za-z0-9._-]");
    }
}

bool is_object_namespace(std::string_view key) {
    return key.starts_with("@obj:");
}

bool valid_bucket(std::string_view bucket) {
    if (bucket.empty() || bucket.size() > 63) {
        return false;
    }
    for (const unsigned char ch : bucket) {
        if (!((ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') ||
              (ch >= '0' && ch <= '9'))) {
            return false;
        }
    }
    return true;
}

bool valid_object_key(std::string_view key) {
    if (key.empty()) {
        return false;
    }
    for (const unsigned char ch : key) {
        const bool alpha = (ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z');
        const bool digit = ch >= '0' && ch <= '9';
        if (!(alpha || digit || ch == '.' || ch == '_' || ch == '-')) {
            return false;
        }
    }
    return true;
}

void require_bucket(std::string_view bucket) {
    if (!valid_bucket(bucket)) {
        throw UsageError("bucket must be 1..63 ASCII alphanumeric characters");
    }
}

void require_object_key(std::string_view bucket, std::string_view key) {
    require_bucket(bucket);
    if (!valid_object_key(key) || key.size() > 122U - bucket.size()) {
        throw UsageError("object key must use [A-Za-z0-9._-] and fit the 128-byte internal key limit");
    }
}

void require_object_prefix(std::string_view bucket, std::string_view prefix) {
    require_bucket(bucket);
    if ((!prefix.empty() && !valid_object_key(prefix)) || prefix.size() > 122U - bucket.size()) {
        throw UsageError("object prefix must use [A-Za-z0-9._-] and fit the 128-byte internal key limit");
    }
}

std::string object_store_key(std::string_view bucket, std::string_view key) {
    require_object_key(bucket, key);
    return "@obj:" + std::string(bucket) + ":" + std::string(key);
}

bool valid_object_store_key(std::string_view key) {
    constexpr std::string_view prefix = "@obj:";
    if (!key.starts_with(prefix)) {
        return false;
    }
    const std::size_t separator = key.find(':', prefix.size());
    if (separator == std::string_view::npos) {
        return false;
    }
    const std::string_view bucket = key.substr(prefix.size(), separator - prefix.size());
    const std::string_view object_key = key.substr(separator + 1);
    return valid_bucket(bucket) && valid_object_key(object_key) &&
           object_key.size() <= 122U - bucket.size();
}

bool valid_storage_key(std::string_view key) {
    return valid_key(key) || valid_object_store_key(key);
}

std::pair<Fd, std::string> open_parent_and_name(const std::filesystem::path& input) {
    std::error_code ec;
    const auto absolute = std::filesystem::absolute(input, ec).lexically_normal();
    if (ec) {
        throw StoreError("resolve database path: " + ec.message());
    }
    const std::string name = absolute.filename().string();
    if (name.empty() || name == "." || name == "..") {
        throw UsageError("database path must name a directory below an existing parent");
    }
    const auto canonical_parent = std::filesystem::canonical(absolute.parent_path(), ec);
    if (ec) {
        throw StoreError("database parent must already exist: " + ec.message());
    }
    const int parent_fd = ::open(canonical_parent.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (parent_fd < 0) {
        system_error("open database parent directory");
    }
    return {Fd(parent_fd), name};
}

Fd open_database_directory(int parent_fd, const std::string& name) {
    const int raw = ::openat(parent_fd, name.c_str(),
                             O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (raw < 0) {
        system_error("open database directory (symlinks are not accepted)");
    }
    Fd dir(raw);
    struct stat st {};
    if (::fstat(dir.get(), &st) != 0) {
        system_error("fstat database directory");
    }
    if (!S_ISDIR(st.st_mode) || st.st_uid != ::geteuid() || (st.st_mode & 0077) != 0) {
        throw StoreError("database directory must be owned by this user and have mode 0700");
    }
    return dir;
}

std::optional<std::vector<std::uint8_t>> read_private_file_at(
    int dir_fd, const char* name, std::size_t maximum, bool may_be_missing) {
    const int raw = ::openat(dir_fd, name, O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC);
    if (raw < 0) {
        if (may_be_missing && errno == ENOENT) {
            return std::nullopt;
        }
        system_error(std::string("open ") + name + " (symlinks are not accepted)");
    }
    Fd file(raw);
    require_private_regular(file.get(), name);
    struct stat st {};
    if (::fstat(file.get(), &st) != 0) {
        system_error(std::string("fstat ") + name);
    }
    if (st.st_size < 0 || static_cast<std::uint64_t>(st.st_size) > maximum) {
        throw StoreError(std::string(name) + " exceeds the supported size limit");
    }
    const auto size = static_cast<std::size_t>(st.st_size);
    std::vector<std::uint8_t> bytes(size);
    const std::size_t got = pread_available(file.get(), bytes.data(), size, 0);
    if (got != size) {
        throw StoreError(std::string(name) + " changed or was truncated while reading");
    }
    return bytes;
}

Fd open_lock(int dir_fd) {
    const int raw = ::openat(dir_fd, "LOCK", O_RDWR | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC);
    if (raw < 0) {
        system_error("open LOCK (symlinks are not accepted)");
    }
    Fd lock(raw);
    require_private_regular(lock.get(), "LOCK");
    if (::flock(lock.get(), LOCK_EX | LOCK_NB) != 0) {
        if (errno == EWOULDBLOCK || errno == EAGAIN) {
            throw StoreError("database is already open by another process");
        }
        system_error("flock LOCK");
    }
    return lock;
}

std::vector<std::uint8_t> make_format_file() {
    std::vector<std::uint8_t> bytes;
    bytes.reserve(kFormatSize);
    append_string(bytes, kFormatMagic);
    append_u32(bytes, kFormatVersion);
    append_u32(bytes, crc32(bytes.data(), bytes.size()));
    return bytes;
}

void validate_format_file(const std::vector<std::uint8_t>& bytes) {
    if (bytes.size() != kFormatSize ||
        std::string_view(reinterpret_cast<const char*>(bytes.data()), kFormatMagic.size()) != kFormatMagic ||
        read_u32(bytes.data() + 8) != kFormatVersion ||
        read_u32(bytes.data() + 12) != crc32(bytes.data(), 12)) {
        throw StoreError("FORMAT is missing, corrupt, or uses an unsupported version");
    }
}

void create_private_file(int dir_fd, const char* name, const std::vector<std::uint8_t>& content,
                         bool sync_contents) {
    const int raw = ::openat(dir_fd, name,
                             O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (raw < 0) {
        system_error(std::string("create ") + name);
    }
    Fd file(raw);
    require_private_regular(file.get(), name);
    if (!content.empty()) {
        write_all(file.get(), content.data(), content.size());
    }
    if (sync_contents) {
        sync_data(file.get(), name);
    }
}

void initialize_store(const std::filesystem::path& path) {
    auto [parent, name] = open_parent_and_name(path);
    if (::mkdirat(parent.get(), name.c_str(), 0700) != 0) {
        if (errno == EEXIST) {
            throw StoreError("init refuses to reuse an existing path; choose a new empty directory name");
        }
        system_error("mkdir database directory");
    }
    Fd dir = open_database_directory(parent.get(), name);
    create_private_file(dir.get(), "LOCK", {}, true);
    create_private_file(dir.get(), "WAL", {}, true);
    create_private_file(dir.get(), "FORMAT", make_format_file(), true);
    sync_directory(dir.get());
    sync_directory(parent.get());
}

struct Entry {
    std::uint64_t sequence = 0;
    bool tombstone = false;
    std::vector<std::uint8_t> value;
};

class Store {
public:
    static Store open(const std::filesystem::path& path) {
        auto [parent, name] = open_parent_and_name(path);
        Fd dir = open_database_directory(parent.get(), name);
        Fd lock = open_lock(dir.get());
        Store store(std::move(dir), std::move(lock));
        const auto format = read_private_file_at(store.dir_.get(), "FORMAT", kFormatSize, false);
        validate_format_file(*format);
        store.load_table();
        store.recover_wal();
        store.open_wal_for_append();
        return store;
    }

    Store(const Store&) = delete;
    Store& operator=(const Store&) = delete;
    Store(Store&&) noexcept = default;
    Store& operator=(Store&&) noexcept = default;

    std::uint64_t put(std::string_view key, std::string_view value) {
        require_key(key);
        return put_value(key, value);
    }

    std::uint64_t put_object(std::string_view bucket, std::string_view key,
                             std::string_view value) {
        const std::string stored_key = object_store_key(bucket, key);
        return put_value(stored_key, value);
    }

    const Entry& get_object(std::string_view bucket, std::string_view key) const {
        const std::string stored_key = object_store_key(bucket, key);
        const auto it = entries_.find(stored_key);
        if (it == entries_.end() || it->second.tombstone) {
            throw NotFound("object not found: " + std::string(bucket) + "/" + std::string(key));
        }
        return it->second;
    }

    std::uint64_t erase_object(std::string_view bucket, std::string_view key) {
        const std::string stored_key = object_store_key(bucket, key);
        const auto it = entries_.find(stored_key);
        if (it == entries_.end() || it->second.tombstone) {
            throw NotFound("object not found: " + std::string(bucket) + "/" + std::string(key));
        }
        return append_operation(kDelete, stored_key, {});
    }

    void list_objects(std::string_view bucket, std::string_view prefix) const {
        require_object_prefix(bucket, prefix);
        const std::string bucket_prefix = "@obj:" + std::string(bucket) + ":";
        const std::string match_prefix = bucket_prefix + std::string(prefix);
        for (const auto& [key, entry] : entries_) {
            if (!entry.tombstone && key.starts_with(match_prefix)) {
                std::cout << "object\t" << key.substr(bucket_prefix.size())
                          << "\tsize\t" << entry.value.size() << '\n';
            }
        }
    }

    std::uint64_t erase(std::string_view key) {
        require_key(key);
        const auto it = entries_.find(key);
        if (it == entries_.end() || it->second.tombstone) {
            throw NotFound("key not found: " + std::string(key));
        }
        return append_operation(kDelete, key, {});
    }

    const Entry& get(std::string_view key) const {
        require_key(key);
        const auto it = entries_.find(key);
        if (it == entries_.end() || it->second.tombstone) {
            throw NotFound("key not found: " + std::string(key));
        }
        return it->second;
    }

    void list() const {
        for (const auto& [key, entry] : entries_) {
            if (entry.tombstone || is_object_namespace(key)) {
                continue;
            }
            std::cout << "key\t" << key << "\tvalue_hex\t";
            for (const std::uint8_t byte : entry.value) {
                std::cout << std::hex << std::setw(2) << std::setfill('0')
                          << static_cast<unsigned int>(byte);
            }
            std::cout << std::dec << std::setfill(' ') << '\n';
        }
    }

    std::uint64_t compact() {
        std::vector<std::uint8_t> table = encode_table();
        remove_stale_temp("TABLE.sst.tmp");
        create_temp_and_sync("TABLE.sst.tmp", table);
        if (::renameat(dir_.get(), "TABLE.sst.tmp", dir_.get(), "TABLE.sst") != 0) {
            system_error("rename compacted SSTable");
        }
        maybe_pause_at_test_failpoint("after_table_rename_before_dir_fsync");
        sync_directory(dir_.get());

        remove_stale_temp("WAL.tmp");
        create_temp_and_sync("WAL.tmp", {});
        if (::renameat(dir_.get(), "WAL.tmp", dir_.get(), "WAL") != 0) {
            system_error("rotate WAL");
        }
        maybe_pause_at_test_failpoint("after_wal_rename_before_dir_fsync");
        sync_directory(dir_.get());
        open_wal_for_append();
        wal_size_ = 0;
        return live_key_count();
    }

    [[nodiscard]] std::uint64_t max_sequence() const { return max_sequence_; }
    [[nodiscard]] std::uint64_t live_key_count() const {
        std::uint64_t count = 0;
        for (const auto& [key, entry] : entries_) {
            (void)key;
            if (!entry.tombstone) {
                ++count;
            }
        }
        return count;
    }

private:
    Store(Fd dir, Fd lock) : dir_(std::move(dir)), lock_(std::move(lock)) {}

    std::uint64_t put_value(std::string_view key, std::string_view value) {
        if (value.size() > kMaxValueBytes) {
            throw UsageError("value exceeds the 1 MiB limit");
        }
        const std::vector<std::uint8_t> bytes(value.begin(), value.end());
        check_capacity(key, bytes.size());
        return append_operation(kPut, key, bytes);
    }

    void load_table() {
        const auto maybe_bytes = read_private_file_at(dir_.get(), "TABLE.sst", kMaxTableBytes, true);
        if (!maybe_bytes) {
            return;
        }
        const auto& bytes = *maybe_bytes;
        if (bytes.size() < kTableHeaderSize ||
            std::string_view(reinterpret_cast<const char*>(bytes.data()), kTableMagic.size()) != kTableMagic ||
            read_u32(bytes.data() + 8) != kFormatVersion || read_u32(bytes.data() + 12) != 0) {
            throw StoreError("TABLE.sst has an invalid header or unsupported version");
        }
        const std::uint64_t count = read_u64(bytes.data() + 16);
        const std::uint64_t maximum_sequence = read_u64(bytes.data() + 24);
        const std::uint64_t payload_size = read_u64(bytes.data() + 32);
        const std::uint32_t expected_crc = read_u32(bytes.data() + 40);
        if (read_u32(bytes.data() + 44) != 0 || payload_size != bytes.size() - kTableHeaderSize ||
            expected_crc != crc32(bytes.data() + kTableHeaderSize,
                                  bytes.size() - kTableHeaderSize)) {
            throw StoreError("TABLE.sst length, reserved field, or checksum is invalid");
        }
        if (count > kMaxTableBytes / 14U) {
            throw StoreError("TABLE.sst record count exceeds the supported limit");
        }
        std::size_t offset = kTableHeaderSize;
        std::string previous_key;
        for (std::uint64_t index = 0; index < count; ++index) {
            if (bytes.size() - offset < 14) {
                throw StoreError("TABLE.sst contains a truncated record header");
            }
            const std::uint64_t sequence = read_u64(bytes.data() + offset);
            const std::uint16_t key_size = read_u16(bytes.data() + offset + 8);
            const std::uint32_t value_size = read_u32(bytes.data() + offset + 10);
            offset += 14;
            if (key_size == 0 || key_size > kMaxKeyBytes || value_size > kMaxValueBytes ||
                static_cast<std::uint64_t>(key_size) + value_size > bytes.size() - offset) {
                throw StoreError("TABLE.sst contains invalid record lengths");
            }
            const std::string key(reinterpret_cast<const char*>(bytes.data() + offset), key_size);
            offset += key_size;
            if (!valid_storage_key(key) || (!previous_key.empty() && key <= previous_key) ||
                sequence == 0 || sequence > maximum_sequence) {
                throw StoreError("TABLE.sst keys or sequence numbers are invalid");
            }
            std::vector<std::uint8_t> value(bytes.begin() + static_cast<std::ptrdiff_t>(offset),
                                            bytes.begin() + static_cast<std::ptrdiff_t>(offset + value_size));
            offset += value_size;
            apply(kPut, key, value, sequence);
            previous_key = key;
        }
        if (offset != bytes.size()) {
            throw StoreError("TABLE.sst has trailing bytes after its final record");
        }
        max_sequence_ = maximum_sequence;
    }

    void recover_wal() {
        const auto maybe_bytes = read_private_file_at(dir_.get(), "WAL", kMaxWalBytes, false);
        const auto& bytes = *maybe_bytes;
        const std::uint64_t table_sequence = max_sequence_;
        std::size_t offset = 0;
        std::size_t valid_end = 0;
        std::optional<std::uint64_t> previous_sequence;
        while (offset < bytes.size()) {
            const std::size_t remaining = bytes.size() - offset;
            if (remaining < kWalHeaderSize) {
                break; // A torn final header is the only silently discarded case.
            }
            if (std::string_view(reinterpret_cast<const char*>(bytes.data() + offset), kWalMagic.size()) !=
                kWalMagic) {
                throw StoreError("WAL record magic is invalid (refusing recovery)");
            }
            const std::uint64_t sequence = read_u64(bytes.data() + offset + 4);
            const std::uint8_t operation = bytes[offset + 12];
            const std::uint16_t key_size = read_u16(bytes.data() + offset + 13);
            const std::uint32_t value_size = read_u32(bytes.data() + offset + 15);
            if ((operation != kPut && operation != kDelete) || key_size == 0 ||
                key_size > kMaxKeyBytes || value_size > kMaxValueBytes ||
                (operation == kDelete && value_size != 0)) {
                throw StoreError("WAL record contains invalid operation or lengths");
            }
            const std::uint64_t total64 = kWalHeaderSize + static_cast<std::uint64_t>(key_size) +
                                          value_size + kWalTrailerSize;
            if (total64 > remaining) {
                break; // Partial payload/checksum at the end: discard only this tail.
            }
            const std::size_t total = static_cast<std::size_t>(total64);
            const std::size_t checksum_offset = offset + total - kWalTrailerSize;
            if (read_u32(bytes.data() + checksum_offset) !=
                crc32(bytes.data() + offset, total - kWalTrailerSize)) {
                throw StoreError("WAL checksum mismatch (refusing recovery)");
            }
            if (sequence == 0 || (previous_sequence &&
                (*previous_sequence == UINT64_MAX || sequence != *previous_sequence + 1))) {
                throw StoreError("WAL sequence numbers are not strictly consecutive");
            }
            if (!previous_sequence && sequence != 1 &&
                (table_sequence == UINT64_MAX || sequence != table_sequence + 1)) {
                throw StoreError("WAL starts at an unexpected sequence number");
            }
            const std::string key(reinterpret_cast<const char*>(bytes.data() + offset + kWalHeaderSize),
                                  key_size);
            if (!valid_storage_key(key)) {
                throw StoreError("WAL contains an invalid key");
            }
            if (sequence > table_sequence) {
                if (max_sequence_ == UINT64_MAX || sequence != max_sequence_ + 1) {
                    throw StoreError("WAL sequence does not follow the compacted table");
                }
                const std::size_t value_offset = offset + kWalHeaderSize + key_size;
                std::vector<std::uint8_t> value(
                    bytes.begin() + static_cast<std::ptrdiff_t>(value_offset),
                    bytes.begin() + static_cast<std::ptrdiff_t>(value_offset + value_size));
                apply(operation, key, value, sequence);
                max_sequence_ = sequence;
            }
            previous_sequence = sequence;
            offset += total;
            valid_end = offset;
        }
        if (valid_end != bytes.size()) {
            truncate_wal(valid_end);
        }
        wal_size_ = valid_end;
    }

    void truncate_wal(std::size_t size) {
        const int raw = ::openat(dir_.get(), "WAL", O_WRONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC);
        if (raw < 0) {
            system_error("open WAL for tail repair");
        }
        Fd wal(raw);
        require_private_regular(wal.get(), "WAL");
        if (::ftruncate(wal.get(), static_cast<off_t>(size)) != 0) {
            system_error("truncate incomplete WAL tail");
        }
        sync_data(wal.get(), "repair WAL tail");
    }

    void open_wal_for_append() {
        const int raw = ::openat(dir_.get(), "WAL", O_WRONLY | O_APPEND | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC);
        if (raw < 0) {
            system_error("open WAL for append");
        }
        Fd new_wal(raw);
        require_private_regular(new_wal.get(), "WAL");
        wal_ = std::move(new_wal);
    }

    void check_capacity(std::string_view key, std::size_t value_size) const {
        std::uint64_t projected = live_bytes_;
        const auto existing = entries_.find(key);
        if (existing == entries_.end() || existing->second.tombstone) {
            projected += key.size();
        } else {
            projected -= existing->second.value.size();
        }
        projected += value_size;
        if (projected > kMaxTableBytes - kTableHeaderSize) {
            throw StoreError("live data exceeds the 256 MiB MiniStore limit");
        }
    }

    void apply(std::uint8_t operation, const std::string& key,
               const std::vector<std::uint8_t>& value, std::uint64_t sequence) {
        auto it = entries_.find(key);
        if (operation == kPut) {
            if (it == entries_.end()) {
                live_bytes_ += key.size();
                it = entries_.emplace(key, Entry{}).first;
            } else if (it->second.tombstone) {
                live_bytes_ += key.size();
            } else {
                live_bytes_ -= it->second.value.size();
            }
            live_bytes_ += value.size();
            if (live_bytes_ > kMaxTableBytes - kTableHeaderSize) {
                throw StoreError("live data exceeds the 256 MiB MiniStore limit");
            }
            it->second = Entry{sequence, false, value};
        } else {
            if (it == entries_.end()) {
                it = entries_.emplace(key, Entry{}).first;
            } else if (!it->second.tombstone) {
                live_bytes_ -= key.size() + it->second.value.size();
            }
            it->second = Entry{sequence, true, {}};
        }
    }

    std::vector<std::uint8_t> encode_wal_record(std::uint8_t operation, std::string_view key,
                                                 const std::vector<std::uint8_t>& value,
                                                 std::uint64_t sequence) const {
        std::vector<std::uint8_t> bytes;
        bytes.reserve(kWalHeaderSize + key.size() + value.size() + kWalTrailerSize);
        append_string(bytes, kWalMagic);
        append_u64(bytes, sequence);
        bytes.push_back(operation);
        append_u16(bytes, static_cast<std::uint16_t>(key.size()));
        append_u32(bytes, static_cast<std::uint32_t>(value.size()));
        append_string(bytes, key);
        bytes.insert(bytes.end(), value.begin(), value.end());
        append_u32(bytes, crc32(bytes.data(), bytes.size()));
        return bytes;
    }

    std::uint64_t append_operation(std::uint8_t operation, std::string_view key,
                                   const std::vector<std::uint8_t>& value) {
        if (max_sequence_ == UINT64_MAX) {
            throw StoreError("sequence number space is exhausted");
        }
        const std::uint64_t sequence = max_sequence_ + 1;
        const auto record = encode_wal_record(operation, key, value, sequence);
        if (record.size() > kMaxWalBytes - wal_size_) {
            throw StoreError("WAL reached 64 MiB; run 'compact' before writing more");
        }
        if (test_failpoint_requested("after_partial_wal_append")) {
            constexpr std::size_t kInjectedPrefixBytes = kWalHeaderSize / 2;
            write_all(wal_.get(), record.data(), kInjectedPrefixBytes);
            pause_at_test_failpoint("after_partial_wal_append");
        }
        write_all(wal_.get(), record.data(), record.size());
        maybe_pause_at_test_failpoint("after_wal_write_before_fdatasync");
        sync_data(wal_.get(), "commit WAL record");
        maybe_pause_at_test_failpoint("after_wal_fdatasync_before_apply");
        apply(operation, std::string(key), value, sequence);
        max_sequence_ = sequence;
        wal_size_ += record.size();
        return sequence;
    }

    std::vector<std::uint8_t> encode_table() const {
        std::vector<std::uint8_t> payload;
        for (const auto& [key, entry] : entries_) {
            if (entry.tombstone) {
                continue;
            }
            append_u64(payload, entry.sequence);
            append_u16(payload, static_cast<std::uint16_t>(key.size()));
            append_u32(payload, static_cast<std::uint32_t>(entry.value.size()));
            append_string(payload, key);
            payload.insert(payload.end(), entry.value.begin(), entry.value.end());
        }
        if (payload.size() > kMaxTableBytes - kTableHeaderSize) {
            throw StoreError("compacted TABLE.sst exceeds the 256 MiB limit");
        }
        std::vector<std::uint8_t> table;
        table.reserve(kTableHeaderSize + payload.size());
        append_string(table, kTableMagic);
        append_u32(table, kFormatVersion);
        append_u32(table, 0);
        append_u64(table, live_key_count());
        append_u64(table, max_sequence_);
        append_u64(table, payload.size());
        append_u32(table, crc32(payload.data(), payload.size()));
        append_u32(table, 0);
        table.insert(table.end(), payload.begin(), payload.end());
        return table;
    }

    void remove_stale_temp(const char* name) {
        struct stat st {};
        if (::fstatat(dir_.get(), name, &st, AT_SYMLINK_NOFOLLOW) != 0) {
            if (errno == ENOENT) {
                return;
            }
            system_error(std::string("inspect temporary file ") + name);
        }
        if (!S_ISREG(st.st_mode) || st.st_uid != ::geteuid() || st.st_nlink != 1 ||
            (st.st_mode & 0077) != 0) {
            throw StoreError(std::string("refusing to remove unsafe temporary file ") + name);
        }
        if (::unlinkat(dir_.get(), name, 0) != 0) {
            system_error(std::string("remove stale temporary file ") + name);
        }
    }

    void create_temp_and_sync(const char* name, const std::vector<std::uint8_t>& bytes) {
        const int raw = ::openat(dir_.get(), name,
                                 O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
        if (raw < 0) {
            system_error(std::string("create temporary file ") + name);
        }
        Fd file(raw);
        require_private_regular(file.get(), name);
        if (!bytes.empty()) {
            write_all(file.get(), bytes.data(), bytes.size());
        }
        sync_data(file.get(), name);
    }

    Fd dir_;
    Fd lock_;
    Fd wal_;
    std::map<std::string, Entry, std::less<>> entries_;
    std::uint64_t max_sequence_ = 0;
    std::uint64_t live_bytes_ = 0;
    std::size_t wal_size_ = 0;
};

std::string hex_encode(const std::vector<std::uint8_t>& bytes) {
    std::ostringstream output;
    output << std::hex << std::setfill('0');
    for (const std::uint8_t byte : bytes) {
        output << std::setw(2) << static_cast<unsigned int>(byte);
    }
    return output.str();
}

std::string read_object_stdin() {
    std::string value;
    std::array<char, 8192> buffer{};
    while (std::cin) {
        std::cin.read(buffer.data(), static_cast<std::streamsize>(buffer.size()));
        const std::streamsize count = std::cin.gcount();
        if (count > 0) {
            const auto bytes = static_cast<std::size_t>(count);
            if (bytes > kMaxValueBytes - value.size()) {
                throw UsageError("object exceeds the 1 MiB limit");
            }
            value.append(buffer.data(), bytes);
        }
    }
    if (std::cin.bad() || !std::cin.eof()) {
        throw StoreError("failed while reading object bytes from stdin");
    }
    return value;
}

[[noreturn]] void usage() {
    throw UsageError(
        "usage:\n"
        "  ministore init --dir PATH\n"
        "  ministore put --dir PATH KEY VALUE\n"
        "  ministore get --dir PATH KEY\n"
        "  ministore delete --dir PATH KEY\n"
        "  ministore list --dir PATH\n"
        "  ministore object-put --dir PATH BUCKET KEY < OBJECT\n"
        "  ministore object-get --dir PATH BUCKET KEY > OBJECT\n"
        "  ministore object-delete --dir PATH BUCKET KEY\n"
        "  ministore object-list --dir PATH BUCKET [PREFIX]\n"
        "  ministore compact --dir PATH");
}

int run(int argc, char** argv) {
    if (argc < 4 || std::string_view(argv[2]) != "--dir") {
        usage();
    }
    const std::string_view command(argv[1]);
    const std::filesystem::path path(argv[3]);
    if (command == "init") {
        if (argc != 4) {
            usage();
        }
        initialize_store(path);
        std::cout << "initialized " << path.string() << '\n';
        return 0;
    }

    Store store = Store::open(path);
    if (command == "put") {
        if (argc != 6) {
            usage();
        }
        const auto sequence = store.put(argv[4], argv[5]);
        std::cout << "OK seq=" << sequence << '\n';
    } else if (command == "get") {
        if (argc != 5) {
            usage();
        }
        const Entry& entry = store.get(argv[4]);
        std::cout << "key\t" << argv[4] << "\tvalue_hex\t" << hex_encode(entry.value) << '\n';
    } else if (command == "delete") {
        if (argc != 5) {
            usage();
        }
        const auto sequence = store.erase(argv[4]);
        std::cout << "OK seq=" << sequence << '\n';
    } else if (command == "list") {
        if (argc != 4) {
            usage();
        }
        store.list();
    } else if (command == "object-put") {
        if (argc != 6) {
            usage();
        }
        const std::string value = read_object_stdin();
        const auto sequence = store.put_object(argv[4], argv[5], value);
        std::cout << "OK seq=" << sequence << " size=" << value.size() << '\n';
    } else if (command == "object-get") {
        if (argc != 6) {
            usage();
        }
        const Entry& entry = store.get_object(argv[4], argv[5]);
        write_all(STDOUT_FILENO, entry.value.data(), entry.value.size());
    } else if (command == "object-delete") {
        if (argc != 6) {
            usage();
        }
        const auto sequence = store.erase_object(argv[4], argv[5]);
        std::cout << "OK seq=" << sequence << '\n';
    } else if (command == "object-list") {
        if (argc != 5 && argc != 6) {
            usage();
        }
        store.list_objects(argv[4], argc == 6 ? argv[5] : "");
    } else if (command == "compact") {
        if (argc != 4) {
            usage();
        }
        const auto keys = store.compact();
        std::cout << "compacted seq=" << store.max_sequence() << " live_keys=" << keys << '\n';
    } else {
        usage();
    }
    return 0;
}

} // namespace

int main(int argc, char** argv) {
    try {
        return run(argc, argv);
    } catch (const NotFound& error) {
        std::cerr << "not found: " << error.what() << '\n';
        return 3;
    } catch (const UsageError& error) {
        std::cerr << error.what() << '\n';
        return 2;
    } catch (const StoreError& error) {
        std::cerr << "ministore: " << error.what() << '\n';
        return 1;
    } catch (const std::exception& error) {
        std::cerr << "ministore: unexpected error: " << error.what() << '\n';
        return 1;
    }
}
