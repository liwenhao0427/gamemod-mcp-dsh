#!/usr/bin/env python3
"""验证会话状态的**目标选择**逻辑（P0 回归测试）。

被修复的缺陷
============================================================
`ensure_runtime` 开头是无条件早退：

    with _state.lock:
        if _state.has_runtime():
            return {"ok": True, "reused": True, **_state.summary()}   # ← 缺陷

后果有两个，都不是「不好用」而是「静默撒谎」：

1. 已有 pid=A 的会话时调用 `inject_process(pid=B)` → **静默返回 A**，
   返回体里 `ok=True`、`pid=A`。Agent 以为注入到了 B。
2. `launch_game()` 在已有会话时**永远不启动新进程**，
   直接返回旧会话。Agent 以为游戏是它启动的。

为什么必须自动化验证
============================================================
这两个都是「返回值看起来完全正常」的错误，只能靠断言
「传入的目标 == 返回的目标」来发现，人工测试极易漏掉。

本脚本用**假的 RuntimeSession**替换真实注入，因此：
    · 不需要管理员权限
    · 不启动游戏、不注入任何进程
    · 可以在 CI / 任何机器上跑

    python devtools/verify_runtime_target.py
"""

from __future__ import annotations

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import io

_sys.stdout = io.TextIOWrapper(_sys.stdout.buffer, encoding="utf-8",
                               errors="replace")

import samples  # noqa: E402
samples.add_src_to_path()

from gamemod_mcp.engine import state as S  # noqa: E402

PASS, FAIL = [], []


def check(label: str, cond: bool, detail: str = "") -> None:
    tag = "✅" if cond else "❌"
    print(f"  {tag} {label}" + (f" — {detail}" if detail else ""))
    (PASS if cond else FAIL).append(label)


def show(title: str) -> None:
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


class FakeRuntime:
    """假装是一个活着的运行时会话。不碰 Frida。"""

    def __init__(self, pid: int, spawned: bool = False) -> None:
        self.pid = pid
        self.name = f"fake-{pid}.exe"
        self.spawned = spawned
        self.alive = True
        self.closed = False

    def close(self) -> dict:
        self.alive = False
        self.closed = True
        return {"ok": True, "pid": self.pid, "restored": True}

    def runtime_info(self) -> dict:
        return {"ok": True, "pid": self.pid}

    def wait_ready(self, timeout: float = 30.0,
                   preferred_module: str = "",
                   settle: float | None = None) -> dict:
        """ensure_runtime 会在 attach 之后等 IL2CPP 就绪。假的直接报就绪。

        `settle` 必须收下：真实现的签名里有它（spawn 路径第一次探测前的静默，
        见 `runtime/session.py` 的 `READY_SETTLE_DELAY`）。
        桩要跟着真签名走 —— 否则产品一加参数，测试就报 TypeError，
        把「桩落后了」伪装成「产品坏了」。
        """
        return {"ok": True, "pid": self.pid, "module": "FakeAssembly.dll",
                "base": "0x10000000", "api_available": [], "api_missing": []}

    def __repr__(self) -> str:
        return f"<FakeRuntime pid={self.pid}>"


def reset() -> None:
    """把单例会话恢复到「干净、无运行时」状态。"""
    st = S.state()
    st.close_runtime()
    st.runtime = None
    st.notes.clear()


