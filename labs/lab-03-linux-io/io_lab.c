#define _GNU_SOURCE
#define _FILE_OFFSET_BITS 64

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/uio.h>
#include <unistd.h>

#ifndef SSIZE_MAX
#define SSIZE_MAX ((ssize_t)(SIZE_MAX >> 1))
#endif

#define MAX_IOV 16
#define DIRECT_ALIGNMENT 4096U
#define DIRECT_LENGTH 4096U

typedef ssize_t (*vector_call)(int, const struct iovec *, int);

static int report_error(const char *operation, int error_number)
{
    fprintf(stderr, "%s: %s\n", operation, strerror(error_number));
    return -1;
}

static int validate_vectors(const struct iovec *iov, int iovcnt,
                            size_t *total_out)
{
    size_t total = 0;

    if (iov == NULL || iovcnt <= 0 || iovcnt > MAX_IOV) {
        errno = EINVAL;
        return -1;
    }

    for (int i = 0; i < iovcnt; ++i) {
        if (iov[i].iov_len != 0 && iov[i].iov_base == NULL) {
            errno = EINVAL;
            return -1;
        }
        if (iov[i].iov_len > (size_t)SSIZE_MAX - total) {
            errno = EOVERFLOW;
            return -1;
        }
        total += iov[i].iov_len;
    }

    *total_out = total;
    return 0;
}

static ssize_t vector_full(int fd, const struct iovec *iov, int iovcnt,
                           vector_call call)
{
    struct iovec pending[MAX_IOV];
    size_t total;
    size_t completed = 0;

    if (validate_vectors(iov, iovcnt, &total) < 0)
        return -1;
    if (total == 0)
        return 0;

    memcpy(pending, iov, (size_t)iovcnt * sizeof(*pending));

    while (completed < total) {
        int first = 0;
        while (first < iovcnt && pending[first].iov_len == 0)
            ++first;
        if (first == iovcnt) {
            errno = EIO;
            return -1;
        }

        ssize_t amount = call(fd, &pending[first], iovcnt - first);
        if (amount < 0) {
            if (errno == EINTR)
                continue;
            return -1;
        }
        if (amount == 0 || (size_t)amount > total - completed) {
            errno = EIO;
            return -1;
        }

        completed += (size_t)amount;
        size_t consumed = (size_t)amount;
        for (int i = first; i < iovcnt && consumed != 0; ++i) {
            if (pending[i].iov_len == 0)
                continue;
            if (consumed >= pending[i].iov_len) {
                consumed -= pending[i].iov_len;
                pending[i].iov_base = (char *)pending[i].iov_base +
                                      pending[i].iov_len;
                pending[i].iov_len = 0;
            } else {
                pending[i].iov_base = (char *)pending[i].iov_base + consumed;
                pending[i].iov_len -= consumed;
                consumed = 0;
            }
        }
    }

    return (ssize_t)completed;
}

static ssize_t writev_full(int fd, const struct iovec *iov, int iovcnt)
{
    return vector_full(fd, iov, iovcnt, writev);
}

static ssize_t readv_full(int fd, const struct iovec *iov, int iovcnt)
{
    return vector_full(fd, iov, iovcnt, readv);
}

static int validate_positional_io(const void *buffer, size_t length,
                                  off_t offset)
{
    if ((length != 0 && buffer == NULL) || length > (size_t)SSIZE_MAX ||
        offset < 0 || (uintmax_t)offset > (uintmax_t)INT64_MAX - length) {
        errno = EINVAL;
        return -1;
    }
    return 0;
}

static ssize_t positional_full(int fd, void *buffer, size_t length,
                               off_t offset, int writing)
{
    size_t completed = 0;

    if (validate_positional_io(buffer, length, offset) < 0)
        return -1;

    while (completed < length) {
        ssize_t amount;
        off_t current = offset + (off_t)completed;
        size_t remaining = length - completed;

        if (writing) {
            amount = pwrite(fd, (const char *)buffer + completed, remaining,
                            current);
        } else {
            amount = pread(fd, (char *)buffer + completed, remaining, current);
        }

        if (amount < 0) {
            if (errno == EINTR)
                continue;
            return -1;
        }
        if (amount == 0 || (size_t)amount > remaining) {
            errno = EIO;
            return -1;
        }
        completed += (size_t)amount;
    }

    return (ssize_t)completed;
}

