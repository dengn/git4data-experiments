"""Pluggable LLM backend for the agent.

  ANTHROPIC_API_KEY set -> real Claude (tool use)
  OPENAI_API_KEY set    -> real OpenAI (function calling)
  neither               -> a deterministic local planner (runs offline, repeatable)

All backends share a stateful interface so the agent loop is backend-agnostic and
the real tool-use message threading stays correct:
    llm.begin(task)
    decision, usage = llm.step(prev_tool_record)   # prev=None on first call
`decision` is {"type":"tool_call","tool":..,"args":{..}} or {"type":"final","text":..}
`prev_tool_record` is {"tool","args","result","ok"} for the tool just executed.
"""
import os
import re

from .tools import TOOL_SCHEMAS


def make_llm():
    if os.getenv("ANTHROPIC_API_KEY"):
        return AnthropicLLM()
    if os.getenv("DEEPSEEK_API_KEY"):
        return DeepSeekLLM()
    if os.getenv("OPENAI_API_KEY"):
        return OpenAILLM()
    return LocalLLM()


# --------------------------- offline deterministic ---------------------------
class LocalLLM:
    model = "local-planner-v1"
    system = "local"

    def begin(self, task):
        self.task = task
        self._hist = []

    def step(self, prev):
        if prev is not None:
            self._hist.append(prev)
        return self._decide()

    def _decide(self):
        task, hist = self.task, self._hist
        tl = task.lower()
        done = {h["tool"] for h in hist}
        usage = {"in": 24 + 12 * len(hist) + len(task) // 4, "out": 16}

        if hist and not hist[-1]["ok"]:                       # a tool errored -> give up
            return {"type": "final",
                    "text": f"Sorry, I couldn't complete that ({hist[-1]['result']})."}, usage

        if "who wrote" in tl or "author of" in tl:            # author lookup
            if "kb_lookup" not in done:
                ent = re.split(r"who wrote|author of", tl)[-1].strip(" ?.")
                return {"type": "tool_call", "tool": "kb_lookup", "args": {"entity": ent}}, usage
            return {"type": "final", "text": str(hist[-1]["result"])}, usage

        m_pop = re.search(r"population of ([a-z ]+?)(?:,|\s+(?:doubled|times|x)\b|\?|$)", tl)
        if m_pop:                                             # population (+ doubled/times N)
            if "kb_lookup" not in done:
                return {"type": "tool_call", "tool": "kb_lookup",
                        "args": {"entity": m_pop.group(1).strip() + " population"}}, usage
            if "calculator" not in done:
                base = hist[-1]["result"]
                factor = 2 if "doubl" in tl else 1
                mt = re.search(r"times (\d+)", tl)
                if mt:
                    factor = int(mt.group(1))
                return {"type": "tool_call", "tool": "calculator",
                        "args": {"expression": f"{base} * {factor}"}}, usage
            return {"type": "final", "text": str(hist[-1]["result"])}, usage

        m_pct = re.search(r"(\d+(?:\.\d+)?)% of (.+?)(?:\(|\?|$)", tl)
        if m_pct:                                             # N% of <entity>
            if "kb_lookup" not in done:
                ent = m_pct.group(2).replace("the ", "").strip(" ?()")
                return {"type": "tool_call", "tool": "kb_lookup", "args": {"entity": ent}}, usage
            if "calculator" not in done:
                base = hist[-1]["result"]
                return {"type": "tool_call", "tool": "calculator",
                        "args": {"expression": f"{base} * {float(m_pct.group(1)) / 100}"}}, usage
            return {"type": "final", "text": str(hist[-1]["result"])}, usage

        m_ar = re.search(r"(-?\d+\.?\d*\s*[\+\-\*/]\s*-?\d+\.?\d*)", task)
        if m_ar:                                              # pure arithmetic
            if "calculator" not in done:
                return {"type": "tool_call", "tool": "calculator",
                        "args": {"expression": m_ar.group(1)}}, usage
            return {"type": "final", "text": str(hist[-1]["result"])}, usage

        if "kb_lookup" not in done:                           # fallback lookup (may error)
            ent = re.sub(r"[^a-z ]", "", tl).replace("what is", "").replace("population of", "").strip()
            return {"type": "tool_call", "tool": "kb_lookup", "args": {"entity": ent[:40]}}, usage
        return {"type": "final", "text": "I don't know."}, usage


_SYS = ("You are a careful assistant. Use the provided tools to look up facts and do "
        "arithmetic; do not guess numbers. When done, give a short final answer.")


# ------------------------------- real Claude --------------------------------
class AnthropicLLM:
    system = "anthropic"

    def __init__(self, model="claude-sonnet-4-5"):
        import anthropic
        self.client = anthropic.Anthropic()
        self.model = model
        self.tools = [{"name": t["name"], "description": t["description"],
                       "input_schema": t["parameters"]} for t in TOOL_SCHEMAS]

    def begin(self, task):
        self.msgs = [{"role": "user", "content": task}]
        self._last = None

    def step(self, prev):
        if prev is not None:
            self.msgs.append({"role": "assistant", "content": self._last["content"]})
            self.msgs.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": self._last["id"],
                 "content": str(prev["result"])}]})
        r = self.client.messages.create(model=self.model, max_tokens=512, system=_SYS,
                                        tools=self.tools, messages=self.msgs)
        usage = {"in": r.usage.input_tokens, "out": r.usage.output_tokens}
        for b in r.content:
            if b.type == "tool_use":
                self._last = {"id": b.id, "content": r.content}
                return {"type": "tool_call", "tool": b.name, "args": dict(b.input)}, usage
        text = "".join(b.text for b in r.content if b.type == "text")
        return {"type": "final", "text": text}, usage


# ------------------------------- real OpenAI --------------------------------
class OpenAILLM:
    system = "openai"

    def __init__(self, model="gpt-4o-mini"):
        import openai
        self.client = openai.OpenAI()
        self.model = model
        self.tools = [{"type": "function", "function": t} for t in TOOL_SCHEMAS]

    def begin(self, task):
        self.msgs = [{"role": "system", "content": _SYS}, {"role": "user", "content": task}]
        self._last = None

    def step(self, prev):
        import json
        if prev is not None:
            self.msgs.append(self._last)
            self.msgs.append({"role": "tool", "tool_call_id": self._last["tool_calls"][0]["id"],
                              "content": str(prev["result"])})
        r = self.client.chat.completions.create(model=self.model, messages=self.msgs, tools=self.tools)
        m = r.choices[0].message
        usage = {"in": r.usage.prompt_tokens, "out": r.usage.completion_tokens}
        if m.tool_calls:
            tc = m.tool_calls[0]
            self._last = {"role": "assistant", "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}]}
            return {"type": "tool_call", "tool": tc.function.name,
                    "args": json.loads(tc.function.arguments)}, usage
        return {"type": "final", "text": m.content or ""}, usage


# ------------------- real DeepSeek (OpenAI-compatible API) -------------------
class DeepSeekLLM(OpenAILLM):
    system = "deepseek"

    def __init__(self, model="deepseek-chat"):
        import openai
        self.client = openai.OpenAI(api_key=os.getenv("DEEPSEEK_API_KEY"),
                                    base_url="https://api.deepseek.com")
        self.model = model
        self.tools = [{"type": "function", "function": t} for t in TOOL_SCHEMAS]
