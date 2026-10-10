# 运行时层端到端验证
#
# 关键点：**必须真的注入一个进程**并调用探针，不能只做静态检查。
# 优先选择真实游戏进程（IL2CPP）；找不到就退而验证注入机制本身
# （用一个可注入的普通进程验证 attach / RPC / 内存读写 / Hook）。
#
# 诚实原则：拿不到 IL2CPP 目标时必须明确说明「未验证 IL2CPP 路径」，
# 不能因为机制通了就宣称引擎工具可用。
import sys, io, os, json, time, subprocess
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
# 仓库路径由 samples.py 推导（不许硬编码工作区绝对路径）
# 用 os.path 推导，避免依赖后面才 import 的 pathlib
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import samples  # noqa: E402
samples.add_src_to_path()


def show(title):
    print()
    print("=" * 76)
    print(title)
    print("=" * 76)


from gamemod_mcp.engine import discovery
from gamemod_mcp.runtime import (
    probe_available, frida_version, attach, SessionError, RuntimeUnavailable,
    close_all_sessions, list_sessions,
)

show("0. Frida 可用性")
print(f"  版本: {frida_version()}")
print(f"  探测: {json.dumps(probe_available(), ensure_ascii=False)}")

# ---------------------------------------------------------------- 找目标
show("1. 寻找可注入目标")
procs = discovery.find_processes(only_games=False)
il2cpp_targets = [p for p in procs if p.runtime == "il2cpp"]
print(f"  枚举到进程 {len(procs)} 个")
print(f"  其中 IL2CPP 进程 {len(il2cpp_targets)} 个")
for p in il2cpp_targets[:8]:
    print(f"     pid={p.pid:<7} {p.name:<38} {p.runtime}")

target = il2cpp_targets[0] if il2cpp_targets else None
if target:
    print(f"  选用真实 IL2CPP 目标: {target.name} (pid={target.pid})")
else:
    print("  未发现运行中的 IL2CPP 游戏")
    print("  -> 将用一个受控目标验证注入机制本身；")
    print("     IL2CPP 专属路径（class_info/method_target）在本次运行中无法验证。")

# ---------------------------------------------------------------- 注入
show("2. 注入与探针握手")

spawned = None
if target is None:
    # 用 python 自身作为受控目标：可注入、能验证完整机制链路
    spawned = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    pid = spawned.pid
    name = "受控目标(python)"
    time.sleep(1.5)
else:
    pid = target.pid
    name = target.name

try:
    sess = attach(pid, name=name, init=False)
except SessionError as exc:
    print(f"  ❌ 注入失败：{exc}")
    if spawned:
        spawned.kill()
    sys.exit(1)

print(f"  ✅ 注入成功: {sess}")

try:
    ping = sess.ping()
    print(f"  ping: {json.dumps(ping, ensure_ascii=False)}")
except Exception as exc:
    print(f"  ❌ ping 失败：{exc}")
    sess.close()
    if spawned:
        spawned.kill()
    sys.exit(1)

# ---------------------------------------------------------------- init 结果
show("3. 探针初始化（IL2CPP 模块与导出解析）")
init = sess.init()
print(f"  ok = {init.get('ok')}")
if init.get("ok"):
    print(f"  模块      : {init.get('module')}  base={init.get('base')}")
    print(f"  可用导出  : {init.get('api_available')}")
    print(f"  缺失导出  : {len(init.get('api_missing') or [])}")
    print(f"  已启用回退: {init.get('fallbacks')}")
else:
    print(f"  错误: {init.get('error')}")
    print("  （受控目标不是 IL2CPP 进程，此处失败是**正确行为**：")
    print("    探针拒绝在找不到 IL2CPP 模块时假装成功）")

# ---------------------------------------------------------------- 通用机制
show("4. 通用机制验证（与引擎无关，任何进程都应通过）")