static ssize_t pread_full(int fd, void *buffer, size_t length, off_t offset)
{
    return positional_full(fd, buffer, length, offset, 0);
}

static ssize_t pwrite_full(int fd, const void *buffer, size_t length,
                           off_t offset)
{
    return positional_full(fd, (void *)buffer, length, offset, 1);
}

static int make_temp_file(const char *directory, const char *prefix,
                          char *path, size_t path_capacity)
{
    int written = snprintf(path, path_capacity, "%s/%s-XXXXXX", directory,
                           prefix);
    if (written < 0 || (size_t)written >= path_capacity) {
        errno = ENAMETOOLONG;
        return -1;
    }

    int fd = mkstemp(path);
    if (fd < 0)
        return -1;

    struct stat status;
    if (fstat(fd, &status) < 0) {
        int saved = errno;
        (void)close(fd);
        (void)unlink(path);
        errno = saved;
        return -1;
    }
    if (!S_ISREG(status.st_mode)) {
        (void)close(fd);
        (void)unlink(path);
        errno = EINVAL;
        return -1;
    }
    if (fcntl(fd, F_SETFD, FD_CLOEXEC) < 0) {
        int saved = errno;
        (void)close(fd);
        (void)unlink(path);
        errno = saved;
        return -1;
    }
    return fd;
}

static int test_positional_and_vector_file_io(const char *directory)
{
    char path[256] = {0};
    int fd = make_temp_file(directory, "file", path, sizeof(path));
    int result = -1;
    const char first[] = "header:";
    const char second[] = "payload";
    const char third[] = "\n";
    const char expected[] = "header:payload\n";
    const size_t expected_length = sizeof(expected) - 1;
    char part1[6];
    char part2[5];
    char part3[4];
    char complete[sizeof(expected) - 1];
    char suffix = '!';
    char suffix_read = '\0';

    if (fd < 0)
        return report_error("mkstemp file", errno);

    struct iovec write_parts[] = {
        {.iov_base = (void *)first, .iov_len = sizeof(first) - 1},
        {.iov_base = (void *)second, .iov_len = sizeof(second) - 1},
        {.iov_base = (void *)third, .iov_len = sizeof(third) - 1},
    };
    if (writev_full(fd, write_parts, 3) != (ssize_t)expected_length) {
        report_error("writev_full", errno);
        goto done;
    }

    off_t current = lseek(fd, 0, SEEK_CUR);
    if (current != (off_t)expected_length) {
        report_error("verify writev file position", current < 0 ? errno : EIO);
        goto done;
    }

    struct iovec read_parts[] = {
        {.iov_base = part1, .iov_len = sizeof(part1)},
        {.iov_base = part2, .iov_len = sizeof(part2)},
        {.iov_base = part3, .iov_len = sizeof(part3)},
    };
    if (lseek(fd, 0, SEEK_SET) != 0) {
        report_error("lseek before readv", errno);
        goto done;
    }
    if (readv_full(fd, read_parts, 3) != (ssize_t)expected_length) {
        report_error("readv_full", errno);
        goto done;
    }
    memcpy(complete, part1, sizeof(part1));
    memcpy(complete + sizeof(part1), part2, sizeof(part2));
    memcpy(complete + sizeof(part1) + sizeof(part2), part3, sizeof(part3));
    if (memcmp(complete, expected, expected_length) != 0) {
        report_error("verify readv segment order", EILSEQ);
        goto done;
    }

    current = lseek(fd, 0, SEEK_CUR);
    if (current != (off_t)expected_length) {
        report_error("verify readv file position", current < 0 ? errno : EIO);
        goto done;
    }
    if (pread_full(fd, complete, expected_length, 0) !=
        (ssize_t)expected_length) {
        report_error("pread_full", errno);
        goto done;
    }
    if (memcmp(complete, expected, expected_length) != 0) {
        report_error("verify pread content", EILSEQ);
        goto done;
    }
    if (lseek(fd, 0, SEEK_CUR) != current) {
        report_error("verify pread does not change file position",
                     errno != 0 ? errno : EIO);
        goto done;
    }

    if (pwrite_full(fd, &suffix, 1, (off_t)expected_length) != 1) {
        report_error("pwrite_full", errno);
        goto done;
    }
    if (lseek(fd, 0, SEEK_CUR) != current) {
        report_error("verify pwrite does not change file position",
                     errno != 0 ? errno : EIO);
        goto done;
    }
    if (pread_full(fd, &suffix_read, 1, (off_t)expected_length) != 1) {
        report_error("read back pwrite result", errno);
        goto done;
    }
    if (suffix_read != suffix) {
        report_error("verify pwrite contents", EILSEQ);
        goto done;
    }
    if (fdatasync(fd) < 0) {
        report_error("fdatasync temporary file", errno);
        goto done;
    }

    puts("PASS: pread/pwrite offsets and full-transfer handling");
    puts("PASS: writev/readv segment ordering and partial-vector handling");
    result = 0;

done:
    {
        int saved = errno;
        if (close(fd) < 0 && result == 0)
            result = report_error("close temporary file", errno);
        if (unlink(path) < 0 && result == 0)
            result = report_error("unlink temporary file", errno);
        errno = saved;
    }
    return result;
}

