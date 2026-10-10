CXX ?= g++
CXXFLAGS ?= -std=c++20 -O2 -g -Wall -Wextra -Wpedantic -Wconversion -Wshadow
CPPFLAGS ?= -Isrc
LDFLAGS ?=

TARGET := build/index_cache_lab
TEST_TARGET := build/test_index_cache

.PHONY: all test clean
all: $(TARGET)

$(TARGET): src/main.cpp src/index_cache.hpp
	@mkdir -p build
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) src/main.cpp -o $@ $(LDFLAGS)

$(TEST_TARGET): tests/test_index_cache.cpp src/index_cache.hpp
	@mkdir -p build
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -UNDEBUG tests/test_index_cache.cpp -o $@ $(LDFLAGS)

test: $(TARGET) $(TEST_TARGET)
	./$(TEST_TARGET)
	./$(TARGET) --report

clean:
	rm -rf build
