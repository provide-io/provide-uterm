UNAME := $(shell uname)

ifeq ($(UNAME), Darwin)
    TARGET  = libuterm_capture.dylib
    LDFLAGS = -dynamiclib
    CFLAGS  = -fPIC -O2 -Wall -Wextra -Werror
else
    TARGET  = libuterm_capture.so
    LDFLAGS = -shared -ldl
    CFLAGS  = -fPIC -O2 -Wall -Wextra -Werror
endif

.PHONY: all clean

all: $(TARGET)

$(TARGET): capture.c
	cc $(CFLAGS) $(LDFLAGS) -o $(TARGET) capture.c

clean:
	rm -f $(TARGET)
