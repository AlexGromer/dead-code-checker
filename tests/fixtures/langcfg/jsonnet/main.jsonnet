local lib = import 'lib/util.libsonnet';
local note = importstr 'notes.txt';
{ a: lib.x, b: note, c: import "data.json" }
