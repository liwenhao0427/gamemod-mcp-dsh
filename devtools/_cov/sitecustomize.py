"""动态记录 MCP 工具的真实调用：sitecustomize 垫片。

为什么需要它
============================================================
`verify_tool_coverage.py` 是**静态**检查（工具名有没有出现在脚本里），
它证明不了「这个工具真的被跑过」—— 脚本完全可以在
`if not have(sample): skip` 分支里跳过某个工具，名字却还在。

本文件补上**动态**证据：作为 sitecustomize 在解释器启动时加载，
把每个工具函数包一层，记录「谁被调用、返回的 ok 是什么、返回体多大」。

用法（不直接运行）：
    $env:PYTHONPATH = "<repo>\\devtools\\_cov"
    $env:GAMEMOD_COV_OUT = "<repo>\\.cache-test\\tool_calls.jsonl"
    python devtools\\verify_mcp_tools.py

原理
============================================================
verify 脚本都是先 `sys.path.insert(...)` 再 `import gamemod_mcp.server as S`，
之后通过 `S.<工具名>(...)` 调用。因为 `S` 绑定的是**模块对象本身**，
所以只要在导入完成之后改掉模块上的属性，`S.<工具名>` 在调用时
就会解析到我们的包装版 —— 不需要拦截 import 过程。

因此做法极简：包一层 `builtins.__import__`，在
`gamemod_mcp.server` 被导入完成的那一刻包装它的工具属性。

垫片只做**观测**：不改参数、不改返回值，异常原样抛出，
因此不会影响被测逻辑。
"""

from __future__ import annotations

import atexit
import builtins
import json
import os

_OUT = os.environ.get("GAMEMOD_COV_OUT", "")
_TARGET = "gamemod_mcp.server"
_records: list[dict] = []
_wrapped = False


def _wrap_module(module) -> None:
    global _wrapped
    if _wrapped:
        return
    manager = getattr(getattr(module, "mcp", None), "_tool_manager", None)
    if manager is None:
        return
    _wrapped = True

    for name in set(manager._tools):
        fn = getattr(module, name, None)
        if fn is None or not callable(fn):
            continue

        def make(orig, tname):
            def wrapper(*args, **kwargs):
                rec: dict = {"tool": tname, "ok": None, "error": None, "size": 0}
                try:
                    res = orig(*args, **kwargs)
                    if isinstance(res, dict):
                        rec["ok"] = res.get("ok")
                        rec["error"] = res.get("error")
                    try:
                        rec["size"] = len(json.dumps(res, default=str))
                    except (TypeError, ValueError):
                        rec["size"] = -1
                    return res
                except BaseException as exc:   # 记录后原样抛出
                    rec["ok"] = False
                    rec["error"] = f"{type(exc).__name__}: {exc}"
                    raise
                finally:
                    _records.append(rec)

            wrapper.__name__ = getattr(orig, "__name__", tname)
            wrapper.__doc__ = getattr(orig, "__doc__", None)
            return wrapper

        setattr(module, name, make(fn, name))


def _dump() -> None:
    if not _OUT or not _records:
        return
    try:
        parent = os.path.dirname(_OUT)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(_OUT, "a", encoding="utf-8") as fh:
            for r in _records:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    except OSError:
        pass


if _OUT:
    _real_import = builtins.__import__

    def _hooked_import(name, globals=None, locals=None, fromlist=(), level=0):
        module = _real_import(name, globals, locals, fromlist, level)
        # 两种 import 写法都要覆盖：
        #   import gamemod_mcp.server as S            → name == "gamemod_mcp.server"
        #   from gamemod_mcp import server as S       → name == "gamemod_mcp"
        #                                               fromlist == ("server",)
        # 后者一开始被漏掉了，导致 verify_mcp_tools.py 的调用没被记录。
        hit = (
            level == 0
            and (
                name == _TARGET
                or name.startswith(_TARGET + ".")
                or (
                    name == _TARGET.split(".")[0]
                    and _TARGET.split(".", 1)[1] in (fromlist or ())
                )
            )
        )
        if hit:
            try:
                import sys
                mod = sys.modules.get(_TARGET)
                if mod is not None:
                    _wrap_module(mod)
            except Exception:
                pass
        return module

    builtins.__import__ = _hooked_import
    atexit.register(_dump)
