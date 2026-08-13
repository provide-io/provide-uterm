UNAME := $(shell uname)

COMMON_CFLAGS = -std=c11 -fPIC -O2 -Wall -Wextra -Werror -fvisibility=hidden
TEST_CFLAGS = $(COMMON_CFLAGS) -pthread
TEST_TARGET = test_capture_writer
EARLY_TEST = test_preload_early
SYMBOL_TEST = test_symbols.sh

ifeq ($(UNAME), Darwin)
    TARGET  = libuterm_capture.dylib
    LDFLAGS = -dynamiclib
    EARLY_TEST =
else
    TARGET  = libuterm_capture.so
    LDFLAGS = -shared -ldl
endif

.PHONY: all clean test install

all: $(TARGET)

$(TARGET): capture.c capture_writer.c capture_writer.h
	cc $(COMMON_CFLAGS) -o $(TARGET) capture.c capture_writer.c $(LDFLAGS)

$(TEST_TARGET): test_capture_writer.c capture_writer.c capture_writer.h
	cc $(TEST_CFLAGS) -o $(TEST_TARGET) test_capture_writer.c capture_writer.c

test_preload_early: test_preload_early.c
	cc -std=c11 -O2 -Wall -Wextra -Werror -o $@ $<

test: $(TEST_TARGET) $(TARGET) $(EARLY_TEST)
	./$(TEST_TARGET)
	./$(SYMBOL_TEST) $(TARGET) $(UNAME)
	@if [ -n "$(EARLY_TEST)" ]; then \
		UTERM_CAPTURE_SOCKET=/run/uterm-preinit-missing.sock \
		LD_PRELOAD=./$(TARGET) ./$(EARLY_TEST); \
	fi

# Where every consumer loads the library from. It is a build artifact, ignored
# by git, and copying it by hand is how a checkout ends up running a library
# built from sources that have since changed.
NATIVE_DIR = ../../src/provide/uterm/pty/_native

install: $(TARGET)
	cp $(TARGET) $(NATIVE_DIR)/

clean:
	rm -f $(TARGET) $(TEST_TARGET) test_preload_early
	rm -rf $(TARGET).dSYM $(TEST_TARGET).dSYM test_preload_early.dSYM
