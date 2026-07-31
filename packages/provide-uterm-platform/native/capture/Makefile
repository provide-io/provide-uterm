UNAME := $(shell uname)

COMMON_CFLAGS = -std=c11 -fPIC -O2 -Wall -Wextra -Werror -pthread
TEST_TARGET = test_capture_writer

ifeq ($(UNAME), Darwin)
    TARGET  = libuterm_capture.dylib
    LDFLAGS = -dynamiclib
else
    TARGET  = libuterm_capture.so
    LDFLAGS = -shared -ldl
endif

.PHONY: all clean test

all: $(TARGET)

$(TARGET): capture.c capture_writer.c capture_writer.h
	cc $(COMMON_CFLAGS) -o $(TARGET) capture.c capture_writer.c $(LDFLAGS)

$(TEST_TARGET): test_capture_writer.c capture_writer.c capture_writer.h
	cc $(COMMON_CFLAGS) -o $(TEST_TARGET) test_capture_writer.c capture_writer.c

test: $(TEST_TARGET)
	./$(TEST_TARGET)

clean:
	rm -f $(TARGET) $(TEST_TARGET)
	rm -rf $(TARGET).dSYM $(TEST_TARGET).dSYM
