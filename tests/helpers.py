"""整合測試輔助：以假 LLM 驅動 Supervisor，模擬 CLI 的審核迴圈"""
from typing import Any, Callable

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command


class ToolCallingFake(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def build_agent_calling(tool, args: dict[str, Any]):
    """建立一個「先呼叫指定工具一次，再回覆 done」的 Supervisor"""
    msgs = iter([
        AIMessage(content="", tool_calls=[{"name": tool.name, "args": args, "id": "call-1"}]),
        AIMessage(content="done"),
    ])
    saver = InMemorySaver()
    agent = create_agent(model=ToolCallingFake(messages=msgs), tools=[tool], checkpointer=saver)
    return agent, saver


def drive(agent, decide: Callable[[dict[str, Any]], dict[str, Any]], thread_id: str = "t1"):
    """執行到結束；每次中斷時以 decide(request) 的結果恢復。回傳 (所有審核請求, 最後的 ToolMessage)"""
    cfg = {"configurable": {"thread_id": thread_id}, "recursion_limit": 60}
    payload: Any = {"messages": [("user", "go")]}
    requests = []
    while True:
        agent.invoke(payload, cfg)
        interrupts = agent.get_state(cfg).interrupts
        if not interrupts:
            break
        req = interrupts[0].value
        requests.append(req)
        payload = Command(resume={interrupts[0].id: decide(req)})
    tool_msgs = [m for m in agent.get_state(cfg).values["messages"] if isinstance(m, ToolMessage)]
    return requests, tool_msgs[-1]
