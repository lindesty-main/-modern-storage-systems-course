#define FUSE_USE_VERSION 31
#include <fuse.h>

#include "memfs_core.h"

#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static struct memfs_state filesystem;
static pthread_mutex_t filesystem_lock = PTHREAD_MUTEX_INITIALIZER;

static int fs_getattr(const char *path, struct stat *st,
                      struct fuse_file_info *fi)
{
    int result;

    (void)fi;
    pthread_mutex_lock(&filesystem_lock);
    result = memfs_lookup(&filesystem, path, st);
    pthread_mutex_unlock(&filesystem_lock);
    return result;
}

static int fs_readdir(const char *path, void *buffer, fuse_fill_dir_t filler,
                      off_t offset, struct fuse_file_info *fi,
                      enum fuse_readdir_flags flags)
{
    static const char *const names[] = { ".", "..", "hello.txt" };
    struct stat dir_stat;
    struct stat entry_stat;
    int result;
    off_t index;

    (void)fi;
    (void)flags;
    if (offset < 0)
        return -EINVAL;

    pthread_mutex_lock(&filesystem_lock);
    result = memfs_lookup(&filesystem, path, &dir_stat);
    if (result == 0 && S_ISDIR(dir_stat.st_mode))
        result = memfs_lookup(&filesystem, MEMFS_FILE_PATH, &entry_stat);
    pthread_mutex_unlock(&filesystem_lock);
    if (result != 0)
        return result;
    if (!S_ISDIR(dir_stat.st_mode))
        return -ENOTDIR;

    for (index = offset; index < (off_t)(sizeof(names) / sizeof(names[0]));
         ++index) {
        if (index != 2)
            entry_stat = dir_stat;
        if (filler(buffer, names[index], &entry_stat, index + 1, 0) != 0)
            break;
    }
    return 0;
}

static int fs_open(const char *path, struct fuse_file_info *fi)
{
    int result;

    pthread_mutex_lock(&filesystem_lock);
    result = memfs_open(&filesystem, path, fi->flags);
    pthread_mutex_unlock(&filesystem_lock);
    return result;
}

static int fs_read(const char *path, char *buffer, size_t size, off_t offset,
                   struct fuse_file_info *fi)
{
    int result;

    (void)fi;
    pthread_mutex_lock(&filesystem_lock);
    result = memfs_read(&filesystem, path, buffer, size, offset);
    pthread_mutex_unlock(&filesystem_lock);
    return result;
}

static int fs_write(const char *path, const char *buffer, size_t size,
                    off_t offset, struct fuse_file_info *fi)
{
    struct stat st;
    bool append = (fi->flags & O_APPEND) != 0;
    size_t start;
    int result;

    pthread_mutex_lock(&filesystem_lock);
    result = memfs_lookup(&filesystem, path, &st);
    if (result == 0 && S_ISDIR(st.st_mode))
        result = -EISDIR;
    if (result == 0 && filesystem.mode == MEMFS_MEMORY && offset < 0)
        result = -EINVAL;
    if (result == 0 && filesystem.mode == MEMFS_MEMORY && size > 0U) {
        start = append ? filesystem.size :
                ((uintmax_t)offset > (uintmax_t)MEMFS_CAPACITY ?
                 MEMFS_CAPACITY : (size_t)offset);
        if (start >= MEMFS_CAPACITY || size > MEMFS_CAPACITY - start)
            result = -ENOSPC;
    }
    if (result == 0)
        result = memfs_write(&filesystem, path, buffer, size, offset, append);
    pthread_mutex_unlock(&filesystem_lock);
    return result;
}

static const struct fuse_operations operations = {
    .getattr = fs_getattr,
    .readdir = fs_readdir,
    .open = fs_open,
    .read = fs_read,
    .write = fs_write,
};

static void usage(const char *program)
{
    fprintf(stderr,
            "Usage: %s --readonly|--memory [FUSE options] MOUNTPOINT\n"
            "Example: %s --memory -f -s /tmp/m03-mount\n",
            program, program);
}

int main(int argc, char **argv)
{
    enum memfs_mode mode;
    char **fuse_argv;
    int fuse_argc;
    int result;
    int index;

    if (argc < 2) {
        usage(argv[0]);
        return EXIT_FAILURE;
    }
    if (strcmp(argv[1], "--readonly") == 0) {
        mode = MEMFS_READONLY;
    } else if (strcmp(argv[1], "--memory") == 0) {
        mode = MEMFS_MEMORY;
    } else {
        usage(argv[0]);
        return EXIT_FAILURE;
    }

    fuse_argc = argc - 1;
    fuse_argv = calloc((size_t)fuse_argc + 1U, sizeof(*fuse_argv));
    if (fuse_argv == NULL) {
        perror("calloc");
        return EXIT_FAILURE;
    }
    fuse_argv[0] = argv[0];
    for (index = 2; index < argc; ++index)
        fuse_argv[index - 1] = argv[index];

    memfs_init(&filesystem, mode);
    result = fuse_main(fuse_argc, fuse_argv, &operations, NULL);
    free(fuse_argv);
    return result;
}
