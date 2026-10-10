"""Bring your own model. One function, ask(prompt) -> text, behind a provider picked by WEND_MODEL:

  WEND_MODEL=anthropic:claude-opus-5-5         Anthropic Messages API. Key: WEND_API_KEY or ANTHROPIC_API_KEY.
                                               WEND_EFFORT (default medium) sets the effort level.
  WEND_MODEL=openai:<model>                    Any OpenAI-compatible /chat/completions endpoint: OpenAI, OpenRouter,
                                               vLLM, LM Studio, Ollama (WEND_BASE_URL=http://localhost:11434/v1).
                                               Key: WEND_API_KEY or OPENAI_API_KEY (none needed for local servers).
  WEND_MODEL=agent[:DIR]                       YOU are the model. The harness writes each question to
                                               DIR/ask-N.md (default .wend/agent) and waits for DIR/answer-N.json.
                                               For coding agents (Claude Code, Codex, Aider...) driving the repo.

The model's identity goes into each fix's meta.author and the run's provenance. Standard library only.
"""
import json
import os
import time
import urllib.error
import urllib.request


def _post(url, body, headers, timeout=600, retries=4):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"content-type": "application/json", **headers})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if (e.code == 429 or e.code >= 500) and attempt < retries:
                time.sleep(2 ** attempt * 2)
                continue
            raise RuntimeError(f"HTTP {e.code} from {url}: {e.read().decode(errors='replace')[:500]}")
        except (urllib.error.URLError, TimeoutError):
            if attempt < retries:
                time.sleep(2 ** attempt * 2)
                continue
            raise


def _key(*names):
    return next((os.environ[n] for n in names if os.environ.get(n)), None)


class Anthropic:
    def __init__(self, model):
        self.model, self.effort = model, os.environ.get("WEND_EFFORT", "medium")
        self.key = _key("WEND_API_KEY", "ANTHROPIC_API_KEY")
        if not self.key:
            raise SystemExit("WEND_MODEL=anthropic:... needs WEND_API_KEY (or ANTHROPIC_API_KEY)")
        self.label = f"anthropic:{model} (effort {self.effort})"
        self.usage = {"input_tokens": 0, "output_tokens": 0}

    def ask(self, prompt):
        r = _post("https://api.anthropic.com/v1/messages",
                  {"model": self.model, "max_tokens": 32000, "output_config": {"effort": self.effort},
                   "messages": [{"role": "user", "content": prompt}]},
                  {"x-api-key": self.key, "anthropic-version": "2023-06-01"})
        for k in self.usage:
            self.usage[k] += r.get("usage", {}).get(k, 0)
        return "".join(b.get("text", "") for b in r["content"] if b["type"] == "text")


class OpenAICompatible:
    def __init__(self, model):
        self.model = model
        self.base = os.environ.get("WEND_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.key = _key("WEND_API_KEY", "OPENAI_API_KEY")
        self.label = f"openai-compatible:{model} @ {self.base.split('//')[-1].split('/')[0]}"
        self.usage = {"input_tokens": 0, "output_tokens": 0}

    def ask(self, prompt):
        r = _post(f"{self.base}/chat/completions",
                  {"model": self.model, "messages": [{"role": "user", "content": prompt}]},
                  {"authorization": f"Bearer {self.key}"} if self.key else {})
        u = r.get("usage", {})
        self.usage["input_tokens"] += u.get("prompt_tokens", 0)
        self.usage["output_tokens"] += u.get("completion_tokens", 0)
        return r["choices"][0]["message"]["content"]


class Agent:
    """The calling agent answers. Each question is a self-contained markdown file; the answer is a JSON file."""

    def __init__(self, d):
        self.dir = d or ".wend/agent"
        os.makedirs(self.dir, exist_ok=True)
        self.n = len([f for f in os.listdir(self.dir) if f.startswith("ask-")])
        self.label = f"agent ({os.environ.get('WEND_AGENT_NAME', 'unnamed agent; set WEND_AGENT_NAME')})"
        self.usage = {}

    def ask(self, prompt):
        self.n += 1
        q, a = f"{self.dir}/ask-{self.n}.md", f"{self.dir}/answer-{self.n}.json"
        open(q, "w").write(prompt)
        print(f"WEND AGENT QUESTION: read {q} and write your JSON answer to {a}", flush=True)
        deadline = time.time() + float(os.environ.get("WEND_AGENT_TIMEOUT", "3600"))
        while time.time() < deadline:
            if os.path.exists(a):
                time.sleep(1)  # let the writer finish
                return open(a).read()
            time.sleep(3)
        raise RuntimeError(f"no answer at {a} within the timeout")


def from_env():
    spec = os.environ.get("WEND_MODEL", "")
    kind, _, arg = spec.partition(":")
    if kind == "anthropic":
        return Anthropic(arg or "claude-opus-5-5")
    if kind == "openai":
        if not arg:
            raise SystemExit("WEND_MODEL=openai:<model name>")
        return OpenAICompatible(arg)
    if kind == "agent":
        return Agent(arg)
    raise SystemExit("set WEND_MODEL: anthropic:<model>, openai:<model> (with WEND_BASE_URL for other servers), "
                     "or agent[:dir] if you, the agent, will answer. See AGENTS.md.")
