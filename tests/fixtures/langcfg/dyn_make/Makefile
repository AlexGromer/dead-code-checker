# fixture: computed prerequisites and arguments keep recipes alive (dynamic sites), as in the Helm layer
SUBS = sub1 sub2
TARGETS = tx
OBJS := a.o
BIN = app

all: $(SUBS)
	$(MAKE) $(TARGETS) $(MAKEFLAGS)

$(BIN): $(OBJS) $(wildcard src/*.c)
	@echo link

sub1:
	@echo 1
sub2:
	@echo 2
tx:
	@echo x
a.o:
	@echo o
