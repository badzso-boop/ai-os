(class_declaration
  name: (identifier) @class.name) @class.def

(function_declaration
  name: (identifier) @function.name) @function.def

(method_definition
  name: (property_identifier) @function.name) @function.def

(call_expression
  function: (identifier) @call.name) @call.site

(call_expression
  function: (member_expression
    property: (property_identifier) @call.name)) @call.site
