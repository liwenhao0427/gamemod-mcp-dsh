"""GameMod MCP —— 通用 Agent 的游戏 Mod 辅助工具集。

本包把「AI 辅助做游戏 Mod」所需的底层能力封装成标准 MCP 工具，
使任何支持 MCP 的通用 Agent（Claude Code / Codex / Cursor 等）都能接入。

流水线：发现进程 → 注入探针 → 提取元数据 → 建索引 → 查询定位 → Hook 创作
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
