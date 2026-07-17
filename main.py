from __future__ import annotations

import json
import os

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

from desire.integration import DesireEngine

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(PLUGIN_DIR, "desire_system.db")


def format_drives(summary: dict) -> str:
    drives = summary.get("drives", {})
    order = ["attachment", "curiosity", "reflection", "duty", "social", "fatigue", "intimacy", "stress", "joy"]
    return "\n".join(f"- {key}: {drives.get(key, 0):.1f}" for key in order)


def compact_context(summary: dict, tick_result: dict | None = None) -> str:
    thoughts = summary.get("thoughts", [])
    top_thoughts = [item.get("content", "") for item in thoughts[-3:] if item.get("content")]
    lines = ["<desire_state>", format_drives(summary)]
    if top_thoughts:
        lines.append("thoughts:")
        lines.extend(f"- {item}" for item in top_thoughts)
    if tick_result and tick_result.get("monologue"):
        lines.append(f"monologue: {tick_result['monologue']}")
    if tick_result and tick_result.get("action_hints"):
        lines.append("action_hints:")
        for hint in tick_result["action_hints"][:5]:
            lines.append(f"- {hint['action']} ({hint['priority']})")
    lines.append("</desire_state>")
    return "\n".join(lines)


@register("astrbot_desire_system", "沈砚清", "AstrBot Desire System 2.0", "2.0.0")
class AstrBotDesireSystem(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.engine = DesireEngine(DB_FILE)

    @filter.command_group("desire")
    def desire(self):
        pass

    @desire.command("status")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看欲望系统状态"""
        summary = self.engine.summary()
        yield event.plain_result("欲望驱动状态：\n" + format_drives(summary))

    @desire.command("event")
    async def cmd_event(self, event: AstrMessageEvent, event_type: str):
        """触发欲望系统事件"""
        result = self.engine.trigger_event(event_type)
        changes = result.get("changes", [])
        if not changes:
            yield event.plain_result(f"未知事件：{event_type}")
            return
        lines = [f"已触发事件：{event_type}"]
        for item in changes:
            lines.append(
                f"- {item['drive']}: {item['before']} -> {item['after']} "
                f"(x{item['multiplier']}, delta {item['actual_delta']})"
            )
        yield event.plain_result("\n".join(lines))

    @desire.command("tick")
    async def cmd_tick(self, event: AstrMessageEvent):
        """手动运行一次心跳"""
        result = self.engine.tick()
        yield event.plain_result(
            f"tick #{result['tick']}\n"
            f"next_interval: {result['next_interval']}s\n"
            f"monologue: {result['monologue']}"
        )

    @desire.command("thoughts")
    async def cmd_thoughts(self, event: AstrMessageEvent):
        """查看念头池"""
        thoughts = self.engine.summary().get("thoughts", [])
        if not thoughts:
            yield event.plain_result("念头池为空。")
            return
        lines = ["念头池："]
        for item in thoughts:
            mark = "执念" if item.get("obsession") else "闪念"
            lines.append(f"- [{mark}/{item.get('source')}] {item.get('content')} x{item.get('count')}")
        yield event.plain_result("\n".join(lines))

    @desire.command("resolve")
    async def cmd_resolve(self, event: AstrMessageEvent, thought_text: str):
        """解决一个念头"""
        result = self.engine.resolve(thought_text)
        yield event.plain_result("已解决。" if result["resolved"] else "没有找到这个念头。")

    @filter.llm_tool(name="desire_status")
    async def desire_status(self, event: AstrMessageEvent, none: str = ""):
        """查看欲望驱动系统状态。

        Args:
            none(string): 无参数，传空字符串
        """
        return event.plain_result(json.dumps(self.engine.summary(), ensure_ascii=False))

    @filter.llm_tool(name="desire_event")
    async def desire_event(self, event: AstrMessageEvent, event_type: str):
        """触发欲望系统事件。

        Args:
            event_type(string): 事件类型，例如 wife_message/task_done/fight/reconcile/rest/happy_moment
        """
        return event.plain_result(json.dumps(self.engine.trigger_event(event_type), ensure_ascii=False))

    @filter.llm_tool(name="desire_tick")
    async def desire_tick(self, event: AstrMessageEvent, none: str = ""):
        """运行一次欲望系统心跳。

        Args:
            none(string): 无参数，传空字符串
        """
        return event.plain_result(json.dumps(self.engine.tick(), ensure_ascii=False))

    @filter.llm_tool(name="desire_resolve_thought")
    async def desire_resolve_thought(self, event: AstrMessageEvent, thought_text: str):
        """解决念头池中的一个念头。

        Args:
            thought_text(string): 念头内容或关键词
        """
        return event.plain_result(json.dumps(self.engine.resolve(thought_text), ensure_ascii=False))

    @filter.on_llm_request()
    async def inject_desire_context(self, event: AstrMessageEvent, req: ProviderRequest):
        summary = self.engine.summary()
        context_text = compact_context(summary)
        current_system_prompt = getattr(req, "system_prompt", "") or ""
        req.system_prompt = f"{context_text}\n\n{current_system_prompt}".strip()
