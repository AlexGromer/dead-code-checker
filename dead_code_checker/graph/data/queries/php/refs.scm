; graph.extract.ts_generic uses of PHP. @ref.<kind> = the use, @name = the name, @member = also a member of that name, @cmd = the callee of a
; callback pattern, @scope = the derived class of an inherit / impl, @dyn = a dynamic site. A qualified name `\A\B\C` is read by its last name.

; f(), \Ns\f(), new Foo, new \Ns\Foo
(function_call_expression function: (name) @name) @ref.call
(function_call_expression function: (qualified_name (name) @name)) @ref.call
(object_creation_expression (name) @name) @ref.call
(object_creation_expression (qualified_name (name) @name)) @ref.call
; $x->m(), $x?->m(), Foo::m(), self::m(), static::m(): a receiver the text does not resolve is any member of that name (tier N)
(member_call_expression name: (name) @name @member) @ref.call
(nullsafe_member_call_expression name: (name) @name @member) @ref.call
(scoped_call_expression scope: (_) name: (name) @name @member) @ref.call
(scoped_call_expression scope: (name) @name) @ref.ref
(scoped_call_expression scope: (qualified_name (name) @name)) @ref.ref
; Foo::CONST, Foo::class, $x->prop
(class_constant_access_expression (name) @name) @ref.ref
(class_constant_access_expression (qualified_name (name) @name)) @ref.ref
(scoped_property_access_expression scope: (name) @name) @ref.ref
; type hints, instanceof, catch (A | B $e)
(named_type (name) @name) @ref.ref
(named_type (qualified_name (name) @name)) @ref.ref
(binary_expression right: (name) @name) @ref.ref
; class A extends B implements C { use T; }
(class_declaration name: (name) @scope (base_clause (name) @name)) @ref.inherit
(class_declaration name: (name) @scope (base_clause (qualified_name (name) @name))) @ref.inherit
(class_declaration name: (name) @scope (class_interface_clause (name) @name)) @ref.impl
(class_declaration name: (name) @scope (class_interface_clause (qualified_name (name) @name))) @ref.impl
(class_declaration name: (name) @scope (declaration_list (use_declaration (name) @name))) @ref.inherit
(class_declaration name: (name) @scope (declaration_list (use_declaration (qualified_name (name) @name)))) @ref.inherit
(trait_declaration name: (name) @scope (declaration_list (use_declaration (name) @name))) @ref.inherit
(interface_declaration name: (name) @scope (base_clause (name) @name)) @ref.inherit
(interface_declaration name: (name) @scope (base_clause (qualified_name (name) @name))) @ref.inherit
; call_user_func('f'), array_map('f', $a), add_action('init', 'cb'): a string argument of a callback function names a function (rules.json ref_cmd.ref)
; 'text' and "text" are two node types; an interpolated "a/$b" has more than the one string_content and is no name
(function_call_expression function: (name) @cmd arguments: (arguments (argument [(string (string_content) @name) (encapsed_string . (string_content) @name .)]))) @ref.ref
; [$this, 'm'], [Foo::class, 'm'], [new Foo, 'm'], ['Foo', 'm'], [$obj, 'm']: a callable array names a method of that class or of any class (a member of that name, tier N)
(array_creation_expression . (array_element_initializer (_)) . (array_element_initializer [(string (string_content) @name @member) (encapsed_string . (string_content) @name @member .)]) .) @ref.ref
; require 'a.php', include_once __DIR__ . '/b.php': a file of ours
[
  (require_expression [(string (string_content) @name) (encapsed_string . (string_content) @name .)])
  (require_once_expression [(string (string_content) @name) (encapsed_string . (string_content) @name .)])
  (include_expression [(string (string_content) @name) (encapsed_string . (string_content) @name .)])
  (include_once_expression [(string (string_content) @name) (encapsed_string . (string_content) @name .)])
] @ref.include
[
  (require_expression (binary_expression [(string (string_content) @name) (encapsed_string . (string_content) @name .)]))
  (require_once_expression (binary_expression [(string (string_content) @name) (encapsed_string . (string_content) @name .)]))
  (include_expression (binary_expression [(string (string_content) @name) (encapsed_string . (string_content) @name .)]))
  (include_once_expression (binary_expression [(string (string_content) @name) (encapsed_string . (string_content) @name .)]))
] @ref.include
[
  (require_expression (parenthesized_expression [(string (string_content) @name) (encapsed_string . (string_content) @name .)]))
  (require_once_expression (parenthesized_expression [(string (string_content) @name) (encapsed_string . (string_content) @name .)]))
  (include_expression (parenthesized_expression [(string (string_content) @name) (encapsed_string . (string_content) @name .)]))
  (include_once_expression (parenthesized_expression [(string (string_content) @name) (encapsed_string . (string_content) @name .)]))
] @ref.include
[
  (require_expression (parenthesized_expression (binary_expression [(string (string_content) @name) (encapsed_string . (string_content) @name .)])))
  (require_once_expression (parenthesized_expression (binary_expression [(string (string_content) @name) (encapsed_string . (string_content) @name .)])))
  (include_expression (parenthesized_expression (binary_expression [(string (string_content) @name) (encapsed_string . (string_content) @name .)])))
  (include_once_expression (parenthesized_expression (binary_expression [(string (string_content) @name) (encapsed_string . (string_content) @name .)])))
] @ref.include
; $obj->$m(), $f(), new $cls, $cls::m(), include $file: the name is only known at run time (tier D)
(member_call_expression name: (variable_name) @dyn)
(function_call_expression function: (variable_name) @dyn)
(object_creation_expression (variable_name) @dyn)
(scoped_call_expression scope: (variable_name) @dyn)
[(require_expression (variable_name) @dyn) (require_once_expression (variable_name) @dyn) (include_expression (variable_name) @dyn) (include_once_expression (variable_name) @dyn)]