def main() -> int:
    print("会话目标选择验证 —— 用假 RuntimeSession，不注入真实进程")

    st = S.state()
    st.exe = "fake.exe"

    # ---------------------------------------------------------------- 1
    show("1. 无会话 + 显式 pid → 应尝试 attach 该 pid（而不是跳过）")
    reset()
    attempted: list[int] = []

    import gamemod_mcp.runtime as rt_mod

    real_attach = rt_mod.attach

    def fake_attach(pid, name="", timeout=10.0, init=True,
                    preferred_module=""):
        attempted.append(pid)
        return FakeRuntime(pid)

    rt_mod.attach = fake_attach
    # ensure_runtime 内部用 `from ..runtime import attach`，是模块级名字查找，
    # 所以必须替换 runtime 模块上的同名属性（上面已做）。
    try:
        res = S.ensure_runtime(pid=4242, auto_find=False)
        check("尝试 attach 传入的 pid", attempted == [4242], f"attempted={attempted}")
        check("返回 ok", res.get("ok") is True, str(res.get("error", ""))[:80])
        check("返回的 pid 就是传入的 pid",
              (res.get("runtime") or {}).get("pid") == 4242,
              f"runtime.pid={(res.get('runtime') or {}).get('pid')}")

        # ------------------------------------------------------------ 2
        show("2. 【缺陷核心】已有 pid=A，再指定 pid=B → 必须切到 B，不许静默返回 A")
        attempted.clear()
        res2 = S.ensure_runtime(pid=7777, auto_find=False)
        got = (res2.get("runtime") or {}).get("pid")
        check("确实尝试了 attach pid=7777", attempted == [7777],
              f"attempted={attempted}")
        check("返回的 pid 是 7777（修复前会返回 4242）", got == 7777,
              f"got={got}")
        check("不是 reused", res2.get("reused") is not True,
              f"reused={res2.get('reused')}")
        check("旧会话已被断开", st.runtime is not None and st.runtime.pid == 7777)

        # ------------------------------------------------------------ 3
        show("3. 同 pid 重复指定 → 复用（不重复注入）")
        attempted.clear()
        res3 = S.ensure_runtime(pid=7777, auto_find=False)
        check("没有再次 attach", attempted == [], f"attempted={attempted}")
        check("标记为 reused", res3.get("reused") is True, str(res3.get("reused")))

        # ------------------------------------------------------------ 4
        show("4. 不指定目标 → 复用现有会话（懒注入的正常路径）")
        attempted.clear()
        res4 = S.ensure_runtime(auto_find=False)
        check("没有 attach 新进程", attempted == [], f"attempted={attempted}")
        check("复用现有会话", res4.get("reused") is True,
              f"pid={(res4.get('runtime') or {}).get('pid')}")

        # ------------------------------------------------------------ 5
        show("5. 【缺陷核心】已有会话时 spawn → 必须拒绝，不许起第二个进程")
        spawned: list[str] = []
        real_spawn = rt_mod.spawn_and_attach
        rt_mod.spawn_and_attach = lambda exe, **kw: (
            spawned.append(exe), FakeRuntime(9999, spawned=True))[1]
        try:
            res5 = S.ensure_runtime(spawn=True, launcher_exe="D:/fake/game.exe")
            check("被拒绝（ok=False）", res5.get("ok") is False,
                  f"ok={res5.get('ok')}")
            check("没有真的 spawn", spawned == [], f"spawned={spawned}")
            check("错误信息说明了原因",
                  "拒绝" in str(res5.get("error", "")),
                  str(res5.get("error", ""))[:60])
            check("给出了出路（hint 提到 close_session）",
                  "close_session" in str(res5.get("hint", "")),
                  str(res5.get("hint", ""))[:60])
            check("现有会话未被破坏", st.runtime is not None
                  and st.runtime.pid == 7777)

            # -------------------------------------------------------- 6
            show("6. 先关会话再 spawn → 应正常启动（拒绝不是永久性的）")
            st.close_runtime()
            st.runtime = None
            attempted.clear()
            res6 = S.ensure_runtime(spawn=True, launcher_exe="D:/fake/game.exe")
            check("确实 spawn 了", spawned == ["D:/fake/game.exe"],
                  f"spawned={spawned}")
            check("返回 ok", res6.get("ok") is True, str(res6.get("error", ""))[:60])
            check("新会话就是 spawn 出来的 pid",
                  (res6.get("runtime") or {}).get("pid") == 9999,
                  f"pid={(res6.get('runtime') or {}).get('pid')}")
        finally:
            rt_mod.spawn_and_attach = real_spawn
    finally:
        rt_mod.attach = real_attach

    # ---------------------------------------------------------------- 7
    show("7. 会话已失效（alive=False）→ 不应被当成「已有会话」复用")
    reset()
    dead = FakeRuntime(31337)
    dead.alive = False
    st.runtime = dead
    attempted.clear()
    rt_mod.attach = fake_attach
    try:
        res7 = S.ensure_runtime(pid=5150, auto_find=False)
        check("失效会话不阻挡新注入", attempted == [5150], f"attempted={attempted}")
        check("拿到的是新 pid", (res7.get("runtime") or {}).get("pid") == 5150,
              f"pid={(res7.get('runtime') or {}).get('pid')}")
    finally:
        rt_mod.attach = real_attach

    reset()

    # ---------------------------------------------------------------- 8
    show("8. spawn 后的就绪等待预算按引擎给（有托管运行时探针的引擎才等）")

    # 纯函数层：IL2CPP 与 Mono 都给 60 秒（探针都有对应后端），其余 2 秒
    print(f"  ready_timeout_for('il2cpp')={S.ready_timeout_for('il2cpp')} "
          f"('IL2CPP')={S.ready_timeout_for('IL2CPP')} "
          f"('mono')={S.ready_timeout_for('mono')} "
          f"('ue')={S.ready_timeout_for('ue')} ('')={S.ready_timeout_for('')} "
          f"(None)={S.ready_timeout_for(None)}")
    check("il2cpp → 60 秒", S.ready_timeout_for("il2cpp") == 60.0,
          str(S.ready_timeout_for("il2cpp")))
    check("大小写不敏感（'IL2CPP' 也认）",
          S.ready_timeout_for("IL2CPP") == 60.0,
          str(S.ready_timeout_for("IL2CPP")))
    # 订正：Mono 曾经是 2 秒，理由是「Mono 没有探针路径，等也白等」。
    # 探针补上 Mono 后端之后这个理由不再成立 —— 等下去**真的会**就绪。
    check("mono → 60 秒（探针有 Mono 后端，等下去真的会就绪）",
          S.ready_timeout_for("mono") == 60.0, str(S.ready_timeout_for("mono")))
    check("ue → 2 秒（UE 永远没有托管运行时模块，等满 60 秒纯空转）",
          S.ready_timeout_for("ue") == 2.0,
          str(S.ready_timeout_for("ue")))
    check("引擎未知 → 2 秒（不拿 60 秒赌它是托管游戏）",
          S.ready_timeout_for("") == 2.0 and S.ready_timeout_for(None) == 2.0,
          f"{S.ready_timeout_for('')}/{S.ready_timeout_for(None)}")
    # 提示语必须按引擎说，不能在 Mono 游戏上说「IL2CPP 未就绪」
    print(f"  runtime_label: il2cpp={S.runtime_label('il2cpp')!r} "
          f"mono={S.runtime_label('mono')!r} ue={S.runtime_label('ue')!r} "
          f"None={S.runtime_label(None)!r}")
    check("提示语按引擎给（Mono 不说 IL2CPP）",
          S.runtime_label("mono") == "Mono" and S.runtime_label("il2cpp") == "IL2CPP",
          f"{S.runtime_label('mono')}/{S.runtime_label('il2cpp')}")
    check("非托管引擎的提示语不冒充某个具体引擎",
          "IL2CPP" not in S.runtime_label("ue")
          and "Mono" not in S.runtime_label("ue"),
          S.runtime_label("ue"))

    # 端到端层：确认 ensure_runtime 真的把预算传下去了（不是写死 60 秒）
    seen: list[float] = []
    settles: list[float | None] = []

    class TimeoutSpy(FakeRuntime):
        def wait_ready(self, timeout: float = 30.0,
                       preferred_module: str = "",
                       settle: float | None = None) -> dict:
            seen.append(timeout)
            # 顺便钉住：attach 路径必须显式关掉 settle（已启动的进程
            # 没有「启动头几秒」那个危险窗口，白等 3 秒纯属浪费）。
            settles.append(settle)
            return {"ok": True, "pid": self.pid, "module": "FakeAssembly.dll"}

    rt_mod.spawn_and_attach = lambda exe, **kw: TimeoutSpy(8888, spawned=True)
    try:
        # Mono 也走 60 秒：它的探针有运行时后端，等下去**真的会**就绪
        # （曾经给 2 秒，理由是「Mono 没有运行时探针，等也白等」——该理由已不成立）。
        for eng, want in (("mono", 60.0), ("il2cpp", 60.0), ("ue", 2.0)):
            reset()
            st.engine = eng
            res = S.ensure_runtime(spawn=True, launcher_exe="D:/fake/game.exe")
            got = seen[-1] if seen else None
            print(f"  engine={eng:<7} spawn 后 wait_ready(timeout={got}) "
                  f"ok={res.get('ok')}")
            check(f"engine={eng} → 传下去的 timeout 是 {want}", got == want,
                  f"got={got}")
    finally:
        rt_mod.spawn_and_attach = real_spawn
    st.engine = ""
    reset()

    # spawn 路径必须**用默认 settle**（第一次探测前先静默），
    # 因为「游戏启动头几秒」正是卡死窗口（对照实验见报告 §7.3）。
    check("spawn 路径没有把 settle 显式关掉（危险窗口要避开）",
          all(s is None for s in settles),
          f"settles={settles}")

    # attach 路径必须显式 settle=0：注入的是**已经启动好**的进程，
    # 没有那个窗口，白等 3 秒纯属浪费。
    seen.clear()
    settles.clear()
    rt_mod.attach = lambda pid, **kw: TimeoutSpy(pid)
    try:
        reset()
        st.engine = "il2cpp"
        S.ensure_runtime(pid=7777)
    finally:
        rt_mod.attach = real_attach      # 复原，别污染后面的收尾
    check("attach 路径显式传 settle=0（不白等）",
          settles and settles[-1] == 0.0, f"settles={settles}")
    reset()

    print()
    print("=" * 74)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print(f"  - {f}")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    _sys.exit(main())
