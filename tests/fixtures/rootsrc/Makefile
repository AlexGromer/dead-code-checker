PY ?= python3
SCRIPTS = scripts

all: gen test

gen:
	@$(PY) $(SCRIPTS)/gen.py --out build
	./scripts/fmt.sh \
	    --check

test:
	$(MAKE) -C sub all
	$(CP) data/in.txt build/in.txt
	$(UNKNOWN) tools/lint.py
	VERSION=$$(./scripts/version.sh)

VER := $(shell python3 tools/ver.py)
