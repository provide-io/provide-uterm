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

.PHONY: all clean install

all: $(TARGET)

$(TARGET): pam_uterm.c
	cc $(CFLAGS) $(LDFLAGS) -o $(TARGET) pam_uterm.c

install:
ifeq ($(UNAME), Darwin)
	sudo cp $(TARGET) /usr/lib/pam/
else
	sudo cp $(TARGET) /usr/lib/security/
endif

clean:
	rm -f $(TARGET)
