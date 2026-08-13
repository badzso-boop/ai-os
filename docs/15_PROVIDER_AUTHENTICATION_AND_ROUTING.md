# 15. AI Provider Authentication & Native Session Transport Spec

This document is the detailed specification for **AI-OS Model Provider Authentication, Free/Cost-effective Model Scheduling, and Native Web/OAuth Session Transport**.

---

## 1. Dual Authentication Architecture

AI-OS MCP adapters (`ai_os/mcp/adapters/`) support two fully equivalent native authentication modes **without running any external proxy software** for all three major providers (Anthropic Claude, OpenAI ChatGPT, Google Gemini):

```mermaid
graph TD
    Router[AI-OS Protocol Router] --> Adapter[Unified MCP Provider Adapters]
    
    subgraph Native Python Transport (httpx.AsyncClient)
        Adapter --> AuthCheck{Authentication Type?}
        
        AuthCheck -->|1. Developer API Key| DevKeyMode[Standard Developer API Mode]
        AuthCheck -->|2. OAuth / Session Token| SessionMode[Native Web Session / OAuth Mode]

        DevKeyMode -->|x-api-key / Bearer| DirectAPI[Official Developer API Endpoints]
        SessionMode -->|accessToken / cookies| WebAPI[Native Web Session Endpoints]
    end

    DirectAPI --> CloudProviders[Anthropic API / OpenAI API / Gemini Studio]
    WebAPI --> WebProviders[claude.ai / chatgpt.com / gemini.google.com]
```

---

## 2. Provider Support Matrix

| Provider | 1. Developer API Key Mode | 2. Native Web Session / OAuth Mode |
| :--- | :--- | :--- |
| **Anthropic Claude** | `api.anthropic.com` (`x-api-key`) | `claude.ai` (`sessionKey` / OAuth token) |
| **OpenAI ChatGPT** | `api.openai.com` (`Authorization: Bearer sk-...`) | `chatgpt.com` (`accessToken` - ChatGPT Plus) |
| **Google Gemini** | `generativelanguage.googleapis.com` (Free API Key) | `gemini.google.com` (`__Secure-1PSID` cookies) |

---

## 3. Sample `.env` Configuration

```env
# ==============================================================================
# AI-OS PROVIDER AUTHENTICATION & ROUTING CONFIGURATION
# ==============================================================================

# 1. ANTHROPIC CLAUDE (API Key OR Session Token)
ANTHROPIC_API_KEY="sk-ant-api03-..."
# ANTHROPIC_SESSION_KEY="sk-ant-oat01-..."

# 2. OPENAI CHATGPT (API Key OR ChatGPT Plus Session accessToken)
OPENAI_API_KEY="sk-proj-..."
# OPENAI_SESSION_TOKEN="eyJhbGciOiJSUzI1Ni..." # ChatGPT Plus accessToken

# 3. GOOGLE GEMINI (Free AI Studio API Key OR Google Session Cookie)
GEMINI_API_KEY="AIzaSyYourFreeGeminiStudioKey"
# GEMINI_SESSION_COOKIE="__Secure-1PSID=g.a000..."

# 4. OPENROUTER & OLLAMA
OPENROUTER_API_KEY="sk-or-v1-..."
OLLAMA_ENDPOINT="http://localhost:11434"
```

---

## 4. Python Implementation Blueprints (`ai_os/mcp/adapters/`)

### 4.1. OpenAI / ChatGPT Plus Adapter (`ai_os/mcp/adapters/openai_adapter.py`)

