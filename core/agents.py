"""
AutoResearcher Agent Dispatcher

Leader-Worker architecture for efficient token usage:
- Leader: Central decision-maker, persistent conversation within a cycle
- Workers: Specialized agents (idea/code/writing), spawned on demand

Only ONE worker runs at a time. Others idle at zero token cost.
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

# 阿里云百炼 OpenAI 兼容模式默认 endpoint（各地域可能不同，可用环境变量或 config 覆盖）
DEFAULT_DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_DASHSCOPE_MODEL = "qwen3.6-plus"
# 项目默认 LLM（无 config 或缺字段时的回退）
DEFAULT_AGENT_PROVIDER = "dashscope"
DEFAULT_AGENT_MODEL = DEFAULT_DASHSCOPE_MODEL

logger = logging.getLogger("autoresearcher.agents")


# Agent definitions directory
AGENTS_DIR = Path(__file__).parent.parent / "agents"


class AgentDispatcher:
    """Dispatches tasks to specialized agents.

    The Leader agent decides what to do, then dispatches to workers:
    - idea_agent: Literature search, hypothesis formation
    - code_agent: Experiment implementation and execution
    - writing_agent: Report generation and paper writing

    Each worker has a minimal tool set (3-5 tools) to reduce token overhead.
    """

    WORKER_CONFIGS = {
        "idea": {
            "prompt_file": "idea_agent.md",
            "max_turns": 12,
            "tools": ["search_papers", "get_paper", "write_file", "read_file"],
        },
        "code": {
            "prompt_file": "code_agent.md",
            "max_turns": 40,
            "tools": ["run_shell", "launch_experiment", "write_file", "read_file", "list_files"],
        },
        "writing": {
            "prompt_file": "writing_agent.md",
            "max_turns": 30,
            "tools": ["write_file", "read_file", "list_files"],
        },
    }

    # Model mapping between providers
    MODEL_MAP = {
        # Anthropic ↔ OpenAI equivalents
        "claude-sonnet-4-6": "codex-5.3",     # Fast tier
        "claude-opus-4-6": "gpt-5.4",          # Strongest tier
        "codex-5.3": "claude-sonnet-4-6",
        "gpt-5.4": "claude-opus-4-6",
    }

    # 使用 DashScope 时，若 model 仍为其它厂商的占位名，则落到默认 Qwen 模型
    DASHSCOPE_MODEL_ALIASES = {
        "claude-sonnet-4-6": DEFAULT_DASHSCOPE_MODEL,
        "claude-opus-4-6": DEFAULT_DASHSCOPE_MODEL,
        "codex-5.3": DEFAULT_DASHSCOPE_MODEL,
        "gpt-5.4": DEFAULT_DASHSCOPE_MODEL,
    }

    def __init__(
        self,
        model: str = DEFAULT_AGENT_MODEL,
        provider: str = DEFAULT_AGENT_PROVIDER,
        max_steps: int = 3,
        dashscope_base_url: Optional[str] = None,
    ):
        self.model = model
        self.provider = provider  # "anthropic" | "openai" | "dashscope"
        self.max_steps = max_steps
        self.dashscope_base_url = dashscope_base_url
        self._leader_history = []

    def dispatch_leader(self, task: str, context: dict) -> dict:
        """Send a task to the Leader agent.

        The Leader maintains conversation history within a cycle for
        coherent multi-step reasoning. History is cleared between cycles.

        Args:
            task: "think" or "reflect"
            context: Current state (brief, memory, results, etc.)

        Returns:
            Leader's decision as a dict
        """
        system_prompt = self._load_prompt("leader.md")

        messages = list(self._leader_history)
        messages.append({
            "role": "user",
            "content": self._format_leader_input(task, context),
        })

        response = self._call_llm(
            system=system_prompt,
            messages=messages,
            max_turns=10,
        )

        # Persist conversation for within-cycle coherence
        self._leader_history = messages + [{"role": "assistant", "content": response}]

        return self._parse_leader_response(response)

    def dispatch_worker(self, agent_type: str, task: str, tools: list) -> dict:
        """Dispatch a task to a worker agent.

        Workers are stateless — each dispatch is independent.
        This keeps token costs predictable.

        Args:
            agent_type: "idea", "code", or "writing"
            task: Task description from the Leader
            tools: Tool definitions to provide

        Returns:
            Worker's result as a dict
        """
        if agent_type not in self.WORKER_CONFIGS:
            raise ValueError(f"Unknown agent type: {agent_type}")

        config = self.WORKER_CONFIGS[agent_type]
        system_prompt = self._load_prompt(config["prompt_file"])

        logger.info(f"Dispatching {agent_type} agent: {task[:100]}...")

        response = self._call_llm(
            system=system_prompt,
            messages=[{"role": "user", "content": task}],
            tools=tools,
            max_turns=config["max_turns"],
        )

        result = self._parse_worker_response(response, agent_type)
        logger.info(f"Worker {agent_type} completed: {str(result)[:200]}")
        return result

    def reset_leader_history(self):
        """Clear leader conversation history between cycles."""
        self._leader_history = []

    def _call_llm(self, system: str, messages: list, tools: list = None, max_turns: int = 10) -> str:
        """Call the LLM API. Supports Anthropic, OpenAI, and DashScope (Qwen, OpenAI-compatible).

        Provider is determined by self.provider:
        - "anthropic": Uses Claude API with prompt caching
        - "openai": Uses OpenAI API (Codex 5.3 / GPT 5.4)
        - "dashscope": Uses 阿里云百炼 compatible-mode + DASHSCOPE_API_KEY
        """
        if self.provider == "openai":
            return self._call_openai(system, messages)
        if self.provider == "dashscope":
            return self._call_dashscope(system, messages)
        return self._call_anthropic(system, messages)

    def _call_anthropic(self, system: str, messages: list) -> str:
        """Call Anthropic Claude API."""
        try:
            import anthropic

            client = anthropic.Anthropic()

            api_messages = []
            for msg in messages:
                api_messages.append({
                    "role": msg["role"],
                    "content": msg["content"],
                })

            kwargs = {
                "model": self.model,
                "max_tokens": 4096,
                "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                "messages": api_messages,
            }

            response = client.messages.create(**kwargs)
            return response.content[0].text

        except ImportError:
            logger.warning("anthropic package not installed. Trying openai fallback.")
            return self._call_openai(system, messages)

    def _call_openai(self, system: str, messages: list) -> str:
        """Call OpenAI API (Codex 5.3 / GPT 5.4)."""
        try:
            import openai

            client = openai.OpenAI()

            # Map model name if it's an Anthropic model name
            model = self.MODEL_MAP.get(self.model, self.model) if self.provider != "openai" else self.model

            api_messages = [{"role": "system", "content": system}]
            for msg in messages:
                api_messages.append({
                    "role": msg["role"],
                    "content": msg["content"],
                })

            response = client.chat.completions.create(
                model=model,
                max_tokens=4096,
                messages=api_messages,
            )

            return response.choices[0].message.content

        except ImportError:
            logger.warning("openai package not installed. Using mock response.")
            return json.dumps({"action": "wait", "reason": "LLM not available"})

    def _resolve_dashscope_model(self) -> str:
        return self.DASHSCOPE_MODEL_ALIASES.get(self.model, self.model)

    def _call_dashscope(self, system: str, messages: list) -> str:
        """Call 阿里云百炼 OpenAI 兼容接口（如 qwen3.6-plus）。需要环境变量 DASHSCOPE_API_KEY。"""
        try:
            import openai
        except ImportError:
            logger.warning("openai package not installed. Using mock response.")
            return json.dumps({"action": "wait", "reason": "LLM not available"})

        api_key = os.getenv("DASHSCOPE_API_KEY")
        if not api_key:
            logger.warning("DASHSCOPE_API_KEY not set. Using mock response.")
            return json.dumps({"action": "wait", "reason": "DASHSCOPE_API_KEY not set"})

        base_url = (
            self.dashscope_base_url
            or os.getenv("DASHSCOPE_BASE_URL")
            or DEFAULT_DASHSCOPE_BASE_URL
        )
        client = openai.OpenAI(api_key=api_key, base_url=base_url)
        model = self._resolve_dashscope_model()

        api_messages = [{"role": "system", "content": system}]
        for msg in messages:
            api_messages.append({"role": msg["role"], "content": msg["content"]})

        response = client.chat.completions.create(
            model=model,
            max_tokens=4096,
            messages=api_messages,
        )
        content = response.choices[0].message.content
        return content if content is not None else ""

    def _load_prompt(self, filename: str) -> str:
        """Load agent prompt from agents/ directory."""
        prompt_path = AGENTS_DIR / filename
        if prompt_path.exists():
            return prompt_path.read_text()
        logger.warning(f"Prompt file not found: {prompt_path}")
        return f"You are the {filename.replace('.md', '')} agent."

    def _format_leader_input(self, task: str, context: dict) -> str:
        """Format context into a structured input for the Leader."""
        parts = [f"## Task: {task.upper()}\n"]

        if context.get("directive"):
            parts.append(f"## Human Directive (HIGHEST PRIORITY)\n{context['directive']}\n")

        parts.append(f"## Project Brief\n{context.get('brief', 'N/A')}\n")
        parts.append(f"## Memory Log\n{context.get('memory_log', 'N/A')}\n")
        parts.append(f"## Cycle: {context.get('cycle', 'N/A')}\n")

        if context.get("experiment_result"):
            parts.append(f"## Experiment Result\n{json.dumps(context['experiment_result'], indent=2)}\n")

        return "\n".join(parts)

    def _parse_leader_response(self, response: str) -> dict:
        """Parse Leader's response into structured action."""
        try:
            # Try to find JSON in response
            import re
            json_match = re.search(r"\{[^{}]*\}", response, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
        except (json.JSONDecodeError, AttributeError):
            pass

        # Fallback: extract action from text
        response_lower = response.lower()
        if "wait" in response_lower or "no experiment" in response_lower:
            return {"action": "wait", "reason": response[:200]}

        return {
            "action": "experiment",
            "agent": "code",
            "task": response,
        }

    def _parse_worker_response(self, response: str, agent_type: str) -> dict:
        """Parse worker response into structured result."""
        result = {"agent": agent_type, "response": response}

        # Check for experiment launch indicators
        if agent_type == "code":
            if "PID" in response or "launched" in response.lower():
                result["experiment_launched"] = True
                # Try to extract PID
                import re
                pid_match = re.search(r"PID[=:\s]+(\d+)", response)
                if pid_match:
                    result["pid"] = int(pid_match.group(1))

        return result
