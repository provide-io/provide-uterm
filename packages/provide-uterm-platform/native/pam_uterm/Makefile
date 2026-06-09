UNAME := $(shell uname)

ifeq ($(UNAME), Darwin)
    TARGET  = pam_uterm.so
    # macOS: link against libpam — the framework is separate from the loader
    LDFLAGS = -dynamiclib -lpam
    CFLAGS  = -fPIC -O2 -Wall -Wextra -Werror
else
    TARGET  = pam_uterm.so
    # Linux: do NOT link against libpam — symbols are provided by the PAM loader
    # at dlopen time via RTLD_GLOBAL. Linking against -lpam causes symbol conflicts.
    LDFLAGS = -shared
    CFLAGS  = -fPIC -O2 -Wall -Wextra -Werror
endif

.PHONY: all clean install test

all: $(TARGET)

$(TARGET): pam_uterm.c
	cc $(CFLAGS) $(LDFLAGS) -o $(TARGET) pam_uterm.c

# Build & run the C self-test for the static helpers (e.g. _json_escape). The
# module source is #include'd to reach the static function, so -lpam is needed
# to resolve the PAM session symbols it pulls in. The native lib is not built in
# CI, so this is a local correctness gate.
test: test_pam_uterm.c pam_uterm.c
	cc $(CFLAGS) -o pam_uterm_selftest test_pam_uterm.c -lpam
	./pam_uterm_selftest
	rm -f pam_uterm_selftest

install:
ifeq ($(UNAME), Darwin)
	sudo cp $(TARGET) /usr/lib/pam/
else
	sudo cp $(TARGET) /usr/lib/security/
endif

clean:
	rm -f $(TARGET) pam_uterm_selftest
