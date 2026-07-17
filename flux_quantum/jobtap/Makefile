# Build the quantum jobtap plugin against an installed flux-core.
# Requires flux-core development headers (flux/jobtap.h) and jansson.
#
#   make            # builds quantum.so
#   make clean
#
# If pkg-config cannot find flux-core, set FLUX_CFLAGS / FLUX_LIBS by hand,
# e.g.  make FLUX_CFLAGS="-I/usr/include" FLUX_LIBS="-lflux-core -ljansson"

CC        ?= cc
FLUX_CFLAGS ?= $(shell pkg-config --cflags flux-core 2>/dev/null)
FLUX_LIBS   ?= $(shell pkg-config --libs flux-core 2>/dev/null) -ljansson
CFLAGS    ?= -Wall -Werror -fPIC -O2

quantum.so: quantum.c
	$(CC) $(CFLAGS) $(FLUX_CFLAGS) -shared -o $@ $< $(FLUX_LIBS)

clean:
	rm -f quantum.so

.PHONY: clean
