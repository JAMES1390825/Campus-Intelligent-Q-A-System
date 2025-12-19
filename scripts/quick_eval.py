from __future__ import annotations

from rich import print

from app.agents import AgentOrchestrator
from app.config import get_settings
from app.models import QueryRequest

QUESTIONS = [
    "请假超过7天需要找谁审批？",
    "报修流程是什么？",
    "申请助学金需要提交什么材料？",
]


def main():
    settings = get_settings()
    orchestrator = AgentOrchestrator(settings)

    for q in QUESTIONS:
        print(f"\n[bold yellow]Q:[/] {q}")
        resp = orchestrator.handle(QueryRequest(query=q, need_tool=True))
        print(f"[green]A:[/] {resp.answer}")
        print(f"[cyan]Sources:[/] {[s.source for s in resp.sources]}")
        print(f"[magenta]Intent:[/] {resp.intent}; Tools: {resp.used_tools}; Latency: {resp.latency_ms:.1f} ms")


if __name__ == "__main__":
    main()
