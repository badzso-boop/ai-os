# 07. MCP Server & Orchestrator Engine Architecture

This document is the **AI-OS Orchestrator Kernel** and the **MCP (Model Context Protocol) Adapter & Server Engine** megvalositasi szintu specifikacioja. Tartalmazza a determinisztikus rendszermag operation, az UML / Mermaid architektura-diagramokat, a pontos JSON-RPC MCP parancssemakat and the fejleszteshez szukseges Python kod-blueprintet.

---

## 1. Architektura Koncepcio: Kernel vs. AI Vegrehajto Magok

Az AI-OS operation a szamitogepes operacios rendszerek (OS Kernel) mintajara epul:

- **Orchestrator Core (Ring 0 - OS Kernel)**: **100%-ig determinisztikus Python 3.12+ algoritmus** (`asyncio`). responsible az allapotgepekert, a fuggosegi grafokert (DAG), a fajlzarolasokert, az MCP szerver/kliens protokoll kommunikacioert and the Git Worktree / Docker sandbox felugyeleteert. **0 token koltsegu**, nem teveszt, nem hallucinal.
- **AI Executing Cores (Ring 3 - User Space Processes)**: A felcserelheto LLM-ek (Claude, OpenAI, Gemini, DeepSeek). Kizarolag az Orchestrator altal atadott korlatozott kontextussal es MCP eszkozokkel dolgoznak.

```mermaid
graph TD
    User([Fejleszto / Admin]) -->|1. High-level Task / Epic| OrchestratorKernel

    subgraph OrchestratorKernel [AI-OS Orchestrator Kernel (Deterministic Python Engine)]
        DAGPlanner[1. DAG Planner & Cycle Checker]
        LockManager[2. Granular Lock Manager]
        WorktreeMgr[3. Git Worktree Manager]
        MCPServer[4. Internal MCP Server & Router]
        SandboxMgr[5. Ephemeral Docker Sandbox Engine]
        
        DAGPlanner --> LockManager
        LockManager --> WorktreeMgr
        WorktreeMgr --> MCPServer
        MCPServer <--> SandboxMgr
    end

    subgraph ExecutingCores [AI Executing Cores (User Space LLMs)]
        ClaudeCore[Claude 3.5 Sonnet - High Risk]
        GeminiCore[Gemini 1.5 Flash - Low Risk]
        LocalCore[DeepSeek-R1 / Ollama - Offline]
    end

    MCPServer <-->|MCP JSON-RPC Protocol| ClaudeCore
    MCPServer <-->|MCP JSON-RPC Protocol| GeminiCore
    MCPServer <-->|MCP JSON-RPC Protocol| LocalCore
```

---

## 2. UML / Mermaid Szekvencia Diagram: Kodmodositas Eletciklusa

Az alabbi diagram szemlelteti, hogy egy agens hogyan hoz letre vagy modosit egy fajlt a rendszerben anelkul, hogy kozvetlen operacios rendszer hozzaferese lenne:

```mermaid
sequenceDiagram
    autonumber
    participant Kernel as Orchestrator Core (Python)
    participant Lock as Lock Manager
    participant LLM as LLM Agent (via MCP Client)
    participant WT as Git Worktree (.ai-os/worktrees/TASK-102)
    participant Docker as Ephemeral Docker Sandbox

    Kernel->>Lock: Acquire Write-Lock ("src/utils/validator.ts")
    Lock-->>Kernel: Lock Granted
    Kernel->>LLM: MCP Task Execution Request (Context + Tools)
    
    note over LLM: Agens feldolgozza a Context Cache-t
    LLM->>Kernel: MCP Tool Call: propose_file_patch(filepath, content, is_new_file)
    
    note over Kernel: Zarolas es biztonsagi ellenorzes
    Kernel->>WT: Fajl irasa / javitasa a kijelolt Worktree-ben
    WT-->>Kernel: File Written OK
    Kernel-->>LLM: MCP Tool Response: {status: "PATCH_APPLIED_TO_WORKTREE"}
    
    LLM->>Kernel: MCP Tool Call: trigger_sandbox_validation()
    Kernel->>Docker: Kontener inditasa + Mount Worktree Read-Only
    Docker->>Docker: npm test / pytest / tsc compilation
    Docker-->>Kernel: Exit Code: 0 (Success)
    Kernel-->>LLM: MCP Tool Response: {success: true, output: "Tests Passed"}
    
    Kernel->>WT: Git Commit & Merge to Main Branch
    Kernel->>Lock: Release Write-Lock
```

---

## 3. MCP Eszkozok (Tools) Pontos JSON-RPC Semaja

Minden agens kizarolag az alabbi 3 szabvanyos MCP eszkozt hivhatja meg JSON-RPC uzenetekkel.

