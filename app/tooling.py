from __future__ import annotations

import datetime as dt
from typing import Dict, Any

from .models import ToolResult


def submit_application(payload: Dict[str, Any]) -> ToolResult:
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    return ToolResult(
        name="submit_application",
        status="drafted",
        payload=payload,
        message="已生成申请草稿（模拟），请登录教务系统确认。",
        data={"submitted_at": now_iso},
    )


def create_repair_ticket(payload: Dict[str, Any]) -> ToolResult:
    ticket = f"FIX-{int(dt.datetime.now(dt.timezone.utc).timestamp())}"
    return ToolResult(
        name="create_repair_ticket",
        status="created",
        message="报修单已生成（模拟），后勤将尽快处理。",
        payload=payload,
        data={"ticket_id": ticket},
    )


def check_library_hours(payload: Dict[str, Any]) -> ToolResult:
    campus = payload.get("campus", "主校区")
    return ToolResult(
        name="check_library_hours",
        status="ok",
        message=f"{campus} 图书馆今日 08:00-22:00 开放，节假日以校历为准。",
        payload=payload,
    )


def cafeteria_menu(payload: Dict[str, Any]) -> ToolResult:
    return ToolResult(
        name="cafeteria_menu",
        status="ok",
        message="今日食堂推荐：鸡腿饭、麻辣香锅、素炒三丝，窗口 3/5/8 号。",
        payload=payload,
    )


def call_security(payload: Dict[str, Any]) -> ToolResult:
    ticket = f"SEC-{int(dt.datetime.now(dt.timezone.utc).timestamp())}"
    return ToolResult(
        name="call_security",
        status="dispatched",
        message="已通知校园安保，5 分钟内联系你。遇紧急情况请拨打 110。",
        payload=payload,
        data={"ticket_id": ticket},
    )


def schedule_counselor(payload: Dict[str, Any]) -> ToolResult:
    date = payload.get("date", "明日")
    return ToolResult(
        name="schedule_counselor",
        status="booked",
        message=f"已为你预留辅导员沟通时段：{date} 15:00-15:30（模拟）。",
        payload=payload,
    )


class ToolRouter:
    def __init__(self):
        self.registry = {
            "submit_application": submit_application,
            "create_repair_ticket": create_repair_ticket,
            "check_library_hours": check_library_hours,
            "cafeteria_menu": cafeteria_menu,
            "call_security": call_security,
            "schedule_counselor": schedule_counselor,
        }

    def call(self, tool_name: str, payload: Dict[str, Any]) -> ToolResult:
        if tool_name not in self.registry:
            raise ValueError(f"Unknown tool: {tool_name}")
        return self.registry[tool_name](payload)