static int test_local_socket_vector_io(void)
{
    int sockets[2] = {-1, -1};
    const char one[] = "local-";
    const char two[] = "socket-";
    const char three[] = "round-trip";
    const char expected[] = "local-socket-round-trip";
    char received1[5];
    char received2[8];
    char received3[sizeof(expected) - 1 - sizeof(received1) -
                   sizeof(received2)];
    int result = -1;

    if (socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) < 0)
        return report_error("socketpair(AF_UNIX)", errno);

    struct iovec outgoing[] = {
        {.iov_base = (void *)one, .iov_len = sizeof(one) - 1},
        {.iov_base = (void *)two, .iov_len = sizeof(two) - 1},
        {.iov_base = (void *)three, .iov_len = sizeof(three) - 1},
    };
    struct iovec incoming[] = {
        {.iov_base = received1, .iov_len = sizeof(received1)},
        {.iov_base = received2, .iov_len = sizeof(received2)},
        {.iov_base = received3, .iov_len = sizeof(received3)},
    };

    size_t total = sizeof(expected) - 1;
    if (writev_full(sockets[0], outgoing, 3) != (ssize_t)total) {
        report_error("writev_full local socket", errno);
        goto done;
    }
    if (readv_full(sockets[1], incoming, 3) != (ssize_t)total) {
        report_error("readv_full local socket", errno);
        goto done;
    }
    char combined[sizeof(expected) - 1];
    memcpy(combined, received1, sizeof(received1));
    memcpy(combined + sizeof(received1), received2, sizeof(received2));
    memcpy(combined + sizeof(received1) + sizeof(received2), received3,
           sizeof(received3));
    if (memcmp(combined, expected, total) != 0) {
        report_error("verify local socket payload", EILSEQ);
        goto done;
    }

    puts("PASS: local socketpair vectored round-trip");
    result = 0;

done:
    for (size_t i = 0; i < 2; ++i) {
        if (sockets[i] >= 0 && close(sockets[i]) < 0 && result == 0)
            result = report_error("close local socket", errno);
    }
    return result;
}

static int test_invalid_boundaries(void)
{
    char byte = 'x';
    struct iovec too_many[MAX_IOV + 1];
    for (size_t i = 0; i < MAX_IOV + 1; ++i) {
        too_many[i].iov_base = &byte;
        too_many[i].iov_len = 0;
    }

    errno = 0;
    if (writev_full(-1, NULL, 0) != -1 || errno != EINVAL)
        return report_error("reject empty vector", errno != 0 ? errno : EIO);
    errno = 0;
    if (readv_full(-1, too_many, MAX_IOV + 1) != -1 || errno != EINVAL)
        return report_error("reject excessive vector count",
                            errno != 0 ? errno : EIO);

    struct iovec null_buffer = {.iov_base = NULL, .iov_len = 1};
    errno = 0;
    if (readv_full(-1, &null_buffer, 1) != -1 || errno != EINVAL)
        return report_error("reject nonempty null vector",
                            errno != 0 ? errno : EIO);

    struct iovec overflow_vectors[] = {
        {.iov_base = &byte, .iov_len = (size_t)SSIZE_MAX},
        {.iov_base = &byte, .iov_len = 1},
    };
    errno = 0;
    if (writev_full(-1, overflow_vectors, 2) != -1 || errno != EOVERFLOW)
        return report_error("reject vector length overflow",
                            errno != 0 ? errno : EIO);

    errno = 0;
    if (pread_full(-1, &byte, 1, (off_t)-1) != -1 || errno != EINVAL)
        return report_error("reject negative file offset",
                            errno != 0 ? errno : EIO);
    errno = 0;
    if (pread_full(-1, &byte, 1, (off_t)INT64_MAX) != -1 || errno != EINVAL)
        return report_error("reject overflowing file range",
                            errno != 0 ? errno : EIO);

    puts("PASS: invalid vector and offset bounds rejected");
    return 0;
}

