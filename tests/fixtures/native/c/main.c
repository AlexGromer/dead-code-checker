#include "util.h"

struct handlers { int (*run)(int); };
static int on_event(int x) { return api(x); }
static int never_registered(int x) { return x; }
static struct handlers table = { on_event };

__attribute__((constructor)) static void boot(void) { table.run(1); }

int main(void) { return table.run(0); }
