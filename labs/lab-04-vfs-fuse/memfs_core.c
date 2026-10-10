#define _DEFAULT_SOURCE
#include "memfs_core.h"

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>

const char MEMFS_READONLY_CONTENT[] = "Hello from read-only memfs!\n";

_Static_assert(sizeof(MEMFS_READONLY_CONTENT) - 1U <= MEMFS_CAPACITY,
               "read-only fixture must fit in the in-memory file");

static int path_kind(const char *path)
{
    if (path == NULL)
        return -EINVAL;
    if (strcmp(path, "/") == 0)
        return 1;
    if (strcmp(path, MEMFS_FILE_PATH) == 0)
        return 2;
    return 0;
}

void memfs_init(struct memfs_state *state, enum memfs_mode mode)
{
    if (state == NULL)
        return;

    memset(state, 0, sizeof(*state));
    state->mode = mode == MEMFS_MEMORY ? MEMFS_MEMORY : MEMFS_READONLY;
    if (state->mode == MEMFS_READONLY) {
        state->size = sizeof(MEMFS_READONLY_CONTENT) - 1U;
        memcpy(state->data, MEMFS_READONLY_CONTENT, state->size);
    }
}

int memfs_lookup(const struct memfs_state *state, const char *path,
                 struct stat *st)
{
    int kind;

    if (state == NULL || st == NULL)
        return -EINVAL;
    kind = path_kind(path);
    if (kind < 0)
        return kind;
    if (kind == 0)
        return -ENOENT;

    memset(st, 0, sizeof(*st));
    st->st_uid = getuid();
    st->st_gid = getgid();
    st->st_blksize = 4096;
    if (kind == 1) {
        st->st_ino = 1;
        st->st_mode = S_IFDIR | 0755;
        st->st_nlink = 2;
        return 0;
    }

    st->st_ino = 2;
    st->st_mode = S_IFREG | (state->mode == MEMFS_READONLY ? 0444 : 0644);
    st->st_nlink = 1;
    st->st_size = (off_t)state->size;
    st->st_blocks = (blkcnt_t)((state->size + 511U) / 512U);
    return 0;
}

int memfs_open(struct memfs_state *state, const char *path, int flags)
{
    struct stat st;
    int result;
    int access_mode;

    result = memfs_lookup(state, path, &st);
    if (result != 0)
        return result;
    if (S_ISDIR(st.st_mode))
        return -EISDIR;

    access_mode = flags & O_ACCMODE;
    if (access_mode != O_RDONLY && access_mode != O_WRONLY &&
        access_mode != O_RDWR)
        return -EINVAL;
    if ((flags & (O_CREAT | O_EXCL)) == (O_CREAT | O_EXCL))
        return -EEXIST;

    if (state->mode == MEMFS_READONLY) {
        if (access_mode != O_RDONLY || (flags & O_TRUNC) != 0)
            return -EROFS;
        return 0;
    }

    if ((flags & O_TRUNC) != 0 && access_mode == O_RDONLY)
        return -EINVAL;
    if ((flags & O_TRUNC) != 0) {
        memset(state->data, 0, sizeof(state->data));
        state->size = 0;
    }
    return 0;
}

int memfs_read(const struct memfs_state *state, const char *path, char *buffer,
               size_t size, off_t offset)
{
    struct stat st;
    size_t start;
    size_t available;
    size_t amount;
    int result;

    if (state == NULL || offset < 0 || (size > 0U && buffer == NULL))
        return -EINVAL;
    result = memfs_lookup(state, path, &st);
    if (result != 0)
        return result;
    if (S_ISDIR(st.st_mode))
        return -EISDIR;
    if (size == 0U || (uintmax_t)offset >= (uintmax_t)state->size)
        return 0;

    start = (size_t)offset;
    available = state->size - start;
    amount = size < available ? size : available;
    memcpy(buffer, state->data + start, amount);
    return (int)amount;
}

int memfs_write(struct memfs_state *state, const char *path,
                const char *buffer, size_t size, off_t offset, bool append)
{
    struct stat st;
    size_t start;
    size_t available;
    size_t amount;
    size_t end;
    int result;

    if (state == NULL || offset < 0 || (size > 0U && buffer == NULL))
        return -EINVAL;
    result = memfs_lookup(state, path, &st);
    if (result != 0)
        return result;
    if (S_ISDIR(st.st_mode))
        return -EISDIR;
    if (state->mode == MEMFS_READONLY)
        return -EROFS;
    if (size == 0U)
        return 0;

    if (append) {
        start = state->size;
    } else {
        if ((uintmax_t)offset > (uintmax_t)MEMFS_CAPACITY)
            return -ENOSPC;
        start = (size_t)offset;
    }
    if (start >= MEMFS_CAPACITY)
        return -ENOSPC;

    available = MEMFS_CAPACITY - start;
    amount = size < available ? size : available;
    if (start > state->size)
        memset(state->data + state->size, 0, start - state->size);
    memmove(state->data + start, buffer, amount);
    end = start + amount;
    if (end > state->size)
        state->size = end;
    return (int)amount;
}
