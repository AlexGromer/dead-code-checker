; graph.extract.ts_generic uses of a Vue single-file component (the <script> block is extracted as JS / TS, see embed.scm). A tag is a use of the component of
; that name (<MyComp/>, <my-comp>: rules.json pascal); @scan = the text of a template expression ({{ fmt(n) }}, @click="handler($event)", :prop="val"): its words are uses.

(start_tag (tag_name) @name) @ref.ref
(self_closing_tag (tag_name) @name) @ref.ref
(interpolation (raw_text) @scan)
(directive_attribute (quoted_attribute_value (attribute_value) @scan))
(directive_attribute (attribute_value) @scan)
; the value is quoted or bare (src=a.png): both spellings are HTML
(attribute (attribute_name) @cmd (quoted_attribute_value (attribute_value) @name)) @ref.include
(attribute (attribute_name) @cmd (attribute_value) @name) @ref.include
