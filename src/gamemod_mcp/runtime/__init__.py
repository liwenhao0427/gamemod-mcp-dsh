# GameMod MCP 的 Frida 运行时层
#
# 职责：把 agent.js 探针注入目标游戏进程，并把游戏内的能力暴露成
# Python 侧的普通方法调用。上层 MCP 工具只跟本包打交道，不直接碰 frida。
#
# 与 AiMod 的定位一致：AiMod 用 Frida Gadget（lib\AiCore.dll 是改名的
# frida-gadget-raw.dll）做游戏内探针；本实现用常规 Frida 注入达到同等效果，
# 区别在于不需要游戏侧预先打包 Gadget。
from .session import (
    RuntimeSession,
    SessionError,
    RuntimeUnavailable,
    attach,
    spawn_and_attach,
    get_session,
    list_sessions,
    close_session,
    close_all_sessions,
    probe_available,
    frida_version,
    agent_source,
)

__all__ = [
    "RuntimeSession",
    "SessionError",
    "RuntimeUnavailable",
    "attach",
    "spawn_and_attach",
    "get_session",
    "list_sessions",
    "close_session",
    "close_all_sessions",
    "probe_available",
    "frida_version",
    "agent_source",
]
