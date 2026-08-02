# 03. Polyglot Analyzer Engine (Determinisztikus Elemző Réteg)

A **Polyglot Analyzer Engine** felelős a teljes kódbázis folyamatos, determinisztikus elemzéséért. A modul **0 AI token elhasználásával** állítja elő az Absztrakt Szintaxisfát (AST), a szimbólumtáblákat, az import/export függőségeket és a hívási gráfokat (Call Graphs).

---

## 1. Támogatott Nyelvek és Parser Technológia

A determinisztikus parser réteg a **Tree-sitter** (Python kötések: `py-tree-sitter`) könyvtárra épül, amely inkrementális és szupergyors kódértelmezést biztosít.

### 1.1. Elsődlegesen Támogatott Nyelvek
- **JavaScript / TypeScript** (`tree-sitter-typescript`)
- **HTML / Template-ek** (`tree-sitter-html`)
- **CSS / SASS** (`tree-sitter-css`)
- **SQL** (`tree-sitter-sql`)
- **Java** (`tree-sitter-java`)
- **Python** (`tree-sitter-python`)

---

## 2. Szimbólum & Gráf Kinyerési Folyamat

```
[Forráskód Fájl] 
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

### 2.1. Szimbólum Kinyerés (Symbol Extraction)
A parser átvizsgálja a forráskódot, és kivonja a következő metaadatokat:
- **Függvények / Metódusok**: Név, paraméterek típusokkal, visszatérési értékek, docstring-ek, sorok száma (StartLine, EndLine).
- **Osztályok / Interfészek**: Tagváltozók, öröklődési kapcsolatok, láthatóság (public/private/protected).
- **Import / Export Nyilatkozatok**: Fájlok közötti statikus függőségi háló.

### 2.2. Hívási Gráf (Call Graph) Építés
A rendszer determinisztikusan feltérképezi, hogy az egyes függvények mely más függvényeket hívják meg:
```json
{
  "caller": "src/services/UserService.ts::createUser",
  "callee": "src/repositories/UserRepository.ts::save",
  "line": 42,
  "type": "DIRECT_CALL"
}
```

---

## 3. Inkrementális Elemzés és Invalidation

1. **File Watcher Integration**: A rendszer a gazdagép fájlrendszer eseményeit (`watchdog` / `inotify`) figyeli.
2. **Delta Parsing**: Amikor egy ágens módosít egy fájlt, a Polyglot Analyzer **csak a módosított fájlt** és annak közvetlen függőségeit parse-olja újra Tree-sitter segítségével.
3. **Inkrementális AST Frissítés**: A teljes projekt kódjának újrapasszolása helyett ez ezredmásodpercek alatt frissíti a szimbólumtáblát.

---

## 4. Példa: Py-Tree-Sitter Integrációs Interfész (Python)

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
        # Tree Sitter S-expression query vagy AST bejárás
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