# 4a 内存读写：用探针持有的持久缓冲区做跨调用往返校验
try:
    ab = sess.alloc_buffer(64)
    addr = ab.get("address")
    print(f"  分配持久缓冲区: {addr} (size={ab.get('size')})")
    w = sess.write_memory(addr, "deadbeefcafebabe")
    print(f"  写入: {json.dumps(w, ensure_ascii=False)}")
    r = sess.read_memory(addr, 8)
    print(f"  读回: {r.get('hex')}  (期望 deadbeefcafebabe)")
    print(f"  内存往返: {'✅ 一致' if r.get('hex') == 'deadbeefcafebabe' else '❌ 不一致'}")

    # 越界保护：非法长度应被拒绝而不是崩溃
    bad = sess.read_memory(addr, 0)
    print(f"  非法 size=0 被拒: {'✅' if not bad.get('ok') else '❌ ' + str(bad)}")

    # 非法地址应返回结构化错误而不是让脚本挂掉
    bad2 = sess.read_memory("0x1", 4)
    print(f"  非法地址被拒: {'✅' if not bad2.get('ok') else '❌ ' + str(bad2)}")

    bufs = sess.list_buffers()
    print(f"  缓冲区清单: {json.dumps(bufs, ensure_ascii=False)}")
    fb = sess.free_buffer(addr)
    print(f"  释放: {json.dumps(fb, ensure_ascii=False)}")
except Exception as exc:
    print(f"  ❌ 内存读写失败：{exc}")

# 4b Hook：Hook 一个真实存在且能主动触发的函数
try:
    # Windows 上没有全局 getpid；改从目标进程的模块导出里找必然存在的函数。
    # 用 eval_js 让**目标进程自己**挑，避免宿主猜测平台差异。
    pick = sess.eval_js(
        "(function(){"
        "  var names = ['GetTickCount','GetCurrentProcessId','GetTickCount64',"
        "               'GetCurrentThreadId','Sleep'];"
        "  for (var i = 0; i < names.length; i++) {"
        "    try {"
        "      var p = Module.getGlobalExportByName(names[i]);"
        "      if (p) { return JSON.stringify({name: names[i], addr: p.toString()}); }"
        "    } catch (e) { /* 该导出不存在，试下一个 */ }"
        "  }"
        "  return null;"
        "})()"
    )
    picked = pick.get("value")
    print(f"  选取可 Hook 的真实导出: {picked}")
    if picked:
        info = json.loads(picked)
        h = sess.hook(info["addr"], args=[], label=f"selftest.{info['name']}",
                      max_calls=10, backtrace=False, retval=True,
                      retval_type="int")
        print(f"  安装 Hook: ok={h.get('ok')} id={h.get('hook_id')}")
        if h.get("target_rva"):
            print(f"    目标 RVA: {h['target_rva'].get('rva_hex')} "
                  f"(模块 {h['target_rva'].get('module')})")
        if not h.get("ok"):
            print(f"    错误: {h.get('error')}")
        # 在目标进程内**主动调用**它，触发 Hook
        sess.eval_js(
            "(function(){"
            f"  var f = new NativeFunction(ptr('{info['addr']}'), 'int', []);"
            "  for (var i = 0; i < 3; i++) { f(); }"
            "  return 'called 3x';"
            "})()"
        )
        time.sleep(0.6)
        ev = sess.drain_events(limit=100)
        enters = [e for e in ev["events"] if e.get("phase") == "enter"]
        leaves = [e for e in ev["events"] if e.get("phase") == "leave"]
        print(f"  捕获事件: enter={len(enters)} leave={len(leaves)}")
        if enters:
            print(f"    样例: {json.dumps(enters[0], ensure_ascii=False)}")
        if leaves:
            print(f"    返回值样例: {json.dumps(leaves[0], ensure_ascii=False)}")
        print(f"  Hook 链路: {'✅ 通' if enters else '❌ 未捕获到事件'}")
        st = sess.hook_status()
        print(f"  Hook 状态: {json.dumps(st, ensure_ascii=False)}")
        uh = sess.unhook(h.get("hook_id"))
        print(f"  卸载: {json.dumps(uh, ensure_ascii=False)}")

        # 卸载后不应再捕获到事件
        sess.clear_events()
        sess.eval_js(
            "(function(){"
            f"  var f = new NativeFunction(ptr('{info['addr']}'), 'int', []);"
            "  f(); return 'called';"
            "})()"
        )
        time.sleep(0.3)
        ev2 = sess.drain_events(limit=50)
        after = [e for e in ev2["events"] if e.get("phase") == "enter"]
        print(f"  卸载后事件数={len(after)} "
              f"({'✅ 已停止捕获' if not after else '❌ 仍在捕获'})")
    else:
        print("  ⚠️  未找到可用导出，跳过 Hook 验证")
