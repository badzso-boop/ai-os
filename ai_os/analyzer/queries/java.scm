(class_declaration
  name: (identifier) @class.name) @class.def

(interface_declaration
  name: (identifier) @class.name) @class.def

(constructor_declaration
  name: (identifier) @function.name) @function.def

(method_declaration
  name: (identifier) @function.name) @function.def

(method_invocation
  name: (identifier) @call.name) @call.site

(object_creation_expression
  type: (type_identifier) @call.name) @call.site
