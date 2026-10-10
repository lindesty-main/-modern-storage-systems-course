CC = gcc
CFLAGS ?= -O2 -g
CFLAGS += -std=c11 -Wall -Wextra -Wpedantic -Wconversion -Wshadow

.PHONY: all test clean

all: block-image

block-image: block-image.c
	$(CC) $(CFLAGS) -o $@ $<

test: block-image
	./test.sh ./block-image

clean:
	rm -f block-image
