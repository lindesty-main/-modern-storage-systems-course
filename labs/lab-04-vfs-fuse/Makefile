CC ?= cc
CFLAGS ?= -O2 -g
CFLAGS += -std=c11 -Wall -Wextra -Wpedantic -Werror -D_FILE_OFFSET_BITS=64

.PHONY: all test clean fuse test-fuse check-fuse-deps

all: memfs-core-test

memfs-core-test: memfs_core.c memfs_core.h test_memfs_core.c
	$(CC) $(CFLAGS) -o $@ memfs_core.c test_memfs_core.c

test: memfs-core-test
	./test.sh

check-fuse-deps:
	@pkg-config --exists fuse3 || { \
		echo 'libfuse3 development files and pkg-config are required (optional target)' >&2; \
		exit 1; \
	}

fuse: fuse-memfs

fuse-memfs: fuse_memfs.c memfs_core.c memfs_core.h check-fuse-deps
	$(CC) $(CFLAGS) $$(pkg-config --cflags fuse3) -pthread \
		-o $@ fuse_memfs.c memfs_core.c $$(pkg-config --libs fuse3)

test-fuse: fuse-memfs
	./test-fuse.sh

clean:
	rm -f memfs-core-test fuse-memfs
