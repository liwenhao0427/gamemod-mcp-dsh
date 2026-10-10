# 验证「探针线程附着到 IL2CPP 域」的就绪判断与重试链路
#
# 背景（实测踩出来的缺陷，2026-xx）：
#   `GameAssembly.dll` 已加载、导出也能解析，**并不代表** IL2CPP 运行时初始化好了。
#   `il2cpp_init` 还没跑完时 `il2cpp_domain_get()` 返回**野指针**（实测 0x135），
#   把它喂给 `il2cpp_thread_attach` 就是 access violation。
#   老代码把这个异常当成「可降级的 fallback」吞掉继续跑，于是后续每次 il2cpp
#   调用都在**未附着的线程**上执行 → 游戏触发 IL2CPP 断言（0x80000003）当场崩溃。
#
#   观测证据：15 个样本游戏里 9 个 IL2CPP，其中 6 个的 init 带着
#   `il2cpp_thread_attach 调用失败：access violation accessing 0x135`，
#   且**恰好只有这 6 个**在运行中被杀掉（Windows 事件日志：
#   Meta Ghost.exe / PassTheFear.exe / Wanderburg.exe 的 GameAssembly.dll
#   0x80000003；Wanderburg 还起了 UnityCrashHandler64）。
#   对照组：同一个 Wanderburg.exe 不注入探针时 45 秒后仍活着（1710 MB）。
#
# 本脚本断言的就是这条链：
#   域没好 → init 返回**可重试**失败 → wait_ready 继续轮询 → 域好了 → 附着成功
#   → 后续 il2cpp 调用安全、游戏进程活着。
import sys, io, os, json, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import samples  # noqa: E402
samples.add_src_to_path()

from pathlib import Path  # noqa: E402

from gamemod_mcp.runtime import (  # noqa: E402
    RuntimeSession, spawn_and_attach, close_all_sessions, frida_version,
)
from gamemod_mcp.runtime import session as session_mod  # noqa: E402

FAILS: list[str] = []


def show(title):
    print()
    print("=" * 76)
    print(title)
    print("=" * 76)


def check(label, ok, detail=""):
    print(f"  {'✅' if ok else '❌'} {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)
    return ok


def pid_alive(pid):
    """进程是否还在 —— 直接问系统，不经过探针。"""
    import subprocess
    r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                       capture_output=True, text=True)
    return f'"{pid}"' in (r.stdout or "")


# ------------------------------------------------------------------ 0. 源码契约
show("0. agent.js 源码契约：不许再「静默降级」")

AGENT = (Path(session_mod.__file__).parent / "agent.js").read_text(encoding="utf-8")

# 旧写法：把附着失败塞进 fallbacks 后继续跑。这正是崩溃的根因。
OLD_SNIPPET = """        if (funcs.il2cpp_thread_attach) {
            try {
                var domain = funcs.il2cpp_domain_get();
                funcs.il2cpp_thread_attach(domain);
            } catch (e) {
                fallbacks.push('il2cpp_thread_attach 调用失败：' + e.message);
            }
        }"""


def has_silent_fallback(src: str) -> bool:
    """旧写法探测器：把附着异常写进 fallbacks 后继续。"""
    return ("fallbacks.push('il2cpp_thread_attach 调用失败" in src
            or "fallbacks.push('il2cpp_thread_attach 调用失败：" in src)


check("反证：探测器能在**旧代码**上命中（不是匹配空集合）",
      has_silent_fallback(OLD_SNIPPET))
check("产品代码里已没有「附着失败 → fallbacks」的静默降级",
      not has_silent_fallback(AGENT))
check("已加入域就绪判断 domainPointer()", "function domainPointer()" in AGENT)
check("域就绪判断会**试读**指针（野指针在这里暴露）",
      "d.readPointer();" in AGENT)
check("init 里域没好就拒绝继续（可重试失败，不是记一笔往下走）",
      "if (!dom) {" in AGENT
      and "return fail('IL2CPP 域尚未初始化" in AGENT)
check("附着失败同样拒绝继续（不是记一笔就往下走）",
      "无法把探针线程附着到 IL2CPP 域" in AGENT)

# 旧代码里 `var domain = funcs.il2cpp_domain_get();` 紧跟 thread_attach 的写法
# 必须消失 —— 否则「取域」和「附着」之间没有就绪检查。
check("不再直接拿 il2cpp_domain_get() 的返回值去 attach",
      "funcs.il2cpp_thread_attach(domain);" not in AGENT)

# ------------------------------------------------------------------ 1. 重试契约
show("1. wait_ready 的重试契约（桩对象，确定性）")


