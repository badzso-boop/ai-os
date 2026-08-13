# 03. Polyglot Analyzer Engine (Deterministic Analysis Layer)

The **Polyglot Analyzer Engine** is responsible for continuous, deterministic analysis of the entire codebase. Utilizing **0 AI tokens**, the module produces Abstract Syntax Trees (AST), symbol tables, import/export dependencies, and Call Graphs.

---

## 1. Supported Languages and Parser Technology

The deterministic parser layer is built on **Tree-sitter** (Python bindings: `py-tree-sitter`), providing incremental and high-speed code parsing.

### 1.1. Primary Supported Languages
- **JavaScript / TypeScript** (`tree-sitter-typescript`)
- **HTML / Templates** (`tree-sitter-html`)
- **CSS / SASS** (`tree-sitter-css`)
- **SQL** (`tree-sitter-sql`)
- **Java** (`tree-sitter-java`)
- **Python** (`tree-sitter-python`)

---

## 2. Symbol & Graph Extraction Process

```
[Source Code File] 
       │
       ▼
 [Tree-sitter Parser] ────► [AST (Abstract Syntax Tree)]
                                    │
                                    ▼
                        [Symbol & AST Extractor]
                                    │
       ┌────────────────────────────┼────────────────────────────┐
       ▼                            ▼                            ▼
[Class/Func Definitions]   [Import/Export Dependencies]   [Call Graph Nodes]
       │                            │                            │
       └────────────────────────────┼────────────────────────────┘
                                    ▼
                     [Knowledge Engine Indexer]
```

### 2.1. Symbol Extraction
The parser scans the source code and extracts the following metadata:
- **Functions / Methods**: Name, parameters with types, return values, docstrings, line counts (StartLine, EndLine).
- **Classes / Interfaces**: Member variables, inheritance relationships, visibility (public/private/protected).
- **Import / Export Statements**: Static dependency network across files.

### 2.2. Call Graph Construction
The system deterministically maps which functions call which other functions:
```json
{
  "caller": "src/services/UserService.ts::createUser",
  "callee": "src/repositories/UserRepository.ts::save",
  "line": 42,
  "type": "DIRECT_CALL"
}
```

---

## 3. Incremental Analysis and Invalidation

1. **File Watcher Integration**: The system monitors host filesystem events (`watchdog` / `inotify`).
2. **Delta Parsing**: When an agent modifies a file, the Polyglot Analyzer re-parses **only the modified file** and its immediate dependencies using Tree-sitter.
3. **Incremental AST Update**: Instead of re-parsing the entire project codebase, symbol tables are updated in milliseconds.

---

## 4. Example: Py-Tree-Sitter Integration Interface (Python)

```python
from tree_sitter import Language, Parser
import tree_sitter_typescript as tstypescript

class PolyglotAnalyzer:
    def __init__(self):
        self.ts_language = Language(tstypescript.language())
        self.parser = Parser(self.ts_language)

    def parse_code(self, source_code: bytes):
        tree = self.parser.parse(source_code)
        root_node = tree.root_node
        return self._extract_symbols(root_node, source_code)

    def _extract_symbols(self, node, source_code: bytes):
        symbols = []
        # Tree Sitter S-expression query or AST traversal
        for child in node.children:
            if child.type in ("function_declaration", "class_declaration", "interface_declaration"):
                name_node = child.child_by_field_name("name")
                if name_node:
                    symbols.append({
                        "type": child.type,
                        "name": source_code[name_node.start_byte:name_node.end_byte].decode("utf-8"),
                        "start_line": child.start_point[0] + 1,
                        "end_line": child.end_point[0] + 1,
                    })
        return symbols
```

