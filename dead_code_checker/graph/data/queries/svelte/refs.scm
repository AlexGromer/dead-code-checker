; graph.extract.ts_generic uses of a Svelte component (the <script> block is extracted as JS / TS, see embed.scm). A tag is a use of the component of
; that name (<Child/>); @scan = the text of a template expression ({name}, on:click={inc}, {#if a > 1}, {#each items as item}): its words are uses.

(start_tag (tag_name) @name) @ref.ref
(self_closing_tag (tag_name) @name) @ref.ref
(raw_text_expr) @scan
(raw_text_each) @scan
; the value is quoted or bare (src=a.png): both spellings are HTML
(attribute (attribute_name) @cmd (quoted_attribute_value (attribute_value) @name)) @ref.include
(attribute (attribute_name) @cmd (attribute_value) @name) @ref.include
; use:act, transition:fade, in:a, out:b, animate:flip, bind:value, class:on, style:color: the name after the colon is a function or a variable of the script (rules path_sub.call keeps only these)
(attribute (attribute_name) @name) @ref.call
