; graph.extract.ts_generic roots of C#. Vocabulary as c/roots.scm; the filters are in rules.json.
(comment) @root.marker

; static void Main(...)  and top-level statements (the file is the program)
(method_declaration name: (identifier) @root.main)
(compilation_unit (global_statement) @root.main-toplevel)

; [Fact] [Theory] [Test] [TestMethod] ...: a test; [HttpGet] [ApiController] [Function] ...: a framework calls it
(attribute_list (attribute name: (identifier) @root.test))
(attribute_list (attribute name: (qualified_name) @root.test))
(attribute_list (attribute name: (identifier) @root.annotation.aspnet))
(attribute_list (attribute name: (qualified_name) @root.annotation.aspnet))
(attribute_list (attribute name: (identifier) @root.annotation.azure-function))
(attribute_list (attribute name: (identifier) @root.annotation.runtime-entry))

; [Export(typeof(IPlugin))] class P / [ServiceContract] / [DataContract] / [XmlRoot] / [ComVisible] / [CreateAssetMenu] / [Table("t")] ...: a host (MEF, WCF,
; a serializer, COM, Unity, EF) builds the type or calls the member by its attribute, no reference shows it (the list is rules.json, annotation.host-attr)
(attribute_list (attribute name: (identifier) @root.annotation.host-attr))
(attribute_list (attribute name: (qualified_name) @root.annotation.host-attr))

; any attribute on a property or a field: a framework reads the member by reflection (rules.json: kinds var, flag protocol, no root; a
; compiler or tooling attribute is filtered out by the regex). The attribute may also sit on a method or a class: the kinds filter drops those.
(attribute_list (attribute name: (identifier) @root.annotation.member-attr))
(attribute_list (attribute name: (qualified_name) @root.annotation.member-attr))

; class C : BackgroundService / ControllerBase / Hub ...: the host instantiates it
(base_list (identifier) @root.annotation.dotnet-host)
(base_list (generic_name (identifier) @root.annotation.dotnet-host))
(base_list (qualified_name) @root.annotation.dotnet-host)
