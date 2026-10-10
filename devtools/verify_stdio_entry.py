#!/usr/bin/env python3
"""验收：MCP 服务器的**进程级入口**（stdio），两种启动形式都必须能用。

为什么单独一个脚本
============================================================
MCP 客户端配置与文档里出现过两种写法，它们走的代码路径不同：

    ① python -m gamemod_mcp.server             （需要 PYTHONPATH=<repo>/src）
    ② python <repo>/src/gamemod_mcp/server.py  （README / mcp-config 里就是这种）

② 曾经是**坏的**：`server.py` 里用的是 `from . import cap` 这类相对导入，
而以脚本方式运行时没有父包（`__package__` 是空串），于是当场

    ImportError: attempted relative import with no known parent package

而这个异常发生在**任何工具注册之前**，客户端看到的只有

    mcp.shared.exceptions.McpError: Connection closed

—— 从报错里完全看不出是入口写法的问题，只会怀疑环境坏了。
更阴的是它还**不止一处**：`warn` / `progress` / `ue` / `decompile` 都曾是
写在函数体里的惰性相对导入，于是「initialize + tools/list 全过」与
「一调用 ue_* / decompile 就 ImportError」可以同时成立。
所以本脚本第 4 节会**真的去调用**那些工具，而不是只握手。

四节断言
============================================================
1. 形式 ② 在**没有 PYTHONPATH** 的环境下能起来（修复的核心断言）；
2. 形式 ① 也能起来；两种形式报出的工具数必须一致；
3. stdout **全程只有 JSON-RPC**（往 stdout 打日志会直接破坏协议）；
4. 脚本形式下逐个调用「历史上用惰性导入的那批工具」（5 个 UE 工具 + decompile），
   任何一个抛异常（isError）都算失败；
5. 非空转反证：喂一个注定 ImportError 的入口，同一套断言必须报红 ——
   否则「握手成功」可能只是因为脚本根本没在检查。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import samples                                            # noqa: E402
import mcp_stdio                                          # noqa: E402

samples.add_src_to_path()
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PASS = 0
FAIL = 0
WARN = 0

SERVER_PY = samples.SRC / "gamemod_mcp" / "server.py"


def check(label: str, cond: bool, detail: str = "", warn_only: bool = False) -> bool:
    global PASS, FAIL, WARN
    if cond:
        print(f"  \u2705 {label}" + (f" \u2014 {detail}" if detail else ""))
        PASS += 1
        return True
    if warn_only:
        print(f"  \u26a0\ufe0f  {label}" + (f" \u2014 {detail}" if detail else ""))
        WARN += 1
        return False
    print(f"  \u274c {label}" + (f" \u2014 {detail}" if detail else ""))
    FAIL += 1
    return False


def section(title: str) -> None:
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def check_tool_ok(tag: str, r: dict, expect_data_ok: bool | None = None) -> None:
    """断言一次工具调用没有抛异常（ImportError 会以 isError 出现）。"""
    if not r["ok"]:
        check(f"{tag} 调用走通", False, r["error"][:300])
        return
    check(f"{tag} 没有抛异常（isError=False）", not r["is_error"],
          (r["error"] or "")[:300])
    if r["is_error"]:
        return
    data = r["data"]
    if expect_data_ok is not None and isinstance(data, dict):
        check(f"{tag} 业务结果 ok={expect_data_ok}",
              bool(data.get("ok")) is expect_data_ok,
              str(data.get("error", ""))[:200])


def main() -> int:
    print("验收：MCP 服务器进程级入口（两种启动形式）")
    print(f"仓库根  : {samples.ROOT}")
    print(f"server.py: {SERVER_PY}")

    if not SERVER_PY.is_file():
        print(f"\n\u274c 找不到 {SERVER_PY}")
        return 1

    ue = samples.find_ue_pak_v12()
    mono = samples.find_mono()

    # ---------------------------------------------------------------- 1
    section("1. 脚本形式：python <repo>/src/gamemod_mcp/server.py（**不带 PYTHONPATH**）")
    srv = mcp_stdio.McpStdioServer(
        [sys.executable, str(SERVER_PY)],
        env=mcp_stdio.script_form_env(str(samples.SRC), clean_pythonpath=True),
        cwd=str(samples.ROOT)).start()
    script_tools: list[str] = []
    try:
        init = srv.initialize()
        check("能完成 initialize 握手（不再 Connection closed）",
              init["ok"], init["error"][:300] or
              f"serverInfo={init['server_info']}")
        tl = srv.tools()
        script_tools = tl["names"]
        check("tools/list 拿到工具", len(script_tools) > 20,
              f"{len(script_tools)} 个")
        check("stdout 全程只有 JSON-RPC（到目前为止）",
              srv.non_json_lines == 0, f"非 JSON 行数={srv.non_json_lines}")

        # ------------------------------------------------------------ 4
        # 这一节必须在**脚本形式**的同一个进程里做：
        # 相对导入的惰性导入只有在「以脚本启动」时才炸。
        section("4. 脚本形式下真的调用那批「曾用惰性相对导入」的工具")

        if ue is not None:
            r = srv.call_tool("open_game", {"game_dir": str(ue),
                                            "cache_dir": str(samples.CACHE_TEST)})
            check_tool_ok("open_game(UE)", r)
            for tag, args in [
                ("detect_ue", {"exe": ""}),
                ("ue_manifest", {"game_dir": str(ue)}),
            ]:
                check_tool_ok(tag, srv.call_tool(tag, args, timeout=600))
            prof = srv.call_tool("ue_profile", {"exe": ""}, timeout=600)
            check_tool_ok("ue_profile", prof)
            cont = ""
            if isinstance(prof["data"], dict):
                boxes = (prof["data"].get("pak") or []) + \
                        (prof["data"].get("iostore") or [])
                if boxes:
                    first = boxes[0]
                    cont = (first.get("pak") or first.get("utoc")
                            or first.get("path") or "")
            if cont:
                check_tool_ok("ue_list_entries",
                              srv.call_tool("ue_list_entries",
                                            {"container": cont, "limit": 5},
                                            timeout=600))
            else:
                check("ue_profile 报出了容器路径（ue_list_entries 才能测）",
                      False, "容器列表为空", warn_only=True)
            check_tool_ok("ue_script_objects",
                          srv.call_tool("ue_script_objects", {"limit": 5},
                                        timeout=600))
        else:
            check("UE 样本可用", False, "无法验证：样本不在本机", warn_only=True)

        if mono is not None:
            r = srv.call_tool("open_game", {"game_dir": str(mono),
                                            "cache_dir": str(samples.CACHE_TEST)})
            check_tool_ok("open_game(Mono)", r)
            # decompile 也是惰性导入（`from .engine import decompile`）。
            # 直接拿游戏里的真实类型名去反编译 —— 只用 isError 判定，
            # 类型名挑不中时再换下一个候选。
            cands = srv.call_tool("search_classes", {"pattern": "", "limit": 12})
            names = [it.get("full_name", "") for it in
                     (cands["data"] or {}).get("items", [])] \
                if isinstance(cands["data"], dict) else []
            names = [n for n in names if n and not n.startswith("<")]
            done = False
            for name in names[:6]:
                r = srv.call_tool("decompile", {"type_name": name}, timeout=600)
                if not r["ok"] or r["is_error"]:
                    check_tool_ok(f"decompile({name})", r)
                    done = True
                    break
                if (r["data"] or {}).get("ok"):
                    check(f"decompile({name}) 成功且没有抛异常",
                          True, f"{len(r['data'].get('source', ''))} 字符源码")
                    done = True
                    break
            if not done:
                check("decompile 在脚本形式下可用", False,
                      f"试过 {names[:6]} 都没拿到源码", warn_only=True)
        else:
            check("Mono 样本可用", False, "无法验证：样本不在本机", warn_only=True)

        check("stdout 全程只有 JSON-RPC（跑完工具调用后）",
              srv.non_json_lines == 0, f"非 JSON 行数={srv.non_json_lines}")
    finally:
        srv.close()

    # ---------------------------------------------------------------- 2
    section("2. 模块形式：python -m gamemod_mcp.server（PYTHONPATH=<repo>/src）")
    srv2 = mcp_stdio.McpStdioServer(
        [sys.executable, "-m", "gamemod_mcp.server"],
        env=mcp_stdio.script_form_env(str(samples.SRC), clean_pythonpath=False),
        cwd=str(samples.ROOT)).start()
    try:
        init2 = srv2.initialize()
        check("能完成 initialize 握手", init2["ok"], init2["error"][:300] or
              f"serverInfo={init2['server_info']}")
        tl2 = srv2.tools()
        check("tools/list 拿到工具", len(tl2["names"]) > 20,
              f"{len(tl2['names'])} 个")
        check("stdout 全程只有 JSON-RPC", srv2.non_json_lines == 0,
              f"非 JSON 行数={srv2.non_json_lines}")
    finally:
        srv2.close()

    if script_tools and tl2["ok"]:
        only_a = sorted(set(script_tools) - set(tl2["names"]))
        only_b = sorted(set(tl2["names"]) - set(script_tools))
        check("两种形式报出的工具清单完全一致",
              script_tools == tl2["names"],
              f"脚本形式 {len(script_tools)} 个 / 模块形式 {len(tl2['names'])} 个")
        check("没有只在某一种形式下出现的工具",
              not only_a and not only_b,
              f"仅脚本形式有 {only_a}；仅模块形式有 {only_b}")

    # ---------------------------------------------------------------- 5
    section("5. 非空转反证：注定 ImportError 的入口必须被这套断言判为失败")
    with tempfile.TemporaryDirectory() as td:
        broken = Path(td) / "broken_server.py"
        # 与修复前的 server.py 同一种坏法：以脚本运行时做相对导入
        broken.write_text("from . import nothing  # noqa\n", encoding="utf-8")
        bsrv = mcp_stdio.McpStdioServer(
            [sys.executable, str(broken)],
            env=mcp_stdio.script_form_env(str(samples.SRC),
                                          clean_pythonpath=True),
            cwd=td).start()
        try:
            binit = bsrv.initialize(timeout=30)
        finally:
            bsrv.close()
        check("坏入口被判为失败（不是假成功）", not binit["ok"],
              binit["error"][:200])
        check("失败原因里能看出是导入失败，而不是「连接被关掉」了事",
              "ImportError" in binit["error"]
              or "attempted relative import" in binit["error"],
              binit["error"][-200:])

    print()
    print("=" * 74)
    if FAIL:
        print(f"\u274c 通过 {PASS} 项，失败 {FAIL} 项（警告 {WARN}）")
    else:
        print(f"\u2705 通过 {PASS} 项，失败 0 项"
              + (f"（警告 {WARN}）" if WARN else ""))
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