class StubSession(RuntimeSession):
    """只覆盖 init()：前 N 次失败，之后成功。"""

    def __init__(self, fail_times, timeout_at=None):
        self.fail_times = fail_times
        self.calls = 0
        self.timeout_at = timeout_at

    def init(self, preferred_module=""):
        self.calls += 1
        if self.calls <= self.fail_times:
            return {"ok": False,
                    "error": "IL2CPP 域尚未初始化（il2cpp_domain_get 还没返回有效指针）",
                    "detail": {"retryable": True}}
        return {"ok": True, "module": "GameAssembly.dll", "fallbacks": []}


stub = StubSession(fail_times=2)
t0 = time.time()
res = stub.wait_ready(timeout=10.0, interval=0.05)
elapsed = time.time() - t0
print(f"  失败 2 次后成功：attempts={res.get('attempts')} ok={res.get('ok')} "
      f"waited={res.get('waited')} 实际耗时={elapsed:.2f}s")
check("失败两次后成功，且 attempts 如实记到 3", res.get("attempts") == 3)
check("成功返回里没有 timed_out", not res.get("timed_out"))
check("等待时间如实记录（> 0）", (res.get("waited") or 0) > 0)

stub2 = StubSession(fail_times=10_000)
t0 = time.time()
res2 = stub2.wait_ready(timeout=0.6, interval=0.1)
elapsed2 = time.time() - t0
print(f"  一直失败：attempts={res2.get('attempts')} ok={res2.get('ok')} "
      f"timed_out={res2.get('timed_out')} 实际耗时={elapsed2:.2f}s")
check("一直失败会超时并如实标注", res2.get("ok") is False
      and res2.get("timed_out") is True)
check("超时前确实重试了多次（不是试一次就放弃）", (res2.get("attempts") or 0) >= 4,
      f"attempts={res2.get('attempts')}")

# 反证：把「模块未找到」当成终局会怎样 —— 那正是这一轮踩到的坑。
# 刚 spawn 完第一次 init 常常就是「未找到 IL2CPP 运行时模块」
# （GameAssembly.dll 还没加载完），所以它**必须**可重试。


class StubModuleLate(RuntimeSession):
    """前两次报「模块未找到」（DLL 还没加载完），第三次才成功。"""

    def __init__(self):
        self.calls = 0

    def init(self, preferred_module=""):
        self.calls += 1
        if self.calls <= 2:
            return {"ok": False,
                    "error": "未找到 IL2CPP 运行时模块（已尝试 GameAssembly.dll …）"}
        return {"ok": True, "module": "GameAssembly.dll", "fallbacks": []}


stub_late = StubModuleLate()
res_late = stub_late.wait_ready(timeout=5.0, interval=0.05)
print(f"  「模块未找到」两次后成功：attempts={res_late.get('attempts')} "
      f"ok={res_late.get('ok')} module={res_late.get('module')}")
check("「模块未找到」不会被当成终局（会继续等到 DLL 加载完）",
      res_late.get("ok") is True and res_late.get("attempts") == 3,
      f"attempts={res_late.get('attempts')} ok={res_late.get('ok')}")

# ------------------------------------------------------------------ 2. 真实游戏
show("2. 真实 IL2CPP 游戏：密集轮询 init，看每一次的结果")

game = samples.find_il2cpp()
if game is None:
    print("  ⚠️ 无法验证：samples.find_il2cpp() 找不到样本游戏")
