#include "util.h"

static int helper(int x) { return SQUARE(x) + 1; }
#define LOG(x) log_impl(x)
static int log_impl(int x) { return x; }
static int dead_static(int x) { return x - 1; }
int api(int x) { return helper(x) + LOG(x); }
int unused_export(int x) { return dead_static(x); }
