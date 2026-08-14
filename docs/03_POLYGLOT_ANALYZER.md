# 03. Polyglot Analyzer Engine (Determinisztikus Elemzo Reteg)

A **Polyglot Analyzer Engine** is responsible for teljes kodbazis folyamatos, determinisztikus elemzeseert. A modul **0 AI token elhasznalasaval** allitja elo az Absztrakt Szintaxisfat (AST), a szimbolumtablakat, az import/export fuggosegeket and the hivasi grafokat (Call Graphs).

---

## 1. Tamogatott Nyelvek es Parser Technologia

A determinisztikus parser reteg a **Tree-sitter** (Python kotesek: `py-tree-sitter`) konyvtarra epul, amely inkrementalis es szupergyors kodertelmezest biztosit.

### 1.1. Elsodlegesen Tamogatott Nyelvek
- **JavaScript / TypeScript** (`tree-sitter-typescript`)
- **HTML / Template-ek** (`tree-sitter-html`)
- **CSS / SASS** (`tree-sitter-css`)
- **SQL** (`tree-sitter-sql`)
- **Java** (`tree-sitter-java`)
- **Python** (`tree-sitter-python`)

---

## 2. Szimbolum & Graf Kinyeresi Folyamat

```
[Forraskod Fajl] 
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

### 2.1. Szimbolum Kinyeres (Symbol Extraction)
A parser atvizsgalja a forraskodot, es kivonja a kovetkezo metaadatokat:
- **Fuggvenyek / Metodusok**: Nev, parameterek tipusokkal, visszateresi ertekek, docstring-ek, sorok szama (StartLine, EndLine).
- **Osztalyok / Interfeszek**: Tagvaltozok, oroklodesi kapcsolatok, lathatosag (public/private/protected).
- **Import / Export Nyilatkozatok**: Fajlok kozotti statikus fuggosegi halo.

### 2.2. Hivasi Graf (Call Graph) Epites
A rendszer determinisztikusan felterkepezi, that the egyes fuggvenyek mely mas fuggvenyeket hivjak meg:
```json
{
  "caller": "src/services/UserService.ts::createUser",
  "callee": "src/repositories/UserRepository.ts::save",
  "line": 42,
  "type": "DIRECT_CALL"
}
```

---

## 3. Inkrementalis Elemzes es Invalidation

1. **File Watcher Integration**: A rendszer a gazdagep fajlrendszer esemenyeit (`watchdog` / `inotify`) figyeli.
2. **Delta Parsing**: Amikor egy agens modosit egy fajlt, a Polyglot Analyzer **csak a modositott fajlt** es annak kozvetlen fuggosegeit parse-olja ujra Tree-sitter segitsegevel.
3. **Inkrementalis AST Frissites**: A teljes projekt kodjanak ujrapasszolasa instead of ez ezredmasodpercek alatt frissiti a szimbolumtablat.

---

## 4. Pelda: Py-Tree-Sitter Integracios Interfesz (Python)

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
        # Tree Sitter S-expression query vagy AST bejaras
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
