#ifndef M03_MEMFS_CORE_H
#define M03_MEMFS_CORE_H

#include <stdbool.h>
#include <stddef.h>
#include <sys/stat.h>
#include <sys/types.h>

#define MEMFS_CAPACITY 8192U
#define MEMFS_FILE_PATH "/hello.txt"

extern const char MEMFS_READONLY_CONTENT[];

enum memfs_mode {
    MEMFS_READONLY = 0,
    MEMFS_MEMORY = 1
};

/* One fixed file, kept entirely in this process; callers serialize access. */
struct memfs_state {
    enum memfs_mode mode;
    unsigned char data[MEMFS_CAPACITY];
    size_t size;
};

void memfs_init(struct memfs_state *state, enum memfs_mode mode);
int memfs_lookup(const struct memfs_state *state, const char *path,
                 struct stat *st);
int memfs_open(struct memfs_state *state, const char *path, int flags);
int memfs_read(const struct memfs_state *state, const char *path, char *buffer,
               size_t size, off_t offset);
int memfs_write(struct memfs_state *state, const char *path, const char *buffer,
                size_t size, off_t offset, bool append);

#endif