else:
    exe = samples.game_exe(game)
    print(f"  样本: {game}")
    print(f"  主程序: {exe}")
    print(f"  Frida: {frida_version()}")
    sess = None
    try:
        # init=False：先只注入，**不**等就绪，这样第一次 init 就落在
        # 「模块可能刚加载、域还没建好」的窗口里 —— 正是缺陷发生的时刻。
        sess = spawn_and_attach(str(exe), cwd=str(Path(exe).parent), init=False)
        pid = sess.pid
        print(f"  已启动并注入 pid={pid}（未等待就绪）")

        attempts = []
        ready = None
        deadline = time.time() + 60.0
        while time.time() < deadline:
            attempts.append(sess.init())
            if attempts[-1].get("ok"):
                ready = attempts[-1]
                break
            time.sleep(0.1)

        print(f"  init 尝试次数: {len(attempts)}")
        for i, a in enumerate(attempts[:12], 1):
            fb = a.get("fallbacks") or []
            print(f"    #{i:<3} ok={str(a.get('ok')):<5} "
                  f"fallbacks={json.dumps(fb, ensure_ascii=False)[:110]} "
                  f"error={str(a.get('error'))[:90]}")
        if len(attempts) > 12:
            print(f"    …（另有 {len(attempts) - 12} 次）")

        # 缺陷的直接特征：ok=True 但 fallbacks 里带着附着失败。
        bogus = [a for a in attempts
                 if a.get("ok") and any("thread_attach" in str(x)
                                        for x in (a.get("fallbacks") or []))]
        check("没有任何一次「ok=True 却带着附着失败 fallback」", not bogus,
              f"命中 {len(bogus)} 次" if bogus else "")
        check("最终 init 成功", ready is not None)
        if ready is not None:
            print(f"  就绪信息: {json.dumps({k: v for k, v in ready.items() if k != 'images'}, ensure_ascii=False)[:220]}")
            check("就绪时 fallbacks 里没有附着失败",
                  not any("thread_attach" in str(x)
                          for x in (ready.get("fallbacks") or [])))
        notready = [a for a in attempts
                    if not a.get("ok") and "域尚未初始化" in str(a.get("error"))]
        print(f"  观测到「域尚未初始化」的可重试失败 {len(notready)} 次"
              + ("（说明修复路径真的被走到了）" if notready else
                 "（本次没撞上那个时间窗，属正常：竞态依赖机器速度）"))

        # ---- 3. 附着之后：il2cpp 调用必须能用，且游戏不能被搞崩
        show("3. 附着之后的 il2cpp 调用（崩溃回归断言）")
        alive_before = pid_alive(pid)
        check("调用前进程活着", alive_before)

        ci = sess.class_info(name="Time", namespace="UnityEngine")
        print(f"  class_info(UnityEngine.Time): ok={ci.get('ok')} "
              f"字段={len(ci.get('fields') or [])} 方法={len(ci.get('methods') or [])}")
        check("class_info 成功", bool(ci.get("ok")), str(ci.get("error"))[:90])
        check("class_info 后进程仍活着", pid_alive(pid))

        mt = sess.method_target("UnityEngine", "Time", "get_frameCount")
        cands = mt.get("candidates") or []
        print(f"  method_target(get_frameCount): ok={mt.get('ok')} "
              f"候选={len(cands)}")
        if cands:
            print(f"    首个候选: token={cands[0].get('token')} "
                  f"rva={cands[0].get('rva_hex')} 地址={cands[0].get('address')}")
        check("method_target 成功且拿到地址", bool(mt.get("ok")) and bool(cands),
              str(mt.get("error"))[:90])
        check("method_target 后进程仍活着", pid_alive(pid))

        hm = sess.hook_method("UnityEngine", "Time", "get_frameCount")
        print(f"  hook_method: ok={hm.get('ok')} hook_id={hm.get('hook_id')} "
              f"落点={hm.get('target')} token={hm.get('token')}")
        check("hook_method 成功", bool(hm.get("ok")), str(hm.get("error"))[:90])
        check("hook_method 后进程仍活着", pid_alive(pid))

        # 反证：野指针读不出来 —— 就绪检查识别的正是这个条件
        show("4. 非空转反证：野指针本身读不出（0x135 就是实测到的那个值）")
        ev = sess.eval_js('(function(){ try { ptr("0x135").readPointer();'
                          ' return "读成功(不该发生)"; }'
                          ' catch (e) { return "throw: " + e.message; } })()')
        val = ev.get("value") if isinstance(ev, dict) else None
        print(f"  eval_js 读 ptr(0x135): {val}")
        check("读野指针确实抛异常，且信息含 access violation",
              isinstance(val, str) and "throw" in val and "access violation" in val.lower())
        check("反证之后进程仍活着", pid_alive(pid))

        dom = sess.eval_js(
            '(function(){ try { var d = funcs.il2cpp_domain_get();'
            ' if (!d || d.isNull()) { return "null"; }'
            ' d.readPointer(); return d.toString(); }'
            ' catch (e) { return "throw: " + e.message; } })()')
        print(f"  就绪后的域指针（同样的检查逻辑）: {dom.get('value')}")
        check("就绪后域指针可读（非 null、不抛异常）",
              isinstance(dom.get("value"), str)
              and dom["value"].startswith("0x"))
    except Exception as exc:  # noqa: BLE001 - 失败要报出来，不要吞
        print(f"  ❌ 运行时异常：{type(exc).__name__}: {exc}")
        FAILS.append(f"运行时异常 {type(exc).__name__}")
    finally:
        try:
            close_all_sessions()
        except Exception as exc:  # noqa: BLE001
            print(f"  关闭会话时出错：{exc}")
        # 收尾：把游戏进程结束掉，避免留下占单实例锁的僵尸进程
        import subprocess
        if exe is not None:
            subprocess.run(["taskkill", "/F", "/T", "/IM", Path(exe).name],
                           capture_output=True)

# ------------------------------------------------------------------ 汇总
show("汇总")
if FAILS:
    print(f"  ❌ 失败 {len(FAILS)} 项：")
    for f in FAILS:
        print(f"     - {f}")
    sys.exit(1)
print("  ✅ 全部通过")
