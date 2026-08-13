# 08. Knowledge Graph Schema & Subgraph Extraction Algorithms

This document is the **AI-OS Knowledge & Context Engine** melyszintu specification. Kidolgozza a Tudasgraf (Knowledge Graph) grafadatbazis semajat, a $k$-hop szomszedsagi kinyero algoritmust, a tomoritett kodvazak (Skeleton Stubs) automatikus generalasat, valamint a Python nyelven megirt teljes referencia-implementaciot.

---

## 1. Tudasgraf Sema (Node & Edge Schema)

A Tudasgraf (`NetworkX` in-memory vagy `Neo4j` perzisztens adatbazisban) a forraskod entitasait es azok relacioit tarolja determinisztikus Tree-sitter elemzes based on.

```mermaid
classDiagram
    class FileNode {
        +string filepath
        +string language
        +string content_hash
        +boolean is_locked
    }
    class ClassNode {
        +string fqn
        +string name
        +string visibility
        +int start_line
        +int end_line
    }
    class FunctionNode {
        +string fqn
        +string name
        +string return_type
        +list~dict~ parameters
        +string docstring
    }
    class TypeNode {
        +string fqn
        +string type_kind
        +string raw_stub
    }

    FileNode "1" -- "*" ClassNode : CONTAINS
    FileNode "1" -- "*" FunctionNode : CONTAINS
    FileNode "1" -- "*" FileNode : IMPORTS
    FunctionNode "1" -- "*" FunctionNode : CALLS
    ClassNode "1" -- "*" ClassNode : EXTENDS/IMPLEMENTS
    FunctionNode "1" -- "*" TypeNode : USES_TYPE
```

### 1.1. Csomopontok (Nodes) Specifikacioja

| Node Type | FQN (Fully Qualified Name) Pelda | Kulcs Nev-ertek Par Attributumok |
| :--- | :--- | :--- |
| `FileNode` | `src/services/UserService.ts` | `filepath`, `language`, `hash`, `write_locked_by` |
| `ClassNode` | `src/services/UserService.ts::UserService` | `name`, `is_abstract`, `start_line`, `end_line` |
| `FunctionNode` | `src/services/UserService.ts::UserService.createUser` | `name`, `return_type`, `params`, `docstring` |
| `TypeNode` | `src/types/user.ts::UserDTO` | `name`, `kind` (`interface` / `enum` / `type`), `stub` |

### 1.2. Elek (Edges) Specifikacioja

| Edge Type | Source ➔ Target | Description / Attributes |
| :--- | :--- | :--- |
| `CONTAINS` | `FileNode ➔ ClassNode / FunctionNode` | Fajlstruktura tagsag |
| `IMPORTS` | `FileNode A ➔ FileNode B` | Statikus import fuggoseg (`imported_symbols`) |
| `CALLS` | `FunctionNode A ➔ FunctionNode B` | Hivasi graf el (`call_line`, `is_async`) |
| `EXTENDS` | `ClassNode A ➔ ClassNode B` | Osztalyoroklodes |
| `USES_TYPE` | `FunctionNode A ➔ TypeNode T` | Tipusfuggoseg a szignaturaban |

---

## 2. $k$-Hop Subgraph Extraction Algoritmus

Amikor egy AI agens feladatot kap (pl. `target_files = ["src/services/UserService.ts"]`), a rendszer **nem kuldi el a teljes kodbazist**. Ehelyett kiszamitja a celcsomopontok $k$-hop sugaru szomszedsagi grafjat.

```mermaid
graph TD
    subgraph Full Knowledge Graph
        Seed[Seed Node: UserService.ts]:::seed
        Dep1[File: UserRepository.ts]:::hop1
        Dep2[Type: UserDTO.ts]:::hop1
        Dep3[DbConnection.ts]:::hop2
        Unrelated[UnrelatedController.ts]:::pruned

        Seed -->|IMPORTS| Dep1
        Seed -->|USES_TYPE| Dep2
        Dep1 -->|IMPORTS| Dep3
    end

    classDef seed fill:#f96,stroke:#333,stroke-width:2px;
    classDef hop1 fill:#6bf,stroke:#333,stroke-width:1px;
    classDef hop2 fill:#9df,stroke:#333,stroke-width:1px;
    classDef pruned fill:#eee,stroke:#ccc,color:#aaa;
```

### Algoritmus Stepei:

1. **Magcsomopontok (Seed Nodes) beallitasa**:
   - A feladat `write_set` es `read_set` fajljaihoz tartozo `FileNode`, `ClassNode` es `FunctionNode` elemek.
