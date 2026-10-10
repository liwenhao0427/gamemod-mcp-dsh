#!/usr/bin/env python3
"""A/B 实验：探针「附着 IL2CPP 域」的就绪判据该有多严？

## 要回答的问题

`docs/重启验证与运行时理解报告.md` §4.2 记录了一个**间歇性**卡死：
探针在游戏早期初始化阶段调 `il2cpp_thread_attach` 之后，游戏窗口标题变成
`Fatal error in GC`，探针的同步 RPC 永不返回。

隔离实验（同报告 §7.2）已把触发条件缩到「**resume 后立刻轮询 init**」
这条路上（形态 E），而「resume 后先等 25 秒再加载探针」（形态 F）正常。
所以怀疑点是：**域看起来可读、但 GC 还没稳定时就附着**，
把一条错误的线程记录登记进了 GC —— 之后 GC 跑到它就 fatal。

`agent.js` 现在的判据（`domainPointer`）只做一件事：试读域指针指向的第一个指针。
本次实验给它加一条**更严的**判据：要求 `il2cpp_domain_get_assemblies`
返回非空且数量合理（运行时就绪前程序集清单是空的），然后对照两臂的卡死率。

## 为什么要跑多轮

这是**竞态**：形态 E 与 G 是同一条代码路径，一次卡死、一次正常。
AGENTS.md 与本报告都记过同类教训（「样本存活结论必须先确认观察窗口足够长」）。
所以本脚本只报**比率**，不报单次结果，并且在样本量不足时明确说「不足以下结论」。

## 判据（不靠猜）

| 结果 | 怎么判 |
|---|---|
| 卡死 | 某次 `init` 超过 `RPC_TIMEOUT` 未返回（看守线程判定），**或**窗口标题含 `Fatal error` |
| 正常 | `init` 就绪且窗口标题到达 `VED` |

## 用法

    python .cache-test\\wedge_ab.py baseline 6      # 现状
    python .cache-test\\wedge_ab.py strict   6      # 更严的就绪判据
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import frida

EXE = r"E:\project\Ved\ved.exe"
CWD = r"E:\project\Ved"
AGENT = Path(r"E:\project\GameModMCP\src\gamemod_mcp\runtime\agent.js")
OUT = Path(__file__).with_suffix(".jsonl")
_u32 = ctypes.windll.user32

#: 单次 init 的等待上限。产品默认 30 秒；实验里压到 12 秒 ——
#: 卡死是「永不返回」，12 秒足够区分，且能把每轮耗时压下来。
RPC_TIMEOUT = 12.0
#: 每轮观察窗口：够游戏从 resume 走到主窗口（实测约 10~20 秒）。
OBSERVE = 45.0
#: 两次 init 之间的间隔（秒）。产品 `wait_ready` 用 0.5；
#: 实验默认 0（**紧循环**，把竞态窗口放到最大）。
#: 为什么要能调：baseline 紧循环 8/8 卡死，而产品（0.5s 间隔）只是间歇卡死 ——
#: 必须把「轮询密度」与「附着时机」两个因素分开，否则会把
#: 「我的测试脚本太激进」误判成「产品必然卡死」。
INTERVAL = float(os.environ.get("INTERVAL", "0"))
#: 是否复刻产品的「子进程输出转发」（`_hook_child_output`）。
#: 默认关：本实验要把它当作**独立变量**单独验证。
HOOK_OUTPUT = os.environ.get("HOOK_OUTPUT", "") not in ("", "0", "false")
#: resume 之后**先等多久**再发第一次 init（秒）。
#: 用来把「早期启动窗口」这个因素单独拎出来：若 interval=0 时加一点初始延迟
#: 就能避免卡死，说明危险窗口在游戏启动的头几秒，而不是「调用次数多」本身。
DELAY = float(os.environ.get("DELAY", "0"))

#: 更严的就绪判据：要求程序集清单可用且数量合理。
OLD_DOMAIN = """    function domainPointer() {
        try {
            var d = funcs.il2cpp_domain_get();
            if (!d || d.isNull()) { return null; }
            d.readPointer();
            return d;
        } catch (e) {
            return null;
        }
    }"""

STRICT_DOMAIN = """    function domainPointer() {
        try {
            var d = funcs.il2cpp_domain_get();
            if (!d || d.isNull()) { return null; }
            d.readPointer();
            // 更严：运行时就绪前程序集清单是空的，这里要求它已经可用。
            // 目的：避免在 GC 还没稳定时就把探针线程附着进去
            // （那会在 GC 里留下一条坏记录，之后 GC 跑到它就 fatal）。
            if (funcs.il2cpp_domain_get_assemblies) {
                var cp = Memory.alloc(Process.pointerSize);
                cp.writeU32(0);
                var asms = funcs.il2cpp_domain_get_assemblies(d, cp);
                var n = cp.readU32();
                if (!asms || asms.isNull() || n <= 0 || n > 100000) {
                    return null;
                }
            }
            return d;
        } catch (e) {
            return null;
        }
    }"""


def window_title(pid: int) -> str:
    found: list[str] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _lp):
        w = ctypes.c_ulong()
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(w))
        if w.value == pid:
            n = _u32.GetWindowTextLengthW(hwnd)
            if n:
                b = ctypes.create_unicode_buffer(n + 1)
                _u32.GetWindowTextW(hwnd, b, n + 1)
                if b.value.strip():
                    found.append(b.value)
        return True

    _u32.EnumWindows(cb, None)
    return found[0] if found else ""


def kill_game() -> None:
    subprocess.run(["taskkill", "/IM", "ved.exe", "/F"], capture_output=True)


def agent_source(arm: str) -> str:
    src = AGENT.read_text(encoding="utf-8")
    if arm == "strict":
        if OLD_DOMAIN not in src:
            raise SystemExit("agent.js 的 domainPointer 与预期不符，"
                             "本实验的替换锚点已失效 —— 请先更新锚点再跑。")
        src = src.replace(OLD_DOMAIN, STRICT_DOMAIN)
    return src


def one_round(arm: str, i: int) -> dict:
    """形态 E：spawn → resume → **立刻**加载探针并轮询 init。"""
    rec: dict = {"arm": arm, "round": i, "wedged": False, "ready": False,
                 "title": "", "init_calls": 0, "interval": INTERVAL}
    kill_game()
    time.sleep(4)
    dev = frida.get_local_device()
    # 复刻产品 `_hook_child_output`：注册 device 级 output 回调并把子进程
    # stdout 转成 stderr 日志。**这是本实验要单独验证的变量** ——
    # 产品（有回调）在 interval=0.5 下卡死过两次，而本脚本（无回调）
    # 在同一个 interval 下 8/8 正常，所以怀疑「消费子进程输出的速度」
    # 会让游戏在启动早期阻塞在 write 上。
    if HOOK_OUTPUT:
        import sys as _sys

        def _on_output(pid, fd, data):
            try:
                text = (data.decode("utf-8", "replace")
                        if isinstance(data, (bytes, bytearray)) else str(data))
                for line in text.splitlines():
                    if line.strip():
                        print(f"    [game fd={fd}] {line.strip()[:110]}",
                              file=_sys.stderr, flush=True)
            except Exception:
                pass
        dev.on("output", _on_output)
        rec["hooked_output"] = True
    pid = dev.spawn([EXE], cwd=CWD, stdio="pipe")
    sess = dev.attach(pid)
    script = sess.create_script(agent_source(arm), name="gamemod-probe")
    script.load()
    # **先 resume**：这是 `launch_game` 的真实顺序，也是形态 E 的定义
    dev.resume(pid)
    t0 = time.time()
    if DELAY > 0:
        time.sleep(DELAY)
        rec["delay"] = DELAY

    deadline = time.time() + OBSERVE
    while time.time() < deadline:
        rec["init_calls"] += 1
        box: list = []

        def _call() -> None:
            try:
                box.append(script.exports_sync.init(None))
            except Exception as exc:  # noqa: BLE001
                box.append({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

        th = threading.Thread(target=_call, daemon=True)
        th.start()
        th.join(timeout=RPC_TIMEOUT)
        if th.is_alive():
            rec["wedged"] = True
            rec["title"] = window_title(pid)
            rec["seconds"] = round(time.time() - t0, 1)
            break
        r = box[0] if box else {"ok": False, "error": "无返回"}
        if r.get("ok"):
            rec["ready"] = True
            break
        # 间隔可调：0 = 紧循环（把竞态窗口放到最大）。
        # 产品 `wait_ready` 用的是 0.5 秒，所以这个数字必须能对上，
        # 否则测出来的是「脚本多激进」而不是「产品会不会卡」。
        if INTERVAL > 0:
            time.sleep(INTERVAL)
    else:
        rec["title"] = window_title(pid)

    if not rec["wedged"] and rec["ready"]:
        # 就绪后再确认游戏确实走到了主窗口
        t1 = time.time()
        while time.time() - t1 < 25:
            time.sleep(3)
            rec["title"] = window_title(pid)
            if rec["title"] == "VED":
                break
    rec["final_title"] = rec["title"]
    if not rec["wedged"] and "Fatal error" in (rec["title"] or ""):
        # init 返回了、但游戏仍然进了 Fatal error —— 同样算卡死
        rec["wedged"] = True
        rec["wedged_by"] = "title"
    # 收尾必须**有界**：探针卡死时 `script.unload()` / `session.detach()`
    # 同样是没有超时的同步 Frida 调用，会把本脚本一起挂住
    # （这正是 docs/重启验证与运行时理解报告.md §4.2 记的教训；
    #  第一版本脚本因此挂在这里，只能靠 taskkill 救）。
    # 卡死时直接放弃清理 —— 下一轮开头会 taskkill 掉游戏，不需要干净收尾。
    if not rec["wedged"]:
        try:
            _bounded(script.unload, 8.0)
            _bounded(sess.detach, 8.0)
        except Exception as exc:  # noqa: BLE001
            rec["cleanup_error"] = str(exc)
    else:
        rec["cleanup_skipped"] = "wedged"
    return rec


def _bounded(fn, timeout: float) -> None:
    """在有界时间内跑一个没有超时的 Frida 同步调用。"""
    th = threading.Thread(target=fn, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        raise TimeoutError(f"{getattr(fn, '__name__', fn)} 超过 {timeout:g}s")


def main() -> int:
    arm = sys.argv[1] if len(sys.argv) > 1 else "baseline"
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 6
    print(f"臂={arm} 轮数={rounds} RPC_TIMEOUT={RPC_TIMEOUT:g}s "
          f"OBSERVE={OBSERVE:g}s INTERVAL={INTERVAL:g}s "
          f"HOOK_OUTPUT={HOOK_OUTPUT} DELAY={DELAY:g}s")
    recs = []
    for i in range(1, rounds + 1):
        r = one_round(arm, i)
        recs.append(r)
        verdict = ("卡死" if r["wedged"]
                   else ("正常" if r["ready"] else "未就绪"))
        print(f"  第 {i}/{rounds} 轮: {verdict}  init次数={r['init_calls']} "
              f"标题={r['final_title']!r}", flush=True)
        # 逐轮落盘并 flush：本实验会**故意**触发卡死，万一脚本自己挂住，
        # 已经跑出来的数据不能丢（第一版就是丢在这里）。
        with open(OUT, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    kill_game()
    wedged = sum(1 for r in recs if r["wedged"])
    print(f"\n臂 {arm}: 卡死 {wedged}/{len(recs)}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log = open(Path(__file__).with_suffix(".log"), "a", encoding="utf-8")

    class _T:
        def __init__(self, *s): self.s = s
        def write(self, x):
            for st in self.s:
                try: st.write(x); st.flush()
                except Exception: pass
            return len(x)
        def flush(self):
            for st in self.s:
                try: st.flush()
                except Exception: pass
        def reconfigure(self, **k): pass

    log.write(f"\n##### {time.strftime('%H:%M:%S')} argv={sys.argv[1:]} #####\n")
    log.flush()
    sys.stdout = _T(sys.stdout, log)
    try:
        raise SystemExit(main())
    finally:
        log.flush()
