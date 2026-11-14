; graph.extract.cfgts (dockerfile). @from: `--from=x` of a COPY / ADD, @mount: a `from=x` of a RUN --mount, @image: the base of a FROM
; written bare, without a tag or a digest (a stage name when an earlier stage has it).
(copy_instruction (param) @from)
(run_instruction (mount_param (mount_param_param) @mount))
(from_instruction (image_spec . (image_name) @image .))
