# 09. Git Worktree & Merge/Rebase Staging Engine Spec

This document is the **AI-OS Git Worktree & Merge Staging Engine** melyszintu specification. Kidolgozza a parhuzamos agensek fajlrendszer-izolaciojat, a Git Worktree dinamikus eletciklusat, az aszinkron Merge Queue operation, a Rebase/Pre-merge re-validaciot, valamint az automata Git-konfliktus feloldast.

---

## 1. Architektura es Izolacios Koncepcio

Az AI-OS-ben **minden aktiv agens egy teljesen kulonallo Git Worktree mappaban dolgozik**. A Worktree egy virtualis Git elagazas a gazdagep fajlrendszeren, amely megosztja a meglevo `.git` objektum-adatbazist, igy **nem igenyel tarhely-masolast** es ezredmasodpercek alatt letrejon.

```mermaid
graph TD
    MainRepo[Main Git Repository: /mnt/g/Projects/ai-os] -->|Shared .git Store| WT1[.ai-os/worktrees/TASK-101]
    MainRepo -->|Shared .git Store| WT2[.ai-os/worktrees/TASK-102]
    MainRepo -->|Shared .git Store| WT3[.ai-os/worktrees/TASK-103]

    subgraph Parallel Execution Isolation
        WT1 --> Agent1[Agent 1: Modifies UserController.ts]
        WT2 --> Agent2[Agent 2: Modifies ProductService.ts]
        WT3 --> Agent3[Agent 3: Modifies OrderRepository.ts]
    end

    Agent1 -->|Commit & Validate| MergeQueue[Aszinkron Git Merge Queue]
    Agent2 -->|Commit & Validate| MergeQueue
    Agent3 -->|Commit & Validate| MergeQueue

    MergeQueue -->|Atomic Merge / Fast-Forward| MainBranch[Main Branch / Target Branch]
```

---

## 2. A Git Worktree Eletciklusa (Lifecycle States)

Minden feladat (Task) alatt a Worktree az alabbi allapot-atmeneteken megy keresztul:

```mermaid
stateDiagram-v2
    [*] --> WorktreeCreated: git worktree add
    WorktreeCreated --> CodePatching: propose_file_patch
    CodePatching --> ContainerValidation: trigger_sandbox_validation
    
    state ContainerValidation {
        [*] --> RunningTests
        RunningTests --> ValidatedOK: Pass (Exit 0)
        RunningTests --> ValidationFailed: Fail (Exit 1)
    }

    ValidationFailed --> CodePatching: Feedback Loop (Retry < N)
    ValidationFailed --> BlockedHITL: Retry >= N

    ValidatedOK --> RebaseCheck: Push to Merge Queue
    RebaseCheck --> Revalidating: Main Branch Moved Ahead
    Revalidating --> GitCommitAndMerge: Pass
    RebaseCheck --> GitCommitAndMerge: Main Unchanged

    GitCommitAndMerge --> WorktreeCleanup: git worktree remove
    WorktreeCleanup --> [*]
```

---

## 3. Aszinkron Git Merge Queue & Rebase Staging Strategy

Amikor tobb agens fut parhuzamosan (pl. `TASK-101` es `TASK-102`), elofordulhat, hogy `TASK-101` elobb fejezi be a munkat es modositja a `main` agat.

### A Staging Engine Rebase & Re-validation Szabalya:

1. **`TASK-101` befejezi a munkat**: Atmegy a teszteken ➔ beolvad a `main` agba (`git merge --ff-only`).
2. **`TASK-102` befejezi a munkat**: Mielott a `main`-be olvadna, a Staging Engine eszleli, hogy a `main` ag elorelepett!
3. **Automata Rebase & Ujravalidacio**:
   - A Staging Engine vegrehajt egy `git rebase main` parancsot a `TASK-102` worktree-jeben.
   - **Ujra-futtatja a Docker konteneres tesztet** a frissitett kodallapoton.
   - Ha a teszt lefut, a `TASK-102` biztonsagosan beolvad a `main` agba.

---

## 4. Git Merge Konfliktus Kezelo Engine (Conflict Resolution)

Ha a Rebase soran klasszikus Git merge konfliktus keletkezik (pl. `<<<<<<< HEAD` jelek a fajlban):

