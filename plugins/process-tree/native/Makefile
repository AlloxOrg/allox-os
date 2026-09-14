OUTPUT ?= .output
CLANG ?= clang
CC ?= cc
BPFTOOL ?= bpftool
ARCH ?= x86
CFLAGS ?= -O2 -g -Wall -Wextra -Werror
BPF_CFLAGS ?= -O2 -g -target bpf -D__TARGET_ARCH_$(ARCH) -I/usr/include/$(shell $(CC) -dumpmachine)

.PHONY: all clean install
PREFIX ?= /usr/libexec/allox

all: allox-process-tracker

$(OUTPUT):
	mkdir -p $@

$(OUTPUT)/allox_process.bpf.o: allox_process.bpf.c allox_process.h | $(OUTPUT)
	$(CLANG) $(BPF_CFLAGS) -c $< -o $@

$(OUTPUT)/allox_process.skel.h: $(OUTPUT)/allox_process.bpf.o
	$(BPFTOOL) gen skeleton $< > $@

allox-process-tracker: allox-process-tracker.c allox_process.h $(OUTPUT)/allox_process.skel.h
	$(CC) $(CFLAGS) -I$(OUTPUT) $< -o $@ -lbpf -lelf -lz

install: allox-process-tracker
	install -D -m 0755 allox-process-tracker $(DESTDIR)$(PREFIX)/allox-process-tracker

clean:
	rm -rf $(OUTPUT) allox-process-tracker
