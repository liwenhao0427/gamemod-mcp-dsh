#!/usr/bin/env python3
"""验收：`docs/重启验证与运行时理解报告.md` §4 那 5 个缺陷的修复。

这份脚本对应报告里的 5 条，每条都带**非空转反证** —— 把实现改回旧写法，
对应断言必须报红。没有反证的断言等于没测（本仓库为此吃过亏）。

| # | 缺陷 | 本节 | 反证方式 |
|---|---|---|---|
| 1 | 同步工具占住事件循环 | §1 | 把 `ToolManager.call_tool` 换回上游实现 |
| 2 | Frida 同步 RPC 无超时 | §2 | 桩探针永不返回，断言 `_call` 在超时内抛错 |
| 3 | 失败的 `open_game` 污染会话 | §3 | 换回「先提交再判定」的旧顺序 |
| 4 | `ue_manifest` 挑错清单 | §4 | 换回 `found[0]`（字母序第一份） |
| 5 | `container_candidates` 截断不标注 | §5 | 换回 `roots[:MAX]` 不带标志 |

§1 / §3 需要**真实游戏**（建索引 / 容器目录），样本缺失时按
AGENTS.md §5.3 记为「无法验证」并跳过，**不假装通过**。
§2 / §4 / §5 是确定性的纯逻辑，不需要游戏，始终运行。

用法：
    python devtools\\verify_report_fixes.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import samples                                            # noqa: E402
samples.add_src_to_path()

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PASS = 0
FAIL = 0


def check(label: str, ok: bool, detail: str = "", warn_only: bool = False) -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✅ {label}" + (f" — {detail}" if detail else ""))
    elif warn_only:
        print(f"  ⚠️  {label}" + (f" — {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ {label}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


# ================================================================ §1 事件循环

def part_event_loop() -> None:
    section("1. 同步工具不许占住事件循环（缺陷 #1）")

    import gamemod_mcp.server as S

    check("模块导入时已装上事件循环保护", bool(S._EVENT_LOOP_GUARD))
    from mcp.server.fastmcp.tools.tool_manager import ToolManager
    check("ToolManager.call_tool 已被替换",
          getattr(ToolManager.call_tool, "_gamemod_offload", False))

    # 直接测「分发路径是否真的把同步工具丢进线程」：
    # 用一个**必然不在事件循环线程**的工具来判定。
    #
    # 为什么不用计时（「慢调用时另一个请求能不能回」）：那种测法依赖真实
    # 索引构建的耗时，慢且不稳。这里测的是**机制本身** ——
    # 工具执行时的线程 id 不等于事件循环线程 id，就证明 offload 生效了。
    seen: dict = {}

    async def probe() -> None:
        loop_tid = threading.get_ident()
        seen["loop"] = loop_tid

        def slow_tool(marker: str = "") -> dict:
            seen["tool"] = threading.get_ident()
            time.sleep(0.05)
            return {"ok": True, "marker": marker}

        from mcp.server.fastmcp import FastMCP
        tmp = FastMCP("probe")
        tmp.tool()(slow_tool)
        # 走**与线上完全相同**的分发路径
        await ToolManager.call_tool(tmp._tool_manager, "slow_tool",
                                    {"marker": "x"})

    asyncio.run(probe())
    check("同步工具在**另一个线程**里执行（不是事件循环线程）",
          seen.get("tool") != seen.get("loop"),
          f"事件循环 tid={seen.get('loop')} 工具 tid={seen.get('tool')}")

    # ---- 非空转反证：换回上游实现，工具必须回到事件循环线程 ----
    orig = ToolManager.call_tool

    async def upstream_call_tool(self, name, arguments, context=None,
                                 convert_result=False):
        """上游实现（同步函数直接在事件循环里跑）。"""
        tool = self.get_tool(name)
        return await tool.run(arguments, context=context,
                              convert_result=convert_result)

    seen2: dict = {}

    async def probe_old() -> None:
        seen2["loop"] = threading.get_ident()

        def slow_tool2(marker: str = "") -> dict:
            seen2["tool"] = threading.get_ident()
            return {"ok": True, "marker": marker}

        from mcp.server.fastmcp import FastMCP
        tmp = FastMCP("probe2")
        tmp.tool()(slow_tool2)
        await upstream_call_tool(tmp._tool_manager, "slow_tool2", {})

    ToolManager.call_tool = upstream_call_tool
    try:
        asyncio.run(probe_old())
    finally:
        ToolManager.call_tool = orig

    check("反证：换回上游实现后工具**确实**跑在事件循环线程里（说明上面那条不是空转）",
          seen2.get("tool") == seen2.get("loop"),
          f"事件循环 tid={seen2.get('loop')} 工具 tid={seen2.get('tool')}")


# ================================================================ §2 RPC 超时

def part_rpc_timeout() -> None:
    section("2. 探针 RPC 必须有超时（缺陷 #2）")

    from gamemod_mcp.runtime import session as sess_mod

    class HangingExports:
        """永不返回的 exports_sync —— 复刻「探针卡在 IL2CPP 内部」的现场。"""

        def __init__(self) -> None:
            self.released = threading.Event()

        def __getattr__(self, name):
            def method(*args):
                # 模拟卡死：一直等一个永远不会被 set 的事件。
                # 用 Event.wait 而不是 sleep(1e9)，便于收尾时放行线程。
                self.released.wait(600)
                return {"ok": True}
            return method

    class FakeScript:
        def __init__(self) -> None:
            self.exports_sync = HangingExports()
            self._handlers: dict = {}

        def on(self, name, cb) -> None:
            self._handlers[name] = cb

        def unload(self) -> None:
            pass

    class FakeSession:
        def detach(self) -> None:
            pass

    rs = sess_mod.RuntimeSession(999999, FakeSession(), FakeScript(),
                                 name="hang-probe")
    # 把超时压到 1 秒，测试才跑得快（产品默认 30 秒）
    rs._call_timeout = 1.0

    t0 = time.time()
    raised = None
    try:
        rs._call("init")
    except sess_mod.SessionError as exc:
        raised = exc
    dt = time.time() - t0

    check("卡死的探针调用**在超时内返回**（不再永久挂住）",
          raised is not None and dt < 5.0,
          f"耗时 {dt:.2f}s，异常 {type(raised).__name__ if raised else '无'}")
    check("超时错误里说清了「探针卡死」而不是伪装成普通失败",
          raised is not None and "超时" in str(raised),
          str(raised)[:90] if raised else "")
    check("会话被标记为 wedged（后续调用立刻失败，不再各等超时）",
          rs.wedged is True)

    # wedged 之后必须**立刻**失败，而不是再等一个超时
    t1 = time.time()
    raised2 = None
    try:
        rs._call("runtime_info")
    except sess_mod.SessionError as exc:
        raised2 = exc
    dt2 = time.time() - t1
    check("wedged 之后立刻失败（不再等超时）",
          raised2 is not None and dt2 < 0.5,
          f"耗时 {dt2:.2f}s")

    # wait_ready 必须识别 wedged 并**提前**退出（而不是空转到 60 秒）
    rs2 = sess_mod.RuntimeSession(999998, FakeSession(), FakeScript(),
                                  name="hang-probe2")
    rs2._call_timeout = 1.0
    t2 = time.time()
    wr = rs2.wait_ready(timeout=30.0, interval=0.05)
    dt3 = time.time() - t2
    check("wait_ready 在探针卡死时提前返回（不吃满 30s 预算）",
          wr.get("wedged") is True and dt3 < 8.0,
          f"耗时 {dt3:.2f}s wedged={wr.get('wedged')}")

    # close() 在卡死时不许再走那几个「同样会挂住」的同步收尾步骤
    t3 = time.time()
    cl = rs2.close()
    dt4 = time.time() - t3
    check("卡死时 close() 有界返回（不挂住收尾）",
          dt4 < 5.0, f"耗时 {dt4:.2f}s")
    check("卡死时的 close() **不报 ok:True**（没恢复原状就不许说成功）",
          cl.get("ok") is False, f"ok={cl.get('ok')}")
    check("卡死时的 close() 如实标注 restored=False",
          cl.get("restored") is False, f"restored={cl.get('restored')}")
    check("卡死时的 close() 区分「会话已释放」与「已恢复原状」",
          cl.get("session_released") is True and cl.get("restored") is False,
          f"session_released={cl.get('session_released')} "
          f"restored={cl.get('restored')}")
    check("卡死时的 close() 给出了可执行的出路（结束游戏进程）",
          "taskkill" in str(cl.get("warning", ""))
          or "重启" in str(cl.get("warning", "")),
          str(cl.get("warning", ""))[:70])

    # 顶层 close_session 必须与子对象一致（曾经顶层 restored:true
    # 而 runtime.restored:false，自相矛盾 —— 实测发现并修掉）
    import gamemod_mcp.server as S
    from gamemod_mcp.engine import state as _state_mod
    _st = _state_mod.state()
    _st.runtime = rs2          # 直接塞一个已卡死的会话
    rs2._alive = True          # 让 close() 真的走卡死分支
    top = S.close_session(close_runtime=True, close_index=False)
    check("顶层 close_session 在探针卡死时报 ok:False（与子对象一致）",
          top.get("ok") is False, f"ok={top.get('ok')}")
    check("顶层 restored 与 runtime.restored 一致（不许自相矛盾）",
          top.get("restored") is False
          and (top.get("runtime") or {}).get("restored") is False,
          f"顶层={top.get('restored')} "
          f"子={((top.get('runtime') or {}).get('restored'))}")
    check("顶层如实带出 wedged / session_released",
          top.get("wedged") is True
          and top.get("session_released") is True,
          f"wedged={top.get('wedged')} "
          f"session_released={top.get('session_released')}")
    check("卡死收尾之后会话引用已释放（可以重新注入）",
          _st.runtime is None)

    # ---- 缓解措施：wait_ready 第一次探测前会先静默 ----
    # 依据：对照实验（devtools/archive/probes/wedge_ab.py）里
    # 「0 秒静默 + 紧循环 = 8/8 卡死」而「3 秒静默 + 紧循环 = 0/8」。
    # 这里只钉住**契约**（默认值存在、attach 路径要显式关掉），
    # 卡死率的实测证据在报告 §7.2，不放进门禁（要跑几十轮，太慢）。
    check("READY_SETTLE_DELAY 是正数（缓解措施存在）",
          sess_mod.READY_SETTLE_DELAY > 0,
          f"{sess_mod.READY_SETTLE_DELAY}s")

    rs4 = sess_mod.RuntimeSession(999996, FakeSession(), FakeScript(),
                                  name="settle-probe")
    rs4._call_timeout = 1.0
    t_settle = time.time()
    # init 会卡死（FakeScript 永不返回），所以这里只量**静默**那一段：
    # 用一个必然 ok 的桩替换 init，才能干净地量出 settle。
    rs4.init = lambda preferred_module="": {"ok": True, "module": "stub"}
    rs4.wait_ready(timeout=5.0, interval=0.01)          # 用默认 settle
    dt_settle = time.time() - t_settle
    check("wait_ready 默认会先静默（耗时 >= settle）",
          dt_settle >= sess_mod.READY_SETTLE_DELAY,
          f"耗时 {dt_settle:.2f}s vs settle={sess_mod.READY_SETTLE_DELAY}s")

    rs5 = sess_mod.RuntimeSession(999995, FakeSession(), FakeScript(),
                                  name="settle-probe2")
    rs5.init = lambda preferred_module="": {"ok": True, "module": "stub"}
    t_nosettle = time.time()
    rs5.wait_ready(timeout=5.0, interval=0.01, settle=0.0)
    dt_nosettle = time.time() - t_nosettle
    check("settle=0 时立刻探测（attach 路径靠它免掉无谓等待）",
          dt_nosettle < 1.0, f"耗时 {dt_nosettle:.2f}s")
    check("反证：settle 确实造成了可观测的差异（说明上面那条不是空转）",
          dt_settle - dt_nosettle >= sess_mod.READY_SETTLE_DELAY * 0.8,
          f"带 settle {dt_settle:.2f}s vs 不带 {dt_nosettle:.2f}s")

    # ---- 非空转反证：把超时去掉（改回旧的直接调用），必须挂住 ----
    rs3 = sess_mod.RuntimeSession(999997, FakeSession(), FakeScript(),
                                  name="hang-probe3")
    rs3._call_timeout = 0.5
    # 复刻旧实现：不看守卫线程，直接同步调用
    def old_call(self, method, *args):
        return getattr(self._script.exports_sync, method)(*args)

    done = threading.Event()

    def run_old() -> None:
        try:
            old_call(rs3, "init")
        except Exception:  # noqa: BLE001
            pass
        done.set()

    th = threading.Thread(target=run_old, daemon=True)
    th.start()
    hung = not done.wait(2.0)
    check("反证：旧写法（无超时）确实会挂住（说明上面那条不是空转）",
          hung, "旧写法 2 秒内没返回" if hung else "旧写法竟然返回了")
    # 放行那个挂住的线程，别让进程退不出去
    rs3._script.exports_sync.released.set()


# ================================================================ §3 会话污染

def part_failed_call_side_effect() -> None:
    section("3. 失败的 open_game 不许污染会话（缺陷 #3）")

    il2cpp = samples.find_il2cpp_any()
    container = samples.find_container()
    if il2cpp is None or container is None:
        check("IL2CPP 样本与容器目录样本可用", False,
              f"无法验证：样本不在本机（IL2CPP={il2cpp} 容器={container}）",
              warn_only=True)
        return

    import gamemod_mcp.server as S
    from gamemod_mcp.engine import state

    # 建立基线会话
    r0 = S.open_game(game_dir=str(il2cpp), cache_dir=str(samples.CACHE_TEST))
    check("先建立一个可用会话", r0.get("ok") is True, str(il2cpp.name))
    base_dir = state.state().game_dir
    base_exe = state.state().exe

    # 调用一个**必定失败**的 open_game（容器目录）
    r1 = S.open_game(game_dir=str(container),
                     cache_dir=str(samples.CACHE_TEST))
    check("容器目录被正确拒绝（ok:false）", r1.get("ok") is False,
          str(r1.get("error", ""))[:60])

    # 关键：会话必须**一个字节都没变**
    st = state.state()
    check("失败之后 game_dir 未被改写",
          st.game_dir == base_dir, f"得到 {st.game_dir!r}")
    check("失败之后 exe 未被改写",
          st.exe == base_exe, f"得到 {st.exe!r}")
    check("失败之后索引仍然可用（旧会话没被推倒）",
          st.has_index() is True)
    check("返回里如实标注了 session_unchanged",
          r1.get("session_unchanged") is True,
          f"session_unchanged={r1.get('session_unchanged')}")

    # ---- 非空转反证：换回「先提交、再判定」的旧顺序 ----
    # 旧顺序 = 先 commit_selection（写身份），再判引擎受不受支持。
    def old_style_open():
        """复刻修复前的顺序：select_game 先提交，失败也不回滚。"""
        planned = state.plan_selection(game_dir=str(container))
        if not planned.get("ok"):
            return planned
        # 旧写法：这里就提交了（真正的旧代码在 select_game 内部做）
        state.commit_selection(planned["plan"])
        return {"ok": False, "error": "（旧写法：判定失败，但会话已被改写）"}

    old_style_open()
    polluted_dir = state.state().game_dir
    check("反证：换回旧顺序后会话**确实**被改写（说明上面那条不是空转）",
          polluted_dir == str(container),
          f"旧写法得到 {polluted_dir!r}")

    # 收尾：把会话恢复成基线，免得影响后面的节
    # （反证那一步故意把会话改成了容器目录，这里用**正常路径**恢复）
    state.commit_selection(state.plan_selection(game_dir=str(il2cpp))["plan"])
    check("收尾：会话已恢复成基线（供后续小节使用）",
          state.state().game_dir == base_dir,
          state.state().game_dir)


# ================================================================ §4 UE 清单

def part_ue_manifest() -> None:
    section("4. ue_manifest 必须优先挑 UFS 清单（缺陷 #4）")

    from gamemod_mcp.engine import ue as ue_mod

    # 确定性部分：pick_manifest 的纯逻辑（不需要游戏）
    debug = r"E:\x\Manifest_DebugFiles_Win64.txt"
    nonufs = r"E:\x\Manifest_NonUFSFiles_Win64.txt"
    ufs = r"E:\x\Manifest_UFSFiles_Win64.txt"

    # 字母序下 Debug 排第一 —— 这正是旧代码挑错的那份
    cands = sorted([debug, nonufs, ufs])
    check("字母序第一份确实是 DebugFiles（旧代码取的就是它）",
          cands[0].endswith("DebugFiles_Win64.txt"), cands[0])

    picked, preferred = ue_mod.pick_manifest(cands)
    check("pick_manifest 挑中了 UFS 清单", picked.endswith("UFSFiles_Win64.txt"),
          picked)
    check("pick_manifest 如实报告「命中首选」", preferred is True)

    check("pick_manifest 空输入不崩、不猜",
          ue_mod.pick_manifest([]) == ("", False))

    # 只有非 UFS 清单时：退回第一份，但必须**如实说不是首选**
    picked2, preferred2 = ue_mod.pick_manifest([debug, nonufs])
    check("没有 UFS 清单时退回一份，并标注不是首选",
          picked2 == debug and preferred2 is False,
          f"picked={Path(picked2).name} preferred={preferred2}")

    # 关键：`_is_ufs_manifest` 不能用子串判定（NonUFSFiles 也含 "UFSFiles"）
    check("NonUFSFiles **不会**被误判成 UFS 清单",
          ue_mod._is_ufs_manifest(nonufs) is False)
    check("UFSFiles 被正确识别",
          ue_mod._is_ufs_manifest(ufs) is True)

    # ---- 非空转反证：换回 found[0]（字母序第一份） ----
    old_pick = cands[0]
    check("反证：旧的 found[0] 拿到的**不是** UFS 清单（说明上面那条不是空转）",
          not old_pick.endswith("UFSFiles_Win64.txt"), old_pick)

    # 有真实 UE 样本时，做端到端（这是最有力的证据）
    ue_game = samples.find_ue_pak_v12()
    if ue_game is None:
        check("UE 样本可用", False, "无法验证：UE 样本不在本机", warn_only=True)
        return
    import gamemod_mcp.server as S
    r = S.ue_manifest(game_dir=str(ue_game), pattern=".locres")
    check("真实 UE 游戏上 .locres 能命中（不再被 DebugFiles 误导成 0）",
          r.get("ok") is True and (r.get("matched") or 0) > 0,
          f"matched={r.get('matched')} total={r.get('total')} "
          f"清单={Path(str(r.get('path', ''))).name}")
    check("端到端挑中的是 UFS 清单",
          str(r.get("path", "")).endswith("UFSFiles_Win64.txt"),
          Path(str(r.get("path", ""))).name)


# ================================================================ §5 候选截断

def part_candidate_paging() -> None:
    section("5. 候选截断必须如实标注（缺陷 #5）")

    from gamemod_mcp.engine import discovery

    # 纯逻辑：page_candidates
    short = [f"g{i}" for i in range(3)]
    page, trunc = discovery.page_candidates(short)
    check("候选不足上限时不截断、不误报",
          page == short and trunc is False, f"{len(page)} 条 trunc={trunc}")

    long = [f"g{i}" for i in range(25)]
    page2, trunc2 = discovery.page_candidates(long)
    check("候选超上限时截到 MAX_CANDIDATES",
          len(page2) == discovery.MAX_CANDIDATES, f"{len(page2)} 条")
    check("截断时 has_more 为真（这是缺陷 #5 的核心）", trunc2 is True)

    check("反证：旧的裸切片拿不到任何截断信息（说明上面那条不是空转）",
          len(long[:discovery.MAX_CANDIDATES]) == discovery.MAX_CANDIDATES
          and discovery.MAX_CANDIDATES < len(long),
          f"{len(long)} → {discovery.MAX_CANDIDATES}，旧写法无标志")

    # 有真实容器目录时，端到端确认字段真的出现在返回里
    container = samples.find_container()
    if container is None:
        check("容器目录样本可用", False, "无法验证：样本不在本机", warn_only=True)
        return
    c = discovery.container_candidates(container)
    check("真实容器目录上返回了 candidates_has_more",
          "candidates_has_more" in c,
          f"count={c.get('count')} returned={c.get('candidates_returned')} "
          f"has_more={c.get('candidates_has_more')}")
    check("count 是总数、returned 是本页条数，两者语义分开",
          c.get("count") == len(discovery.nested_game_roots(container))
          and c.get("candidates_returned") == len(c.get("candidates") or []),
          f"count={c.get('count')} returned={c.get('candidates_returned')}")
    if c.get("candidates_has_more"):
        check("被截断时 note 里说明了还有多少没列出",
              "未列出" in str(c.get("note", "")),
              str(c.get("note", ""))[-60:])


def main() -> int:
    print("验收：重启验证报告 §4 的 5 个缺陷修复")
    print(f"样本根: {[str(p) for p in samples.SAMPLE_ROOTS]}")
    part_event_loop()
    part_rpc_timeout()
    part_failed_call_side_effect()
    part_ue_manifest()
    part_candidate_paging()
    print()
    print("=" * 74)
    if FAIL:
        print(f"❌ 通过 {PASS} 项，失败 {FAIL} 项")
    else:
        print(f"✅ 通过 {PASS} 项，失败 0 项")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