### 3.1. `propose_file_patch` (Kod letrehozasa vagy modositasa)
Ezzel az eszkozzel az agens uj fajlt hoz letre vagy egy meglevot modosit a sajat Git Worktree-jeben.

#### JSON-RPC Keres (LLM -> Orchestrator):
```json
{
  "jsonrpc": "2.0",
  "id": 42,
  "method": "tools/call",
  "params": {
    "name": "propose_file_patch",
    "arguments": {
      "filepath": "src/utils/validator.ts",
      "is_new_file": true,
      "content": "export function validateEmail(email: string): boolean {\n  const re = /^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$/;\n  return re.test(email);\n}\n"
    }
  }
}
```

#### JSON-RPC Valasz (Orchestrator -> LLM):
```json
{
  "jsonrpc": "2.0",
  "id": 42,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "SUCCESS: Patch applied to worktree isolated environment at .ai-os/worktrees/TASK-102/src/utils/validator.ts"
      }
    ],
    "isError": false
  }
}
```

---

### 3.2. `fetch_symbol_definition` (Szimbolum reszletek kerese)
A kontextusablak kimelese erdekeben az agens ezzel keri le egy olyan osztaly/fuggveny teljes torzset, amely nem volt benne az alap Context Cache-ben.

#### JSON-RPC Keres:
```json
{
  "jsonrpc": "2.0",
  "id": 43,
  "method": "tools/call",
  "params": {
    "name": "fetch_symbol_definition",
    "arguments": {
      "symbol_id": "src/services/UserService.ts::UserRepository"
    }
  }
}
```

---

### 3.3. `trigger_sandbox_validation` (Konteneres validacio inditasa)
Elinditja az eldobhato Docker konteneres tesztet es visszacsatolja az eredmenylogot.

#### JSON-RPC Keres:
```json
{
  "jsonrpc": "2.0",
  "id": 44,
  "method": "tools/call",
  "params": {
    "name": "trigger_sandbox_validation",
    "arguments": {}
  }
}
```

#### JSON-RPC Valasz (Validation Passed):
```json
{
  "jsonrpc": "2.0",
  "id": 44,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "VALIDATION PASSED\nExit Code: 0\nOutput:\n  tsc: 0 errors\n  pytest: 12 passed in 0.42s"
      }
    ],
    "isError": false
  }
}
```

---

## 4. Python Implementacios Blueprint (Orchestrator MCP Core)

Az alabbi kod bemutatja az Orchestrator MCP szerver es Git Worktree kezelojenek mukodokepes vazat:

```python
import asyncio
import os
import subprocess
from pathlib import Path
from typing import Dict, Any

class OrchestratorMCPBridge:
    def __init__(self, task_id: str, project_root: str):
        self.task_id = task_id
        self.project_root = Path(project_root)
        self.worktree_path = self.project_root / ".ai-os" / "worktrees" / task_id

    def setup_worktree(self, base_branch: str = "main"):
        """Letrehoz egy izolalt Git Worktree-t a feladatnak determinisztikusan."""
        self.worktree_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "git", "worktree", "add", "-b", f"feature/{self.task_id}",
            str(self.worktree_path), base_branch
        ]
        subprocess.run(cmd, cwd=self.project_root, check=True, capture_output=True)

    async def handle_propose_file_patch(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Kod irasa / javitasa a szeparalt worktree-ben (NEM a fo kodbazisban!)."""
        rel_filepath = args.get("filepath")
        content = args.get("content")
        
        target_file = self.worktree_path / rel_filepath
        target_file.parent.mkdir(parents=True, exist_ok=True)
        
        # Fajl irasa a Worktree-be
        with open(target_file, "w", encoding="utf-8") as f:
            f.write(content)
            
        return {
            "status": "SUCCESS",
            "message": f"File {rel_filepath} patched in isolated worktree {self.task_id}."
        }

    async def handle_trigger_sandbox_validation(self) -> Dict[str, Any]:
        """Docker container inditasa a modositott worktree validalasara."""
        # Docker run command read-only mount-tal
        cmd = [
            "docker", "run", "--rm",
            "-v", f"{self.worktree_path}:/app:ro",
            "--net", "none",  # Izolalt halozat
            "node:20-alpine", "npm", "test"
        ]
        
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        
        return {
            "exit_code": proc.returncode,
            "success": proc.returncode == 0,
            "logs": stdout.decode() if proc.returncode == 0 else stderr.decode()
        }

    def cleanup_worktree(self):
        """Torli a worktree-t a sikeres merge utan."""
        cmd = ["git", "worktree", "remove", "--force", str(self.worktree_path)]
        subprocess.run(cmd, cwd=self.project_root, capture_output=True)
```
