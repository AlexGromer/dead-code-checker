; graph.extract.ts_generic uses of Nix. @ref.<kind> = the use, @name = the name, @member = also a member of that name, @cmd = the callee of a dyn
; pattern, @dyn = a dynamic site. A path literal is a file of ours (rules.json path_sub: a directory is its default.nix).

(apply_expression function: (variable_expression (identifier) @name)) @ref.call
(variable_expression (identifier) @name) @ref.ref
; pkgs.callPackage, lib.foo.bar, (import ./lib.nix).bar: any member of that name (tier N)
(select_expression (attrpath (identifier) @name @member)) @ref.ref
; inherit helper; inherit (lib) mkIf;
(inherited_attrs (identifier) @name) @ref.ref
; import ./x.nix, callPackage ./pkg.nix {}, imports = [ ./a.nix ../common ]
(path_expression) @name @ref.include
; import path, import (dir + "/${n}.nix"): the file is only known at run time (tier D)
(apply_expression function: (variable_expression (identifier) @cmd) argument: [(variable_expression) (parenthesized_expression) (select_expression)] @dyn) @block
