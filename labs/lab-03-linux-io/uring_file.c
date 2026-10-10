#define _GNU_SOURCE
#define _FILE_OFFSET_BITS 64

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <liburing.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

struct io_request {
    uint64_t tag;
    void *buffer;
    size_t length;
    off_t offset;
    int seen;
};

static int report_error(const char *operation, int error_number)
{
    fprintf(stderr, "%s: %s\n", operation, strerror(error_number));
    return -1;
}

static int ring_unavailable(int error_number)
{
    return error_number == EPERM || error_number == EACCES ||
           error_number == ENOSYS || error_number == EOPNOTSUPP ||
           error_number == EINVAL;
}

static int submit_all(struct io_uring *ring, size_t count, size_t *submitted)
{
    *submitted = 0;
    while (*submitted < count) {
        int amount = io_uring_submit(ring);
        if (amount < 0) {
            errno = -amount;
            return -1;
        }
        if (amount == 0) {
            errno = EIO;
            return -1;
        }
        if ((size_t)amount > count - *submitted) {
            errno = EIO;
            return -1;
        }
        *submitted += (size_t)amount;
    }
    return 0;
}

static int collect_completions(struct io_uring *ring,
                               struct io_request *requests, size_t count)
{
    size_t completed = 0;
    int failed = 0;

    while (completed < count) {
        struct io_uring_cqe *cqe = NULL;
        int wait_result = io_uring_wait_cqe(ring, &cqe);
        if (wait_result == -EINTR)
            continue;
        if (wait_result < 0) {
            report_error("io_uring_wait_cqe", -wait_result);
            return -1;
        }

        struct io_request *matched = NULL;
        for (size_t i = 0; i < count; ++i) {
            if (requests[i].tag == cqe->user_data) {
                matched = &requests[i];
                break;
            }
        }
        if (matched == NULL || matched->seen) {
            report_error("unexpected or duplicate CQE user_data", EPROTO);
            failed = 1;
        } else {
            matched->seen = 1;
            if (cqe->res < 0) {
                report_error("io_uring operation", -cqe->res);
                failed = 1;
            } else if ((size_t)cqe->res != matched->length) {
                fprintf(stderr,
                        "short io_uring completion for tag=%" PRIu64
                        ": got %d bytes, expected %zu\n",
                        matched->tag, cqe->res, matched->length);
                failed = 1;
            }
        }
        io_uring_cqe_seen(ring, cqe);
        ++completed;
    }

    return failed ? -1 : 0;
}

static int run_batch(struct io_uring *ring, int fd,
                     struct io_request *requests, size_t count, int is_read)
{
    if (count == 0 || count > 8) {
        errno = EINVAL;
        return report_error("validate batch size", errno);
    }

    for (size_t i = 0; i < count; ++i)
        requests[i].seen = 0;

    for (size_t i = 0; i < count; ++i) {
        struct io_uring_sqe *sqe = io_uring_get_sqe(ring);
        if (sqe == NULL)
            return report_error("io_uring_get_sqe", ENOMEM);
        if (requests[i].length > UINT32_MAX)
            return report_error("request length exceeds SQE limit", EOVERFLOW);
        if (is_read) {
            io_uring_prep_read(sqe, fd, requests[i].buffer,
                               (unsigned)requests[i].length,
                               requests[i].offset);
        } else {
            io_uring_prep_write(sqe, fd, requests[i].buffer,
                                (unsigned)requests[i].length,
                                requests[i].offset);
        }
        sqe->user_data = requests[i].tag;
    }

    size_t submitted;
    if (submit_all(ring, count, &submitted) < 0) {
        int saved = errno;
        if (submitted != 0)
            (void)collect_completions(ring, requests, submitted);
        errno = saved;
        return report_error("io_uring_submit", saved);
    }
    return collect_completions(ring, requests, count);
}

int main(void)
{
    struct io_uring ring;
    int ring_initialized = 0;
    int fd = -1;
    int result = 1;
    char directory_template[] = "/tmp/m02-uring.XXXXXX";
    char file_path[256] = {0};
    char *directory = NULL;
    char write_a[] = "alpha: completion queue";
    char write_b[] = "beta: submission queue";
    char read_a[sizeof(write_a) - 1];
    char read_b[sizeof(write_b) - 1];
    const off_t offset_a = 0;
    const off_t offset_b = 4096;

    memset(&ring, 0, sizeof(ring));
    int ring_result = io_uring_queue_init(8, &ring, 0);
    if (ring_result < 0) {
        int error_number = -ring_result;
        if (ring_unavailable(error_number)) {
            printf("SKIP: io_uring unavailable or restricted (%s)\n",
                   strerror(error_number));
            return 77;
        }
        return report_error("io_uring_queue_init", error_number) != 0;
    }
    ring_initialized = 1;

    directory = mkdtemp(directory_template);
    if (directory == NULL) {
        report_error("mkdtemp", errno);
        goto done;
    }
    int path_length = snprintf(file_path, sizeof(file_path), "%s/data-XXXXXX",
                               directory);
    if (path_length < 0 || (size_t)path_length >= sizeof(file_path)) {
        report_error("format temporary path", ENAMETOOLONG);
        goto done;
    }
    fd = mkstemp(file_path);
    if (fd < 0) {
        report_error("mkstemp", errno);
        goto done;
    }

    struct stat status;
    if (fstat(fd, &status) < 0) {
        report_error("fstat temporary file", errno);
        goto done;
    }
    if (!S_ISREG(status.st_mode)) {
        report_error("verify temporary ordinary file", EINVAL);
        goto done;
    }

    struct io_request writes[] = {
        {.tag = 1, .buffer = write_a, .length = sizeof(write_a) - 1,
         .offset = offset_a, .seen = 0},
        {.tag = 2, .buffer = write_b, .length = sizeof(write_b) - 1,
         .offset = offset_b, .seen = 0},
    };
    if (run_batch(&ring, fd, writes, 2, 0) < 0)
        goto done;

    struct io_request reads[] = {
        {.tag = 3, .buffer = read_a, .length = sizeof(read_a),
         .offset = offset_a, .seen = 0},
        {.tag = 4, .buffer = read_b, .length = sizeof(read_b),
         .offset = offset_b, .seen = 0},
    };
    if (run_batch(&ring, fd, reads, 2, 1) < 0)
        goto done;

    if (memcmp(read_a, write_a, sizeof(read_a)) != 0 ||
        memcmp(read_b, write_b, sizeof(read_b)) != 0) {
        report_error("verify io_uring temporary-file contents", EILSEQ);
        goto done;
    }

    puts("PASS: io_uring temporary-file write/read completions verified");
    result = 0;

done:
    if (ring_initialized)
        io_uring_queue_exit(&ring);
    if (fd >= 0 && close(fd) < 0) {
        report_error("close temporary file", errno);
        result = 1;
    }
    if (file_path[0] != '\0' && unlink(file_path) < 0) {
        report_error("unlink temporary file", errno);
        result = 1;
    }
    if (directory != NULL && rmdir(directory) < 0) {
        report_error("remove private temporary directory", errno);
        result = 1;
    }
    return result;
}
