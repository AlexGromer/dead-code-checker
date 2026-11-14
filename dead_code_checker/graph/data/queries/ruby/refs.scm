; graph.extract.ts_generic uses of Ruby. @ref.<kind> = the use, @name = the name, @member = also a member of that name, @cmd = the callee of an
; include pattern, @scope = the derived class of an inherit, @dyn = a dynamic site.

; foo(1), recv.foo(1): a receiver the text does not resolve is any member of that name (tier N)
(call method: (identifier) @name) @ref.call
(call receiver: (_) method: (identifier) @name @member) @ref.call
; a bare word on a line of its own is a call of a method without arguments
(body_statement (identifier) @name) @ref.call
; Foo, Foo::Bar, include Concern, rescue MyError: every constant is a use of the class or module (the name of a definition is not)
(constant) @name @ref.ref
; a method passed by name or by symbol: before_action :authenticate, method(:x), scope :s, send(:x) (tier N)
(argument_list (identifier) @name) @ref.ref
(argument_list (simple_symbol) @name) @ref.ref
; map(&:norm) calls norm on the elements (a member of an object the text does not show), each(&blk) names a method or a proc
(block_argument (simple_symbol) @name @member) @ref.ref
(block_argument (identifier) @name) @ref.ref
(pair value: (simple_symbol) @name) @ref.ref
(array (simple_symbol) @name) @ref.ref
(assignment right: (identifier) @name) @ref.ref
; alias new old, alias_method :new, :old
(alias alias: (identifier) @name) @ref.ref
; class A < B
(class name: (_) @scope superclass: (superclass (_) @name)) @ref.inherit
; require "a/b", require_relative "x", load "tasks.rb": a file of ours (rules.json ref_cmd, path_sub)
(call method: (identifier) @cmd arguments: (argument_list . (string (string_content) @name))) @ref.include
; send(name), public_send("x_#{y}"), define_method(name), constantize: the callee is only known at run time (tier D)
(call method: (identifier) @cmd arguments: (argument_list . [(identifier) (string (interpolation)) (call)] @dyn)) @block
; a bare name is a call on self (an inherited or mixed-in method: rules self_word) unless a parameter or a local binds it (@local)
(identifier) @name @self @ref.ref
(method_parameters (identifier) @local)
(block_parameters (identifier) @local)
(lambda_parameters (identifier) @local)
(optional_parameter name: (identifier) @local)
(keyword_parameter name: (identifier) @local)
(splat_parameter name: (identifier) @local)
(hash_splat_parameter name: (identifier) @local)
(block_parameter name: (identifier) @local)
(assignment left: (identifier) @local)
(operator_assignment left: (identifier) @local)
(left_assignment_list (identifier) @local)
(exception_variable (identifier) @local)
