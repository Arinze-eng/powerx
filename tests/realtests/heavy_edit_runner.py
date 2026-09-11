"""Heavy real-model test: multi-file read + edit on a real 3rd-party repo.

Drives the REAL NVIDIA model (nemotron-3-super) through AgentRunner with REAL
local file tools (read/grep/exec/write) + REAL RunPlanTool, and counts every
actual provider round-trip. Detects when steps fail silently, when edits don't
land, and how many calls a real multi-file editing task costs.

Task: in a clone of psf/requests, find a function and make a real edit, verify it.
"""
from __future__ import annotations
import argparse, asyncio, os, re, subprocess, sys
from pathlib import Path
from typing import Any
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from nanobot.agent.runner import AgentRunner
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.run_plan import RunPlanTool
from nanobot.providers.openai_compat_provider import OpenAICompatProvider
from tests.agent.runner_helpers import make_run_spec

class RealTool(Tool):
    def __init__(self, root): self.root = Path(root)
    def _p(self, path):
        p = Path(str(path).strip("\"'"))
        if not p.is_absolute(): p = self.root / p
        return p

class ReadTool(RealTool):
    @property
    def name(self): return "read_file"
    @property
    def description(self): return "Read a file in the repo and return its contents. Use RELATIVE paths like requests/models.py (tool resolves against your cwd)."
    @property
    def parameters(self): return {"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}
    async def execute(self, path: str = "", **kw):
        p = self._p(path)
        if not p.exists(): return f"Error: no such file: {path}"
        data = p.read_text(errors="replace")
        return data[:30000]

class GrepTool(RealTool):
    @property
    def name(self): return "grep"
    @property
    def description(self): return "Search file contents for a regex, relative path optional. Returns matching file:line."
    @property
    def parameters(self): return {"type":"object","properties":{"pattern":{"type":"string"},"path":{"type":"string"}},"required":["pattern"]}
    async def execute(self, pattern: str = "", path: str = "", **kw):
        base = self._p(path) if path else self.root
        if base.is_file(): files=[base]
        else: files=[f for f in base.rglob("*.py") if ".git" not in str(f)]
        out=[]
        try: rx=re.compile(pattern)
        except Exception as e: return f"Error: bad regex {e}"
        for f in files:
            try:
                for i,line in enumerate(f.read_text(errors="replace").splitlines(),1):
                    if rx.search(line): out.append(f"{f.relative_to(self.root)}:{i}: {line[:120]}")
            except Exception: pass
        return "\n".join(out[:200]) or "no matches"

class WriteTool(RealTool):
    @property
    def name(self): return "write_file"
    @property
    def description(self): return "OVERWRITE a file with given content. Use RELATIVE path. Only use for small precise edits; prefer run_plan where possible."
    @property
    def parameters(self): return {"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"}},"required":["path","content"]}
    def __init__(self, root): super().__init__(root); self.writes=[]
    async def execute(self, path: str = "", content: str = "", **kw):
        p = self._p(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        self.writes.append(str(p))
        return f"wrote {len(content)} bytes to {path}"

class ExecTool(RealTool):
    @property
    def name(self): return "exec"
    @property
    def description(self): return "Run a shell command in your cwd (the repo root). Returns stdout+stderr. Prefer one command composing work with &&, or a run_plan loop, to minimize calls."
    @property
    def parameters(self): return {"type":"object","properties":{"command":{"type":"string"}},"required":["command"]}
    def __init__(self, root): super().__init__(root); self.commands=[]
    async def execute(self, command: str = "", **kw):
        self.commands.append(command)
        try:
            proc = subprocess.run(command, shell=True, cwd=self.root, capture_output=True, text=True, timeout=90)
            out=(proc.stdout+proc.stderr).strip()
            return (out if out else f"(exit {proc.returncode})") + f"\n[exit_code={proc.returncode}]"
        except Exception as e:
            return f"Error: {e}"

TASKS = {
 "edit": (
   "Task: REAL edit a real existing function. Read src/requests/utils.py and locate "
   "the function 'prepend_scheme_if_needed'. Add a one-line docstring to ONLY that "
   "function using write_file. Then run exec 'git diff --stat' and report the diff stat "
   "so we prove the edit landed on disk. IMPORTANT: actually call write_file with the "
   "FULL updated file content — do not just plan it. Use at most a couple of read/tool "
   "calls, then write, then verify. Report what changed and the git diff stat."
 ),
 "planedit": (
   "Do the ENTIRE task in ONE run_plan tool call to save model calls: "
   "1) exec 'grep -n prepend_scheme_if_needed src/requests/utils.py' (id=locate) "
   "2) exec 'sed -n 1038,1045p src/requests/utils.py' (id=view) "
   "3) exec 'python3 -c \"import pathlib,re; p='src/requests/utils.py'; s=pathlib.Path(p).read_text(); "
   "s=re.sub(r'(def prepend_scheme_if_needed.*?):\\\\s+\\\"\\\"\\\".*?\\\"\\\"\\\"', "
   "r'\\\\1: \\\"\\\"\\\"Guard.\\\"\\\"\\\"', s, count=1, flags=re.S); pathlib.Path(p).write_text(s); "
   "print(\\\"edited\\\")\"' (id=edit) "
   "4) exec 'git diff --stat' (id=verify) as the final step. "
   "Return the run_plan's 'output' summarized. Do NOT make any other tool calls — "
   "just call run_plan once with all four steps."
 )
}

class Count(OpenAICompatProvider):
    def __init__(self, **kw):
        super().__init__(**kw); self.n=0; self.log=[]; self.errs=[]
    async def chat(self, messages=None, tools=None, **kwargs):
        self.n+=1
        try:
            r=await super().chat(messages=messages, tools=tools, **kwargs)
        except Exception as e:
            self.errs.append((self.n, repr(e)[:160]))
            raise
        names=[t.name for t in r.tool_calls] if r.tool_calls else []
        args=[t.arguments for t in r.tool_calls] if r.tool_calls else []
        self.log.append((self.n, r.finish_reason, names, args))
        return r

async def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--key",default="")
    ap.add_argument("--repo",default=str(Path(__file__).parents[1]/"third_repo")); ap.add_argument("--iter",type=int,default=20)
    ap.add_argument("--task",default="edit"); ap.add_argument("--base",default="https://integrate.api.nvidia.com/v1"); ap.add_argument("--model",default="nvidia/nemotron-3-super-120b-a12b")
    a=ap.parse_args()
    key = a.key or os.environ.get("NV_KEY") or os.environ.get("API_KEY","")
    if not key: raise SystemExit("set --key or NV_KEY/API_KEY env")
    prov=Count(api_key=key, api_base=a.base, default_model=a.model)
    reg=ToolRegistry()
    root=Path(a.repo)
    rd=ReadTool(root); gr=GrepTool(root); wr=WriteTool(root); ex=ExecTool(root)
    for t in (rd,gr,wr,ex): reg.register(t)
    pt=RunPlanTool(); pt.bind_registry(reg); reg.register(pt)
    spec=make_run_spec(prov, initial_messages=[{"role":"user","content":TASKS[a.task]}],
        model=a.model, tools=reg, max_iterations=a.iter,
        max_tool_result_chars=15000, workspace=str(root))
    print("TOOLS:", reg.tool_names, "| TASK:", a.task, "| MODEL:", a.model, "| base:", a.base)
    res=await AgentRunner().run(spec)
    print("\n==== HEAVY TEST RESULT ====")
    print("stop:", res.stop_reason, "| error:", res.error, "| CALLS:", prov.n)
    if prov.errs:
        print("PROVIDER ERRORS:", len(prov.errs))
        for n,e in prov.errs[:10]: print(f"   call{n}: {e}")
    print("final:", (res.final_content or "")[:800])
    for n,fr,names,args in prov.log:
        print(f"  call{n} finish={fr} tools={names}")
        for ar in args: print("      args:", str(ar)[:200])
    print("writes:", wr.writes)
    print("exec cmds:", len(ex.commands))
    for c in ex.commands: print("   exec:", c[:160])

if __name__=="__main__":
    asyncio.run(main())