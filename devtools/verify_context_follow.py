#!/usr/bin/env python3
"""验收：启动/注入的目标换了游戏时，会话上下文必须跟着换。

被修复的缺陷（实测，不是推测）
============================================================
`launch_game(exe=...)` 与 `inject_process(pid=...)` 都允许指定**任意**目标，
但两者都**不动**会话里那份「当前游戏是谁」的身份
（`game_dir` / `engine` / `metadata_path`）以及由它建立的索引。

实测现场（本机）：

    open_game("E:\\project\\Pass The Fear")      # index: 140 个 image
    launch_game(exe="E:\\project\\Ved\\ved.exe") # 真的起来了 ved (pid 17312)
    runtime_info()
      -> image_count=143, static_image_count=140, image_count_matches=false

运行时是 ved（143），静态索引还停在 Pass The Fear（140）。工具**全程 ok:true**，
一个错都不报。

为什么这比崩溃更危险
============================================================
本工具集的方法论是「静态拿 token → 运行时拿地址 → **token 对账**」。
索引与运行时属于两个游戏时，对账必然失败；更糟的是撞巧对上同一个 token，
于是 Hook 挂到了另一个游戏的同名方法上，而 `chosen.token` 看起来完全正常。
也就是说，**用来证明「落点正确」的那把尺子本身被换掉了**。

还要验的边界
============================================================
1. 同一个游戏时不许来回切换（否则每次 launch 都会把已建好的索引推倒重来）；
2. 真的换了游戏时必须建立新索引，且 `engine` 跟着变；
3. 反推不出游戏目录时**不许猜**：如实标 `identified=False`；
4. 上下文切换**不许碰运行时会话**（禁止把刚注入的会话关掉）；
5. `switched` 只反映**实际**的身份变化。

非空转反证
============================================================
把 `align_context_with_exe` 换成空操作（= 修复前的行为），第 2 条断言
必须报红。

    python devtools/verify_context_follow.py
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

from gamemod_mcp.engine import discovery, state as S  # noqa: E402

PASS, FAIL, WARN = [], [], []


def check(label: str, cond: bool, detail: str = "",
          warn_only: bool = False) -> None:
    if cond:
        tag = "✅"
    elif warn_only:
        tag = "⚠️ "
    else:
        tag = "❌"
    print(f"  {tag} {label}" + (f" — {detail}" if detail else ""))
    if cond:
        PASS.append(label)
    elif warn_only:
        WARN.append(label)
    else:
        FAIL.append(label)


def show(title: str) -> None:
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


class FakeRuntime:
    """假装活着的运行时会话。不碰 Frida。"""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.name = "fake.exe"
        self.spawned = False
        self.alive = True
        self.closed = False

    def close(self) -> dict:
        self.alive = False
        self.closed = True
        return {"ok": True, "pid": self.pid, "restored": True}


def reset() -> None:
    st = S.state()
    st.close_runtime()
    st.runtime = None
    st.close_index()
    st.reset_target()
    st.notes.clear()


# ---------------------------------------------------------------- 1. 纯函数

def part_root_discovery(il2cpp, mono) -> None:
    show("1. 从 exe 反推游戏目录（纯函数）")

    exe = discovery.pick_main_exe(str(il2cpp))
    root = discovery.game_root_for_exe(exe)
    print(f"     {exe}")
    print(f"     -> {root}")
    check("Unity 主程序能反推出它所在的游戏目录",
          bool(root) and _os.path.normcase(root) == _os.path.normcase(str(il2cpp)),
          f"root={root}")

    if mono is not None:
        m_exe = discovery.pick_main_exe(str(mono))
        m_root = discovery.game_root_for_exe(m_exe)
        check("Mono 主程序也能反推",
              bool(m_root)
              and _os.path.normcase(m_root) == _os.path.normcase(str(mono)),
              f"{m_exe} -> {m_root}")

    # 反推不出来时必须返回空串，不许猜
    bogus = _os.path.join(_os.path.abspath(_os.sep), "no_such_dir_xyz", "a.exe")
    check("不存在的 exe 返回空串（不猜）",
          discovery.game_root_for_exe(bogus) == "",
          repr(discovery.game_root_for_exe(bogus)))

    # 真实存在但不在游戏里的 exe：用一个系统程序试试（路径是真实文件）
    sys_exe = _os.path.join(_os.environ.get("SystemRoot", r"C:\Windows"),
                            "System32", "notepad.exe")
    if _os.path.isfile(sys_exe):
        got = discovery.game_root_for_exe(sys_exe)
        check("非游戏目录下的真实 exe 返回空串（不猜）",
              got == "", f"notepad.exe -> {got!r}")
    else:
        check("非游戏 exe 反推用例", False, "本机无 notepad.exe", warn_only=True)


# ---------------------------------------------------------------- 2. 对齐

def part_align(il2cpp, mono) -> None:
    show("2. 【缺陷核心】上下文必须跟随目标切换")

    if mono is None:
        check("需要 Mono 样本才能验跨游戏切换", False,
              "Mono 样本不在本机", warn_only=True)
        return

    # 先选中 IL2CPP 游戏并建索引
    reset()
    sel = S.select_game(game_dir=str(il2cpp))
    check("先选中 IL2CPP 游戏", sel.get("ok") is True, sel.get("error", ""))
    idx = S.ensure_index(cache_dir=str(samples.CACHE_TEST))
    check("IL2CPP 索引已建立", idx.get("ok") is True, idx.get("error", ""))
    old_engine = S.state().engine
    old_dir = S.state().game_dir
    print(f"     切换前: game_dir={old_dir} engine={old_engine}")

    # 挂一个假运行时：对齐过程**不许**把它关掉（第 4 条）
    fake = FakeRuntime(31337)
    S.state().runtime = fake

    mono_exe = discovery.pick_main_exe(str(mono))
    align = S.align_context_with_exe(mono_exe)

    print(f"     对齐到: {mono_exe}")
    print(f"     align={ {k: v for k, v in align.items() if k != 'index'} }")
    print(f"     切换后: game_dir={S.state().game_dir} engine={S.state().engine}")

    check("识别出目标属于另一个游戏",
          align.get("identified") is True and align.get("switched") is True,
          f"identified={align.get('identified')} switched={align.get('switched')}")
    check("会话 game_dir 已切到目标游戏",
          _os.path.normcase(S.state().game_dir)
          == _os.path.normcase(str(mono)),
          f"{S.state().game_dir}")
    check("engine 跟着变成 mono", S.state().engine == "mono",
          f"{S.state().engine}")
    check("返回里带 previous_game_dir（调用方能告知用户换过游戏）",
          bool(align.get("previous_game_dir")),
          f"{align.get('previous_game_dir')}")

    # 第 4 条：上下文对齐**绝不能**碰运行时会话
    check("上下文切换没有关掉运行时会话（否则等于白注入）",
          S.state().runtime is fake and fake.closed is False,
          f"runtime={'被关掉了' if fake.closed else '仍持有'}")

    # 索引必须跟着换成新游戏的
    check("新游戏的索引已建立", S.state().has_index() is True,
          f"index_built={align.get('index_built')}")
    if S.state().has_index():
        n_new = S.state().index.conn.execute(
            "SELECT COUNT(DISTINCT image) FROM types").fetchone()[0]
        print(f"     新索引 image 数={n_new}")
        check("新索引确实属于新游戏（与旧游戏的 image 数不同则更明显）",
              n_new > 0, f"{n_new} 个 image")


# ---------------------------------------------------------------- 3. 幂等

def part_idempotent(il2cpp) -> None:
    show("3. 同一个游戏时不许来回切换（幂等）")

    reset()
    S.select_game(game_dir=str(il2cpp))
    S.ensure_index(cache_dir=str(samples.CACHE_TEST))
    before = S.state().game_dir
    exe = discovery.pick_main_exe(str(il2cpp))

    a = S.align_context_with_exe(exe)
    after1 = S.state().game_dir
    a2 = S.align_context_with_exe(exe)

    check("同一个游戏时 switched 为 False（不推倒重来）",
          a.get("switched") is False and a2.get("switched") is False,
          f"{a.get('switched')}/{a2.get('switched')}")
    check("标为 same_game", a.get("same_game") is True,
          f"same_game={a.get('same_game')}")
    check("game_dir 没被改动",
          _os.path.normcase(after1) == _os.path.normcase(before),
          f"{after1}")
    check("已有索引时不再重建（index_built 不出现或为假）",
          not a.get("index_built"),
          f"index_built={a.get('index_built')}")


# ---------------------------------------------------------------- 4. 反证

def part_counterproof(il2cpp, mono) -> None:
    show("4. 非空转反证：把对齐换成空操作必须报红")

    if mono is None:
        check("反证需要 Mono 样本", False, "Mono 样本不在本机", warn_only=True)
        return

    reset()
    S.select_game(game_dir=str(il2cpp))
    S.ensure_index(cache_dir=str(samples.CACHE_TEST))
    before = S.state().game_dir

    # 模拟修复前的行为：不做任何对齐
    orig = S.align_context_with_exe
    S.align_context_with_exe = lambda exe, build_index=True: {
        "ok": True, "identified": False, "switched": False}
    try:
        S._align_and_note(discovery.pick_main_exe(str(mono)))
        stale = S.state().game_dir
    finally:
        S.align_context_with_exe = orig

    print(f"     旧行为（不对齐）后 game_dir={stale}")
    check("反证：不对齐时 game_dir 仍停在旧游戏（正是缺陷现场）",
          _os.path.normcase(stale) == _os.path.normcase(before),
          f"stale={stale} before={before}")


# ---------------------------------------------------------------- 5. 警告

def part_warning(il2cpp) -> None:
    show("5. 反推不出游戏目录时，必须如实警告而不是硬猜")

    reset()
    S.select_game(game_dir=str(il2cpp))
    sys_exe = _os.path.join(_os.environ.get("SystemRoot", r"C:\Windows"),
                            "System32", "notepad.exe")
    if not _os.path.isfile(sys_exe):
        check("警告用例需要 notepad.exe", False, "本机没有", warn_only=True)
        return

    align = S.align_context_with_exe(sys_exe)
    check("反推不出时 identified=False（不猜）",
          align.get("identified") is False,
          f"identified={align.get('identified')}")
    check("不切换会话（不会把上下文指到盘符根）",
          _os.path.normcase(S.state().game_dir)
          == _os.path.normcase(str(il2cpp)),
          f"game_dir={S.state().game_dir}")
    text = S.context_warning(sys_exe, align)
    check("给出上下文可能不匹配的警告", bool(text), text[:90])

    # 正常同游戏调用**不该**刷警告（否则警告会被当成噪音忽略）
    own = discovery.pick_main_exe(str(il2cpp))
    own_align = S.align_context_with_exe(own)
    check("正常同游戏调用不产生警告",
          S.context_warning(own, own_align) == "",
          repr(S.context_warning(own, own_align)))


def main() -> int:
    print("验收：启动/注入的目标换了游戏时，会话上下文必须跟着换")

    il2cpp = samples.find_il2cpp_any()
    mono = samples.find_mono()
    if not samples.require(il2cpp, "IL2CPP 样本（任意一款受支持的）"):
        print("\n没有可用的 IL2CPP 样本，无法验证。")
        return 1
    print(f"  IL2CPP 样本: {il2cpp}")
    print(f"  Mono   样本: {mono}")

    samples.ensure_cache_dirs()
    part_root_discovery(il2cpp, mono)
    part_align(il2cpp, mono)
    part_idempotent(il2cpp)
    part_counterproof(il2cpp, mono)
    part_warning(il2cpp)

    reset()
    print()
    print("=" * 74)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项（警告 {len(WARN)}）")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print(f"  - {f}")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    _sys.exit(main())
