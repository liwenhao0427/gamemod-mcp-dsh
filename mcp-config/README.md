# MCP 接入配置样例

把 `ABSOLUTE_PATH` 换成 `GameModMCP` 文件夹的真实绝对路径，例如：

- Windows：`C:\\Users\\YourName\\GameModMCP`
- macOS / Linux：`/home/yourname/GameModMCP`

> **Windows 路径提醒**：JSON 里的反斜杠必须转义成 `\\`。
> 或者干脆统一用正斜杠 `C:/Users/YourName/GameModMCP`，Python 也能识别。

---

## 通用前提：先装依赖

```bash
pip install -r requirements.txt      # mcp + frida
```

验证能不能起来：

```bash
python -c "import sys; sys.path.insert(0,'src'); from gamemod_mcp import server; print('ok')"
```

---

## Claude Code

**项目级**：把 [claude-code.mcp.json](claude-code.mcp.json) 的内容存成项目根目录的 `.mcp.json`。

**用户级**：加到 `~/.claude.json` 的 `mcpServers` 字段。

```json
{
  "mcpServers": {
    "gamemod": {
      "command": "python",
      "args": ["ABSOLUTE_PATH/src/gamemod_mcp/server.py"],
      "env": {
        "PYTHONPATH": "ABSOLUTE_PATH/src",
        "PYTHONUTF8": "1",
        "PYTHONUNBUFFERED": "1"
      }
    }
  }
}
```

装完用 `/mcp` 命令确认 `gamemod` 已连接。

---

## Codex CLI

加到 `~/.codex/config.toml`：

```toml
[mcp_servers.gamemod]
command = "python"
args = ["ABSOLUTE_PATH/src/gamemod_mcp/server.py"]

[mcp_servers.gamemod.env]
PYTHONPATH = "ABSOLUTE_PATH/src"
PYTHONUTF8 = "1"
PYTHONUNBUFFERED = "1"
```

---

## Cursor

`~/.cursor/mcp.json`（全局）或项目内 `.cursor/mcp.json`：

```json
{
  "mcpServers": {
    "gamemod": {
      "command": "python",
      "args": ["ABSOLUTE_PATH/src/gamemod_mcp/server.py"],
      "env": {
        "PYTHONPATH": "ABSOLUTE_PATH/src",
        "PYTHONUTF8": "1"
      }
    }
  }
}
```

---

## 其它通用 Agent / 自带 MCP 客户端

任何支持 MCP 的客户端，本质都是**用 stdio 拉起一个子进程**并收发 JSON-RPC。
所以只要满足两点就能接：

1. **启动命令**：`python <ABSOLUTE_PATH>/src/gamemod_mcp/server.py`
   - 或者先 `pip install -e .`，然后直接用 `gamemod-mcp` 命令
2. **环境变量**：`PYTHONPATH=<ABSOLUTE_PATH>/src`、`PYTHONUTF8=1`

手动冒烟测试（不依赖任何客户端）：

```bash
# 发一个 initialize，看有没有正常回包
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}' | python src/gamemod_mcp/server.py
```

---

## 常见问题

### 客户端报告连接失败 / 服务器立刻退出

先单独跑一次，看真正的报错：

```bash
python src/gamemod_mcp/server.py
```

它应该**阻塞住**（在等 stdin 上的 JSON-RPC）。如果打印了 traceback 就说明有问题，
最常见的原因是 `mcp` 或 `frida` 没装、或 `PYTHONPATH` 指错了。

### 提示找不到模块 gamemod_mcp

`PYTHONPATH` 没配对。两种解法：

1. 按上面的样例显式设置 `PYTHONPATH`
2. 或者先 `pip install -e .`（在 GameModMCP 目录下执行），就不用设了

### 工具能列出但调用运行时工具全部失败

Frida 没装，或权限级不匹配。先调 `get_diagnostics()` 看 `frida.installed`。

**注意：注入游戏进程不需要管理员权限**（实测订正）。只要客户端与游戏的
**完整性级别相同**即可 —— 普通权限的 python 注入普通权限的 IL2CPP 游戏
是可行的。唯一的例外是**游戏本身以管理员身份运行**，那时才需要把客户端
也提权。带反作弊 / 反注入保护的网游会失败，那是保护机制，与权限无关。

### 中文输出乱码

设 `PYTHONUTF8=1`（Windows 上尤其需要）。
本项目的日志走 stderr，stdout 只跑 JSON-RPC，所以乱码通常只影响日志显示，不影响功能。