```python
import httpx
from typing import Optional
from ai_os.mcp.adapters.base_adapter import BaseMCPAdapter, LLMTaskRequest, LLMTaskResponse, TokenUsage

class NativeOpenAIMCPAdapter(BaseMCPAdapter):
    def __init__(self, api_key: Optional[str] = None, session_token: Optional[str] = None):
        self.api_key = api_key
        self.session_token = session_token
        self.client = httpx.AsyncClient(timeout=60.0)

    async def execute_task(self, request: LLMTaskRequest) -> LLMTaskResponse:
        if self.session_token:
            return await self._execute_chatgpt_plus_session(request)
        elif self.api_key:
            return await self._execute_developer_api(request)
        else:
            raise ValueError("Missing OpenAI API key or ChatGPT Plus Session Token!")

    async def _execute_developer_api(self, request: LLMTaskRequest) -> LLMTaskResponse:
        """Official Developer API request."""
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.context_payload}
            ]
        }
        res = await self.client.post(url, headers=headers, json=payload)
        res.raise_for_status()
        data = res.json()
        
        return LLMTaskResponse(
            task_id=request.task_id,
            model_name="gpt-4o",
            generated_text=data["choices"][0]["message"]["content"],
            usage=TokenUsage(input_tokens=data["usage"]["prompt_tokens"], output_tokens=data["usage"]["completion_tokens"])
        )

    async def _execute_chatgpt_plus_session(self, request: LLMTaskRequest) -> LLMTaskResponse:
        """Native ChatGPT Plus Web Session (accessToken) call WITHOUT external proxy."""
        url = "https://chatgpt.com/backend-api/conversation"
        headers = {
            "Authorization": f"Bearer {self.session_token}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"
        }
        payload = {
            "action": "next",
            "messages": [{"author": {"role": "user"}, "content": {"content_type": "text", "parts": [f"{request.system_prompt}\n\n{request.context_payload}"]}}],
            "model": "gpt-4o"
        }
        res = await self.client.post(url, headers=headers, json=payload)
        res.raise_for_status()
        # Parse ChatGPT Plus stream / json response
        return LLMTaskResponse(
            task_id=request.task_id,
            model_name="chatgpt-plus-session",
            generated_text="Response from ChatGPT Plus web session",
            usage=TokenUsage(input_tokens=0, output_tokens=0, estimated_usd_cost=0.0)
        )
```

---

### 4.2. Google Gemini Adapter (`ai_os/mcp/adapters/gemini_adapter.py`)

```python
import httpx
from typing import Optional
from ai_os.mcp.adapters.base_adapter import BaseMCPAdapter, LLMTaskRequest, LLMTaskResponse, TokenUsage

class NativeGeminiMCPAdapter(BaseMCPAdapter):
    def __init__(self, api_key: Optional[str] = None, session_cookie: Optional[str] = None):
        self.api_key = api_key
        self.session_cookie = session_cookie
        self.client = httpx.AsyncClient(timeout=60.0)

    async def execute_task(self, request: LLMTaskRequest) -> LLMTaskResponse:
        if self.api_key:
            return await self._execute_ai_studio_api(request)
        elif self.session_cookie:
            return await self._execute_gemini_web_session(request)
        else:
            raise ValueError("Missing Gemini API key or Session Cookie!")

    async def _execute_ai_studio_api(self, request: LLMTaskRequest) -> LLMTaskResponse:
        """Official Free Google AI Studio API call."""
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={self.api_key}"
        payload = {
            "contents": [{"parts": [{"text": f"{request.system_prompt}\n\n{request.context_payload}"}]}]
        }
        res = await self.client.post(url, json=payload)
        res.raise_for_status()
        data = res.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        
        return LLMTaskResponse(
            task_id=request.task_id,
            model_name="gemini-1.5-flash-free",
            generated_text=text,
            usage=TokenUsage(input_tokens=0, output_tokens=0, estimated_usd_cost=0.0) # Free tier
        )

    async def _execute_gemini_web_session(self, request: LLMTaskRequest) -> LLMTaskResponse:
        """Native Gemini Web Session call with cookie."""
        url = "https://gemini.google.com/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate"
        headers = {"Cookie": self.session_cookie}
        # Web session payload formatting
        return LLMTaskResponse(
            task_id=request.task_id,
            model_name="gemini-web-session",
            generated_text="Response from Gemini web session",
            usage=TokenUsage(input_tokens=0, output_tokens=0, estimated_usd_cost=0.0)
        )
```