2. **Iranyitott Graf-Bejaras ($k$-hop traversal)**:
   - $k=1$ sugaru bejaras a kimeno `IMPORTS`, `USES_TYPE`, `EXTENDS` eleken.
   - $k=1$ sugaru bejaras a bemeno `CALLS` eleken (kik hivjak ezt a fuggvenyt).
3. **Kod-vaz kinyerese (Skeleton Extraction)**:
   - A $k$-hop grfban szereplo csomopontokbol a rendszer kiszuri a fuggvenytorzseket (beepitett implementacio), es csak a vazat (interfesz szignatura) tartja meg.

---

## 3. Skeleton Extractor (AST Kodtomorites)

A **Skeleton Extractor** a kinyert subgrapban levo fajlokbol eldobja az algoritmusok belso kodjat, 80-90%-os token-megtakaritast erve el:

### Eredeti Kod (100% Token):
```typescript
export class UserRepository {
  private db: DatabaseConnection;

  constructor(db: DatabaseConnection) {
    this.db = db;
    this.db.connect();
  }

  public async findById(id: string): Promise<User | null> {
    console.log("Fetching user from database with id:", id);
    const query = "SELECT * FROM users WHERE id = ?";
    const result = await this.db.query(query, [id]);
    if (!result || result.length === 0) return null;
    return new User(result[0]);
  }
}
```

### Generalt Skeleton Stub (15% Token):
```typescript
// SKELETON STUB FOR DEPENDENCY: src/repositories/UserRepository.ts
// DO NOT MODIFY THIS FILE. FOR INTERFACE REFERENCE ONLY.

export class UserRepository {
  constructor(db: DatabaseConnection);
  public async findById(id: string): Promise<User | null>;
}
```

---

## 4. Python Implementacios Blueprint (NetworkX + Skeleton Generator)

Az alabbi Python modul tartalmazza a teljes tudasgraf epitest, a $k$-hop subgraph kinyerest es a kontextus csomagolot:

```python
import networkx as nx
from typing import List, Set, Dict, Any

class KnowledgeEngine:
    def __init__(self):
        # Iranyitott multi-graf a kodbazis entitasainak
        self.graph = nx.DiGraph()

    def add_file_node(self, filepath: str, language: str):
        self.graph.add_node(filepath, type="FileNode", language=language)

    def add_symbol_node(self, fqn: str, symbol_type: str, stub_code: str, file_path: str):
        self.graph.add_node(fqn, type=symbol_type, stub=stub_code, file=file_path)
        self.graph.add_edge(file_path, fqn, relation="CONTAINS")

    def add_dependency(self, source_fqn: str, target_fqn: str, relation_type: str):
        self.graph.add_edge(source_fqn, target_fqn, relation=relation_type)

    def extract_context_subgraph(self, target_files: List[str], max_hops: int = 2) -> str:
        """
        Kiszamitja a k-hop szomszedsagi grafot es generates a tomoritett Context Cache-t.
        """
        extracted_nodes: Set[str] = set()

        for target_file in target_files:
            if target_file not in self.graph:
                continue
            
            # Ego graph kinyerese k-hop tavolsagra
            subgraph = nx.ego_graph(self.graph, target_file, radius=max_hops, undirected=False)
            extracted_nodes.update(subgraph.nodes())

        # Tomoritett kontextus szoveg generation
        context_blocks: List[str] = []
        visited_files: Set[str] = set()

        for node_id in extracted_nodes:
            node_data = self.graph.nodes[node_id]
            node_type = node_data.get("type")

            if node_type == "FileNode":
                visited_files.add(node_id)
            elif "stub" in node_data and node_data.get("file") in visited_files:
                context_blocks.append(f"// Symbol: {node_id}\n{node_data['stub']}\n")

        header = f"=== COMPRESSED CONTEXT CACHE ({len(context_blocks)} SYMBOLS, k={max_hops}) ===\n"
        return header + "\n".join(context_blocks)

    def invalidate_file(self, filepath: str):
        """
        Esemenyvezerelt ervenytelenites: torli a fajlhoz tartozo szimbolumokat es eleket.
        """
        if filepath in self.graph:
            # Eltavolitjuk a fajl altal tartalmazott szimbolumokat
            contained_nodes = [
                target for _, target, data in self.graph.out_edges(filepath, data=True)
                if data.get("relation") == "CONTAINS"
            ]
            for node in contained_nodes:
                self.graph.remove_node(node)
            self.graph.remove_node(filepath)
```
