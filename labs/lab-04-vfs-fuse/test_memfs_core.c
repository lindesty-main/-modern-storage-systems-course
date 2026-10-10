#include "memfs_core.h"

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define CHECK(condition) do { \
    if (!(condition)) { \
        fprintf(stderr, "%s:%d: check failed: %s\n", __FILE__, __LINE__, #condition); \
        exit(EXIT_FAILURE); \
    } \
} while (0)

static void test_lookup_and_readonly_mode(void)
{
    struct memfs_state state;
    struct stat st;
    char buffer[64] = {0};
    const size_t content_size = strlen(MEMFS_READONLY_CONTENT);

    memfs_init(&state, MEMFS_READONLY);
    CHECK(memfs_lookup(&state, "/", &st) == 0);
    CHECK(S_ISDIR(st.st_mode));
    CHECK(memfs_lookup(&state, MEMFS_FILE_PATH, &st) == 0);
    CHECK(S_ISREG(st.st_mode));
    CHECK((st.st_mode & 0777) == 0444);
    CHECK(st.st_size == (off_t)content_size);
    CHECK(memfs_lookup(&state, "/missing", &st) == -ENOENT);
    CHECK(memfs_lookup(&state, "/../hello.txt", &st) == -ENOENT);

    CHECK(memfs_open(&state, MEMFS_FILE_PATH, O_RDONLY) == 0);
    CHECK(memfs_open(&state, "/", O_RDONLY) == -EISDIR);
    CHECK(memfs_open(&state, "/missing", O_RDONLY) == -ENOENT);
    CHECK(memfs_open(&state, MEMFS_FILE_PATH, O_CREAT | O_EXCL | O_RDONLY) ==
          -EEXIST);
    CHECK(memfs_open(&state, MEMFS_FILE_PATH, O_WRONLY) == -EROFS);
    CHECK(memfs_open(&state, MEMFS_FILE_PATH, O_RDWR) == -EROFS);
    CHECK(memfs_open(&state, MEMFS_FILE_PATH, O_RDONLY | O_TRUNC) == -EROFS);
    CHECK(memfs_read(&state, MEMFS_FILE_PATH, buffer, sizeof(buffer), 0) ==
          (int)content_size);
    CHECK(memcmp(buffer, MEMFS_READONLY_CONTENT, content_size) == 0);
    CHECK(memfs_read(&state, MEMFS_FILE_PATH, buffer, sizeof(buffer),
                     (off_t)content_size) == 0);
    CHECK(memfs_write(&state, MEMFS_FILE_PATH, "x", 1, 0, false) == -EROFS);
    CHECK(memfs_read(&state, "/missing", buffer, sizeof(buffer), 0) == -ENOENT);
}

static void test_invalid_arguments(void)
{
    struct memfs_state state;
    struct stat st;

    CHECK(memfs_lookup(NULL, "/", &st) == -EINVAL);
    CHECK(memfs_lookup(&state, NULL, &st) == -EINVAL);
    CHECK(memfs_lookup(&state, "/", NULL) == -EINVAL);
    CHECK(memfs_read(NULL, MEMFS_FILE_PATH, NULL, 0, 0) == -EINVAL);
    CHECK(memfs_write(NULL, MEMFS_FILE_PATH, NULL, 0, 0, false) == -EINVAL);
    memfs_init(NULL, MEMFS_MEMORY);
}

static void test_memory_mode(void)
{
    struct memfs_state state;
    struct stat st;
    char buffer[16] = {0};
    char large[MEMFS_CAPACITY + 4U];

    memset(large, 'L', sizeof(large));
    memfs_init(&state, MEMFS_MEMORY);
    CHECK(memfs_lookup(&state, MEMFS_FILE_PATH, &st) == 0);
    CHECK((st.st_mode & 0777) == 0644);
    CHECK(st.st_size == 0);
    CHECK(memfs_open(&state, MEMFS_FILE_PATH, O_WRONLY | O_CREAT | O_TRUNC) == 0);
    CHECK(memfs_write(&state, MEMFS_FILE_PATH, "abc", 3, 0, false) == 3);
    CHECK(memfs_write(&state, MEMFS_FILE_PATH, "Z", 1, 1, false) == 1);
    CHECK(memfs_read(&state, MEMFS_FILE_PATH, buffer, sizeof(buffer), 0) == 3);
    CHECK(memcmp(buffer, "aZc", 3) == 0);

    CHECK(memfs_write(&state, MEMFS_FILE_PATH, "Q", 1, 5, false) == 1);
    memset(buffer, 0xff, sizeof(buffer));
    CHECK(memfs_read(&state, MEMFS_FILE_PATH, buffer, sizeof(buffer), 0) == 6);
    CHECK(buffer[3] == '\0' && buffer[4] == '\0' && buffer[5] == 'Q');
    CHECK(memfs_write(&state, MEMFS_FILE_PATH, "!", 1, 0, true) == 1);
    CHECK(state.size == 7 && state.data[6] == '!');
    CHECK(memfs_lookup(&state, MEMFS_FILE_PATH, &st) == 0 && st.st_size == 7);

    CHECK(memfs_write(&state, MEMFS_FILE_PATH, "x", 1, -1, false) == -EINVAL);
    CHECK(memfs_read(&state, MEMFS_FILE_PATH, buffer, sizeof(buffer), -1) ==
          -EINVAL);
    CHECK(memfs_write(&state, MEMFS_FILE_PATH, "x", 1,
                      (off_t)MEMFS_CAPACITY + 1, false) == -ENOSPC);
    CHECK(memfs_write(&state, "/missing", "x", 1, 0, false) == -ENOENT);
    CHECK(memfs_read(&state, "/", buffer, sizeof(buffer), 0) == -EISDIR);
    CHECK(memfs_write(&state, "/", "x", 1, 0, false) == -EISDIR);

    CHECK(memfs_open(&state, MEMFS_FILE_PATH, O_RDONLY | O_TRUNC) == -EINVAL);
    CHECK(memfs_open(&state, MEMFS_FILE_PATH, O_WRONLY | O_TRUNC) == 0);
    CHECK(state.size == 0);
    CHECK(memfs_read(&state, MEMFS_FILE_PATH, buffer, sizeof(buffer), 0) == 0);
    CHECK(memfs_write(&state, MEMFS_FILE_PATH, large, sizeof(large), 0, false) ==
          (int)MEMFS_CAPACITY);
    CHECK(state.size == MEMFS_CAPACITY);
    CHECK(memfs_write(&state, MEMFS_FILE_PATH, "x", 1,
                      (off_t)MEMFS_CAPACITY, false) == -ENOSPC);
    CHECK(memfs_read(&state, MEMFS_FILE_PATH, buffer, sizeof(buffer),
                     (off_t)MEMFS_CAPACITY) == 0);
}

int main(void)
{
    test_lookup_and_readonly_mode();
    test_memory_mode();
    test_invalid_arguments();
    puts("memfs core tests: PASS");
    return EXIT_SUCCESS;
}
