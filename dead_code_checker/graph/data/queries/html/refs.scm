; graph.extract.ts_generic uses of HTML. @ref.include = a file of ours that an attribute names (<script src>, <link href>, <a href>, <img src>); @cmd = the attribute
; name (rules.json ref_cmd.include keeps src and href, path_sub drops URLs and fragments); @scan = an inline handler (onclick="go()"): its calls are uses.

; the value is quoted ("a.js", 'a.js') or bare (src=a.js): both spellings are HTML
(attribute (attribute_name) @cmd (quoted_attribute_value (attribute_value) @name)) @ref.include
(attribute (attribute_name) @cmd (attribute_value) @name) @ref.include
(attribute (attribute_name) @cmd (quoted_attribute_value (attribute_value) @scan)) @block
(attribute (attribute_name) @cmd (attribute_value) @scan) @block
