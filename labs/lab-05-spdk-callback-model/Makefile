CC ?= cc
CFLAGS ?= -O2 -g
CFLAGS += -std=c11 -Wall -Wextra -Wpedantic -Werror

.PHONY: all test clean

all: callback-model-test

callback-model-test: callback_model.c callback_model.h test_model.c
	$(CC) $(CFLAGS) -o $@ callback_model.c test_model.c

test: callback-model-test
	./callback-model-test

clean:
	rm -f callback-model-test