except Exception as exc:
    print(f"  ❌ Hook 验证失败：{exc}")

# 4c AOB 扫描（不要求 IL2CPP，任何模块都应能扫）
try:
    # 目标模块必然以 PE 头 "MZ" (4D 5A) 开头，命中数必须 ≥ 1
    scan = sess.scan_pattern("4D 5A", max_results=8)
    print(f"  AOB 扫描 '4D 5A': ok={scan.get('ok')} "
          f"模块={scan.get('scanned_module')} 段数={scan.get('scanned_ranges')} "
          f"命中={scan.get('matched')}")
    if scan.get("results"):
        print(f"    首命中: {json.dumps(scan['results'][0], ensure_ascii=False)}")
    if not scan.get("ok"):
        print(f"    错误: {scan.get('error')}")
    print(f"  AOB 结果有效性: "
          f"{'✅ 命中数 ≥1（PE 头必然存在）' if (scan.get('matched') or 0) >= 1 else '❌ 命中为 0，扫描逻辑有误'}")
except Exception as exc:
    print(f"  ❌ AOB 扫描失败：{exc}")

# 4d eval_js 逃生舱
try:
    ev = sess.eval_js(
        "JSON.stringify({arch: Process.arch, ptr: Process.pointerSize, "
        "mods: Process.enumerateModules().length})"
    )
    print(f"  eval_js: {ev.get('value')}")
except Exception as exc:
    print(f"  ❌ eval_js 失败：{exc}")

# ---------------------------------------------------------------- IL2CPP 专属
show("5. IL2CPP 专属路径（仅当目标是真实 IL2CPP 游戏时才可验证）")
if target is None:
    print("  ⏭️  本次无 IL2CPP 目标，**未验证** class_info / method_target /")
    print("      class_fields / 字段真实偏移 / 方法 RVA。")
    print("      这些路径依赖 il2cpp 导出符号，需要真实游戏进程才能确认。")
    print("      首次实战使用时应优先验证 `runtime_info` 的 image_count")
    print("      是否与静态元数据的 94 个程序集吻合。")
else:
    ri = sess.runtime_info()
    print(f"  runtime_info: ok={ri.get('ok')} 映像={ri.get('image_count')}")
    if ri.get("ok"):
        for img in (ri.get("images") or [])[:5]:
            print(f"     {img.get('name')}  类数={img.get('class_count')}")

# ---------------------------------------------------------------- 收尾
show("6. 会话生命周期")
print(f"  活跃会话: {json.dumps(list_sessions(), ensure_ascii=False)}")
cl = sess.close()
print(f"  关闭: {json.dumps(cl, ensure_ascii=False)}")
print(f"  关闭后 ping 应失败：", end="")
try:
    sess.ping()
    print("❌ 仍然可用（不符合预期）")
except Exception as exc:
    print(f"✅ 已拒绝（{type(exc).__name__}: {exc}）")

close_all_sessions()
if spawned:
    spawned.kill()
    print("  已回收受控目标进程")
print()
print("运行时层验证结束。")
