; graph.extract.cfg (HCL), see defs.scm. Every attribute expression: the mapping reads var./local./data./module./<type>.<name>
; references from its text (a "${var.x}" interpolation sits inside the expression too).
(attribute (expression) @expr)
; an attribute at the top of the file (a .tfvars file) assigns a variable
(config_file (body (attribute (identifier) @tfvars.name)))
