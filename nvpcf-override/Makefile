CC ?= cc
CFLAGS ?= -O2 -Wall -Wextra -Werror

.PHONY: all clean

all: libnvpcf_override.so

libnvpcf_override.so: nvpcf_override.c
	$(CC) $(CFLAGS) -fPIC -shared -o $@ $< -ldl

clean:
	rm -f libnvpcf_override.so