```mermaid
graph TD
    Conflict[Git Rebase Conflict Detected] --> ExtractMarkers[Conflict Marker & Diff Extractor]
    ExtractMarkers --> CreateConflictTask[Automata TASK-CONFLICT Generalas]
    CreateConflictTask --> AssignLLM[LLM Agent Assignment (High Risk)]
    AssignLLM -->|Fix Conflict & Commit| Revalidate[Docker Container Re-validation]
    Revalidate -->|Success| FinalMerge[Merge to Main]
```

### Konfliktus Feloldo Prompt Sablon (Automata):
```markdown
[GIT MERGE CONFLICT DETECTED]
A git merge conflict occurred while rebasing Task TASK-102 onto main branch.

Conflicting File: src/controllers/UserController.ts
Conflict Content:
<<<<<<< HEAD (Main Branch State)
export async function getUser(id: string): Promise<UserResponse> {
=======
export async function getUser(userId: string): Promise<User> {
>>>>>>> feature/TASK-102 (Agent Change)

Instructions:
Resolve the conflict above by unifying the function signature, keeping interface compatibility.
Use the `propose_file_patch` tool to save the resolved file.
```

---

## 5. Python Implementacios Blueprint (GitStagingEngine)

Az alabbi Python osztaly kezeli a Worktree staging-et, a rebase felugyeletet es a biztonsagos merge-et:

```python
import subprocess
import asyncio
from pathlib import Path
from typing import Dict, Any, Optional

class GitStagingEngine:
    def __init__(self, repo_root: str):
        self.repo_root = Path(repo_root)
        self.worktrees_dir = self.repo_root / ".ai-os" / "worktrees"
        self._merge_lock = asyncio.Lock()

    def create_worktree(self, task_id: str, base_branch: str = "main") -> Path:
        """Determinisztikus Git Worktree letrehozasa."""
        wt_path = self.worktrees_dir / task_id
        branch_name = f"ai-os/{task_id}"
        
        cmd = ["git", "worktree", "add", "-b", branch_name, str(wt_path), base_branch]
        res = subprocess.run(cmd, cwd=self.repo_root, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Git Worktree krealas sikertelen: {res.stderr}")
        return wt_path

    async def stage_and_merge_task(self, task_id: str, commit_message: str, validator_callback) -> bool:
        """
        Zarolt es atomi Merge Queue folyamat: Rebase -> Re-validate -> Merge -> Cleanup
        """
        wt_path = self.worktrees_dir / task_id
        branch_name = f"ai-os/{task_id}"

        async with self._merge_lock:  # Garantalja, hogy egyszerre csak 1 feladat merge-el a main-be
            # 1. Git Commit a Worktree-ben
            subprocess.run(["git", "add", "."], cwd=wt_path, check=True)
            subprocess.run(["git", "commit", "-m", commit_message], cwd=wt_path, check=True)

            # 2. Rebase a legfrissebb main-re
            rebase_res = subprocess.run(["git", "rebase", "main"], cwd=wt_path, capture_output=True, text=True)
            if rebase_res.returncode != 0:
                # Merge konfliktus tortent! Megallitjuk a rebase-t az automata feloldasig.
                subprocess.run(["git", "rebase", "--abort"], cwd=wt_path)
                return False

            # 3. Ujravalidalas Docker kontenerben a frissitett rebase kodallapoton
            is_valid = await validator_callback(wt_path)
            if not is_valid:
                return False

            # 4. Biztonsagos Merge a fo agba (Fast-Forward)
            subprocess.run(["git", "checkout", "main"], cwd=self.repo_root, check=True)
            subprocess.run(["git", "merge", "--ff-only", branch_name], cwd=self.repo_root, check=True)

            # 5. Worktree es ideiglenes ag torlese
            self.cleanup_worktree(task_id, branch_name)
            return True

    def cleanup_worktree(self, task_id: str, branch_name: str):
        """Torli a hasznalt worktree-t es agat."""
        wt_path = self.worktrees_dir / task_id
        subprocess.run(["git", "worktree", "remove", "--force", str(wt_path)], cwd=self.repo_root, capture_output=True)
        subprocess.run(["git", "branch", "-D", branch_name], cwd=self.repo_root, capture_output=True)
```
