UNAME := $(shell uname)

ifeq ($(UNAME), Darwin)
    TARGET  = pam_uterm.so
    LDFLAGS = -dynamiclib -lpam
    CFLAGS  = -fPIC -O2 -Wall -Wextra -Werror
else
    TARGET  = pam_uterm.so
    LDFLAGS = -shared -lpam
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
