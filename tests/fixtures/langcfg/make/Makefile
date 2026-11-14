# fixture: a Makefile whose recipes are reached in different ways
include config.mk
-include $(wildcard local/*.mk)

.PHONY: test lint docs
.DEFAULT_GOAL := all

all: build
build: gen | out
	$(MAKE) -C sub check
gen:
	@echo generate
test:
	@echo test
lint:
	@echo lint
docs:
	@echo docs
deploy: package
	@echo deploy
package:
	@echo package
cleanup-old:
	@echo nobody asks for this
out:
	@echo out

%.o: %.c
	$(CC) -c $<
