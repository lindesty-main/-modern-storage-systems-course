CXX ?= g++
CXXFLAGS ?= -std=c++20 -O2 -g -Wall -Wextra -Wpedantic -Wconversion -Wshadow
CPPFLAGS ?=
LDFLAGS ?=

TARGET := build/ministore
TEST_TARGET := build/ministore_test
SOURCES := src/ministore.cpp

.PHONY: all test ci clean
all: $(TARGET)

$(TARGET): $(SOURCES)
	@mkdir -p build
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $(SOURCES) -o $@ $(LDFLAGS)

$(TEST_TARGET): $(SOURCES)
	@mkdir -p build
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -DMINISTORE_ENABLE_TEST_FAILPOINTS $(SOURCES) -o $@ $(LDFLAGS)

test: $(TARGET) $(TEST_TARGET)
	PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -v

# CI-style gate: force both binaries to rebuild before running the full local suite.
ci:
	$(MAKE) clean
	$(MAKE) test

clean:
	rm -rf build
