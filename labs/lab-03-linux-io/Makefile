CC ?= cc
CFLAGS ?= -O2 -g
CFLAGS += -std=c11 -Wall -Wextra -Wpedantic -Werror -D_FILE_OFFSET_BITS=64

.PHONY: all test clean uring test-uring

all: io-lab

io-lab: io_lab.c
	$(CC) $(CFLAGS) -o $@ $<

# Optional. Requires liburing development headers/library and a permitted kernel.
uring: uring-file

uring-file: uring_file.c
	$(CC) $(CFLAGS) $$(pkg-config --cflags liburing) -o $@ $< $$(pkg-config --libs liburing)

test: all
	./test.sh

test-uring: uring
	./test-uring.sh

clean:
	rm -f io-lab uring-file