static int direct_io_is_unsupported(int error_number)
{
    if (error_number == EINVAL || error_number == EOPNOTSUPP)
        return 1;
#if defined(ENOTSUP) && ENOTSUP != EOPNOTSUPP
    if (error_number == ENOTSUP)
        return 1;
#endif
    return 0;
}

static int test_optional_direct_io(const char *directory)
{
#if defined(O_DIRECT)
    char path[256] = {0};
    void *buffer = NULL;
    int fd = make_temp_file(directory, "direct", path, sizeof(path));
    int result = -1;

    if (fd < 0)
        return report_error("mkstemp O_DIRECT file", errno);

    int allocation_error = posix_memalign(&buffer, DIRECT_ALIGNMENT,
                                          DIRECT_LENGTH);
    if (allocation_error != 0) {
        report_error("posix_memalign O_DIRECT buffer", allocation_error);
        goto done;
    }
    memset(buffer, 0x5a, DIRECT_LENGTH);
    if (pwrite_full(fd, buffer, DIRECT_LENGTH, 0) != DIRECT_LENGTH) {
        report_error("prepare O_DIRECT temporary file", errno);
        goto done;
    }
    if (fdatasync(fd) < 0) {
        report_error("fdatasync before O_DIRECT", errno);
        goto done;
    }
    if (close(fd) < 0) {
        fd = -1;
        report_error("close buffered temporary file", errno);
        goto done;
    }
    fd = -1;

    fd = open(path, O_RDONLY | O_DIRECT | O_CLOEXEC);
    if (fd < 0) {
        if (direct_io_is_unsupported(errno)) {
            printf("SKIP: O_DIRECT unavailable for temporary file (%s)\n",
                   strerror(errno));
            result = 0;
            goto done;
        }
        report_error("open temporary file with O_DIRECT", errno);
        goto done;
    }

    struct stat status;
    if (fstat(fd, &status) < 0) {
        report_error("fstat O_DIRECT temporary file", errno);
        goto done;
    }
    if (!S_ISREG(status.st_mode) || status.st_size != (off_t)DIRECT_LENGTH) {
        report_error("verify O_DIRECT ordinary-file bounds", EIO);
        goto done;
    }

    ssize_t amount;
    do {
        amount = pread(fd, buffer, DIRECT_LENGTH, 0);
    } while (amount < 0 && errno == EINTR);
    if (amount < 0 && direct_io_is_unsupported(errno)) {
        printf("SKIP: O_DIRECT alignment or operation unsupported (%s)\n",
               strerror(errno));
        result = 0;
        goto done;
    }
    if (amount != DIRECT_LENGTH) {
        report_error("O_DIRECT aligned read",
                     amount < 0 ? errno : EIO);
        goto done;
    }
    for (size_t i = 0; i < DIRECT_LENGTH; ++i) {
        if (((unsigned char *)buffer)[i] != 0x5a) {
            report_error("verify O_DIRECT data", EILSEQ);
            goto done;
        }
    }

    puts("PASS: O_DIRECT aligned temporary-file read");
    result = 0;

done:
    if (fd >= 0 && close(fd) < 0 && result == 0)
        result = report_error("close O_DIRECT file", errno);
    if (unlink(path) < 0 && result == 0)
        result = report_error("unlink O_DIRECT file", errno);
    free(buffer);
    return result;
#else
    (void)directory;
    puts("SKIP: O_DIRECT is not available in this build");
    return 0;
#endif
}

int main(void)
{
    char directory_template[] = "/tmp/m02-io.XXXXXX";
    char *directory = mkdtemp(directory_template);
    int result = 0;

    if (directory == NULL) {
        report_error("mkdtemp", errno);
        return 1;
    }

    if (test_positional_and_vector_file_io(directory) < 0)
        result = 1;
    if (test_local_socket_vector_io() < 0)
        result = 1;
    if (test_invalid_boundaries() < 0)
        result = 1;
    if (test_optional_direct_io(directory) < 0)
        result = 1;

    if (rmdir(directory) < 0) {
        report_error("remove private temporary directory", errno);
        result = 1;
    } else if (result == 0) {
        puts("PASS: private temporary files removed");
    }

    return result;
}
