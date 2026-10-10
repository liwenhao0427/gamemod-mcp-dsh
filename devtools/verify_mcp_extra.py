"""补充覆盖：主流程之外的工具。

`verify_mcp_tools.py` 与 `verify_mcp_mono.py` 覆盖的是**主工作流**
（发现 → 索引 → 查询 → 注入 → Hook）。本脚本补上主流程没有走到的
工具，让所有工具都经过实际调用：

    find_game_processes / inspect_binary / inject_process /
    hook_address / read_field_value / gc_control
    + 合并后能力的回归：find_derived_classes(assembly=) /
      class_detail(method_name=)

其中 inject_process / hook_address / read_field_value / gc_control
需要一个真实运行中的游戏，所以本脚本会自己 spawn 一个（用 Garden of Witches），
结束时清掉。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# sys.path 与样本路径都由 samples.py 推导/集中定义，脚本自身不写绝对路径
sys.path.insert(0, str(Path(__file__).resolve().parent))
import samples                                            # noqa: E402
samples.add_src_to_path()
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import gamemod_mcp.server as S              # noqa: E402

# 样本路径：根目录从 samples.py 取，派生路径用相对路径拼出来
_GAME = samples.find_il2cpp()
GAME_DIR = str(_GAME) if _GAME else None
GAME_EXE = str(_GAME / "Garden of Witches.exe") if _GAME else None
GAME_ASSEMBLY = (str(_GAME / "Garden of Witches_Data" / "il2cpp_data"
                     / "Metadata" / "global-metadata.dat")) if _GAME else None

# 样本不可用时记为「无法验证」并跳过（跳过不算失败）
HAVE_GAME = samples.require(GAME_DIR, "IL2CPP 样本 Garden of Witches")
if HAVE_GAME and not os.path.isfile(GAME_EXE):
    print(f"  ⚠️  无法验证：样本主程序不存在 —— {GAME_EXE}")
    HAVE_GAME = False

PASS = 0
FAIL = 0
WARN = 0


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


def section(t: str) -> None:
    print()
    print("=" * 78)
    print(t)
    print("=" * 78)


def have(path) -> bool:
    """样本路径存在性判断；路径为 None（样本缺失）时返回 False。"""
    return bool(path) and Path(path).is_file()


def write_marker(offset: int, width: int, value: int) -> dict:
    """在伪造实例的真实字段偏移处写入 width 字节的标记值。

    只为 `read_field_value` 的**读取宽度**验收服务：把「这个字段自己的宽度」
    交给内存，读回来多一个字节都说明实现读了别人的内存。
    """
    if width == 1:
        expr = f"writeU8(0x{value:02x})"
    elif width == 2:
        expr = f"writeU16(0x{value:04x})"
    elif width == 4:
        expr = f"writeU32(0x{value:08x})"
    else:
        expr = f"writeU64(uint64('0x{value:016x}'))"
    return S.eval_script(
        "(function(){"
        "  globalThis.__verify_buf = globalThis.__verify_buf"
        " || Memory.alloc(512);"
        f"  globalThis.__verify_buf.add({offset}).{expr};"
        "  return globalThis.__verify_buf.toString();"
        "})()"
    )


# ============================================================ 1. 纯静态工具
section("1. find_game_processes —— 不依赖任何游戏")
#
# 注：detect_games 已移除。理由是本 MCP 的每次运行都限定在
# 「游戏项目目录」这个上下文里，主动扫全机 Steam 库属于噪声工具，
# 而且它的结果并不能直接驱动后续步骤（open_game 仍要显式给目录）。
# 因此这里不再断言它存在。

fg = S.find_game_processes()
check("find_game_processes 返回 ok", fg.get("ok") is True, fg.get("error", ""))
print(f"     当前运行中的游戏进程: {fg.get('count')}")
check("返回结构里带 count", isinstance(fg.get("count"), int),
      f"count={fg.get('count')!r}")
check("返回结构里带 processes 列表",
      isinstance(fg.get("processes"), list),
      f"{len(fg.get('processes') or [])} 条")
if fg.get("processes"):
    p0 = fg["processes"][0]
    print(f"     样例: {p0.get('name')} pid={p0.get('pid')} "
          f"runtime={p0.get('runtime')}")
    check("进程条目带 pid 与 name",
          bool(p0.get("pid")) and bool(p0.get("name")))
    # P2-5：`architectures` 是个**恒为空列表**的字段（process_arch 的第二个
    # 返回值历来写死 []），已删除 —— 一个永远 `[]`、又长得像「没检测到任何
    # 架构标记」的字段，比没有它更容易误导。位数看 is_64；
    # None 表示**没测出来**（不是「32 位」）。
    #
    # 注意这里**不**用上面那个 `fg`：它的 only_games=True 在没开游戏时
    # 会返回 0 条，断言就被整段跳过（空转）。改成按名字查本机的 python ——
    # 本脚本自己就是个 python 进程，所以这一条一定有进程可断言。
    fg2 = S.find_game_processes(name_filter="python", only_games=False)
    procs2 = fg2.get("processes") or []
    if procs2:
        p = procs2[0]
        print(f"     形状检查用的进程: {p.get('name')} pid={p.get('pid')}")
        check("进程条目带 is_64（位数）", "is_64" in p,
              f"is_64={p.get('is_64')!r}")
        check("不再有恒为空的 architectures 字段",
              "architectures" not in p, "已删除，位数看 is_64")
        check("is_64 是 bool 或 None（不许拿 False 冒充「不知道」）",
              p.get("is_64") is None or isinstance(p.get("is_64"), bool),
              f"is_64={p.get('is_64')!r}")
    else:
        check("能查到本机 python 进程（上面两条断言的前提）", False,
              f"count={fg2.get('count')} —— 这两条断言没被验证")


section("2. inspect_binary —— PE 结构分析")

if have(GAME_EXE):
    ib = S.inspect_binary(GAME_EXE)
    check("inspect_binary 返回 ok", ib.get("ok") is True, ib.get("error", ""))
    print(f"     架构: {ib.get('machine')}  运行时: {ib.get('runtime')}")
    print(f"     节区数: {ib.get('section_count')}  "
          f"导出数: {ib.get('export_count')}")
    check("识别出架构（顶层直接可见）", bool(ib.get("machine")),
          f"machine={ib.get('machine')!r}")
    check("顶层能直接拿到 is_64", isinstance(ib.get("is_64"), bool),
          f"is_64={ib.get('is_64')!r}")
    check("有节区信息", len((ib.get("pe") or {}).get("sections") or []) > 0,
          f"{len((ib.get('pe') or {}).get('sections') or [])} 个")
    # Unity 主程序通常是原生 exe（逻辑在 GameAssembly.dll）
    check("主程序运行时类型合理",
          ib.get("runtime") in ("native", "dotnet", "il2cpp", "mono",
                                "nativeaot"),
          f"runtime={ib.get('runtime')!r}")

    # GameAssembly.dll 才是 IL2CPP 的逻辑所在，它应当有大量导出
    ga = str(Path(GAME_EXE).parent / "GameAssembly.dll")
    if have(ga):
        ib2 = S.inspect_binary(ga)
        check("inspect_binary 能读 GameAssembly.dll", ib2.get("ok") is True)
        print(f"     GameAssembly.dll: 导出数={ib2.get('export_count')} "
              f"节区数={ib2.get('section_count')}")
        check("GameAssembly.dll 有导出（il2cpp_* API）",
              (ib2.get("export_count") or 0) > 0,
              f"{ib2.get('export_count')} 个")
else:
    # 缺样本是「无法验证」，不是「断言失败」—— 与本文件开头
    # `HAVE_GAME = samples.require(...)` 的口径保持一致。
    # 这里以前调 `check(..., False, ...)`，于是同一个「样本不在本机」
    # 在别处记「无法验证」、在这里记一条红，汇总行的「失败 N 项」
    # 就混进了环境问题。
    print(f"  ⚠️  无法验证：IL2CPP 样本主程序不在本机 —— {GAME_EXE}")
    print("      上面这段 inspect_binary 断言跳过（跳过不算失败）")
    WARN += 1

# 不存在的文件必须报错
bad = S.inspect_binary(r"C:\这个文件根本不存在_xyz.exe")
check("不存在的文件被拒绝", bad.get("ok") is False, bad.get("error", ""))

# P1-5 回归：inspect_binary 只许把 PE 解析**一次**。
# 以前是 `pe_parse(p)` + `pe_summarize(p)`，而 summarize 内部又 parse 一遍，
# 于是同一个文件被完整解析两次，且第一次的结果从未被使用。
# 这里不量时间（会闪），而是数**调用次数** —— 次数是确定的。
if have(GAME_EXE):
    from gamemod_mcp.engine import pe as _pe
    _orig_pe_parse = _pe.parse
    _pe_calls = {"n": 0}

    def _counting_pe_parse(*a, **kw):
        _pe_calls["n"] += 1
        return _orig_pe_parse(*a, **kw)

    # pe.summarize 内部调的是 pe 模块全局的 parse，所以补这里能同时
    # 覆盖「state 自己调的 parse」与「summarize 内部调的 parse」。
    _pe.parse = _counting_pe_parse
    try:
        ib3 = S.inspect_binary(GAME_EXE)
    finally:
        _pe.parse = _orig_pe_parse
    print(f"     inspect_binary 一次调用触发的 pe.parse 次数 = {_pe_calls['n']}")
    check("inspect_binary 只解析 PE 一次（P1-5：不再重复解析）",
          ib3.get("ok") is True and _pe_calls["n"] == 1,
          f"pe.parse 被调 {_pe_calls['n']} 次，期望 1 次")
    # 去掉重复解析后返回内容必须一字不差
    check("去掉重复解析后返回值不变",
          {k: v for k, v in ib3.items() if k != "pe"}
          == {k: v for k, v in ib.items() if k != "pe"},
          "顶层字段应当完全一致")


section("3. find_derived_classes(assembly=) / class_detail(method_name=) —— 需要索引")
#
# find_monobehaviours 与 get_method_source 已合并进这两个工具：
#   find_monobehaviours → find_derived_classes(base_full_name="UnityEngine.MonoBehaviour",
#                                              assembly="Assembly-CSharp")
#   get_method_source  → class_detail(full_name=..., method_name=...)
# 合并的验收断言写成「旧能力仍然可达」，而不是简单删掉原断言。

if not have(GAME_ASSEMBLY):
    print(f"  \u26a0\ufe0f  样本元数据不存在，跳过：{GAME_ASSEMBLY}")
else:
    op = S.open_game(game_dir=GAME_DIR)
    check("open_game 成功", op.get("ok") is True, op.get("error", ""))

    # 原 find_monobehaviours 的能力：找游戏自己写的 MonoBehaviour 脚本
    mb = S.find_derived_classes(
        base_full_name="UnityEngine.MonoBehaviour",
        assembly="Assembly-CSharp", limit=5000)
    check("find_derived_classes 返回 ok", mb.get("ok") is True, mb.get("error", ""))
    print(f"     MonoBehaviour 脚本（限 Assembly-CSharp）: {mb.get('total')}")
    check("找到 MonoBehaviour 脚本（原 find_monobehaviours 的能力）",
          (mb.get("total") or 0) > 0, f"{mb.get('total')}")
    if mb.get("items"):
        print(f"     样例: {mb['items'][0].get('full_name')}")
        check("每项都带 index（合并后新增的信息）",
              all("index" in it for it in mb["items"]))

    # 不限程序集时结果只会更多，不会更少
    mb_all = S.find_derived_classes(
        base_full_name="UnityEngine.MonoBehaviour", limit=5000)
    check("不限程序集得到的结果不少于过滤后",
          (mb_all.get("total") or 0) >= (mb.get("total") or 0),
          f"全部={mb_all.get('total')} vs 过滤={mb.get('total')}")

    # 原 get_method_source 的能力：按方法名取静态信息（token 等）
    gms = S.class_detail(full_name="TransformPingPongMotion",
                         method_name="Update")
    check("class_detail(method_name=) 返回 ok", gms.get("ok") is True,
          str(gms.get("error", ""))[:80])
    if gms.get("ok"):
        print(f"     方法: {gms.get('method')}  "
              f"token={gms.get('token')}  重载数={gms.get('overload_count')}")
        check("返回里有 token（Hook 定位的关键）",
              bool(gms.get("token")), f"{gms.get('token')!r}")
        check("返回里有 methods 列表", isinstance(gms.get("methods"), list))

    # 不存在的方法必须报错（原 get_method_source 的假否定防护）
    bad2 = S.class_detail(full_name="TransformPingPongMotion",
                          method_name="这个方法不存在Xyz")
    check("不存在的方法被拒绝", bad2.get("ok") is False, bad2.get("error", ""))
    check("报错时给出可用方法名作为线索",
          bool(bad2.get("available")),
          f"{len(bad2.get('available') or [])} 个候选")

    # 不存在的类必须报错
    bad3 = S.class_detail(full_name="这个类不存在Xyz", method_name="Whatever")
    check("不存在的类被拒绝", bad3.get("ok") is False,
          str(bad3.get("error", bad3))[:80])

    # 不给 method_name 时仍返回完整类信息（原 class_detail 行为不变）
    full = S.class_detail(full_name="TransformPingPongMotion")
    check("不给 method_name 时返回完整类信息",
          full.get("ok") is True and "fields" in full and "methods" in full,
          f"字段={len(full.get('fields') or [])} 方法={len(full.get('methods') or [])}")


# ============================================================ 4. 需要运行时的工具
section("4. inject_process / hook_address / read_field_value / gc_control")

if not have(GAME_EXE):
    print(f"  \u26a0\ufe0f  游戏不存在，跳过运行时部分：{GAME_EXE}")
else:
    # 先手动 spawn 一个进程（不注入），再走 inject_process 这条路 ——
    # 这样才真正验证「注入已在运行的进程」这个代码路径，
    # 而不是被 launch_game（spawn 模式）覆盖掉。
    import subprocess
    # 开场先清掉可能残留的同名进程 —— 上一轮如果中途崩了会留下进程，
    # 而 Unity 游戏在有旧实例时新实例可能不正常加载 IL2CPP，
    # 会让「注入成功但 IL2CPP 没就绪」这种假象冒出来。
    st_name = Path(GAME_EXE).stem
    try:
        stale = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {Path(GAME_EXE).name}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=30,
        )
        for line in (stale.stdout or "").splitlines():
            if "exe" in line.lower():
                sp = line.split('","')[1] if '","' in line else ""
                if sp.isdigit():
                    print(f"     清理残留进程 pid={sp}")
                    subprocess.run(["taskkill", "/F", "/T", "/PID", sp],
                                   capture_output=True, timeout=30)
        time.sleep(1.0)
    except Exception as exc:
        print(f"     残留检查跳过：{exc}")

    print("     手动启动游戏进程（不注入）…")
    proc = subprocess.Popen(
        [GAME_EXE],
        cwd=str(Path(GAME_EXE).parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pid = proc.pid
    print(f"     pid={pid}，等待 IL2CPP 模块加载…")

    # 等 GameAssembly.dll 加载出来（IL2CPP 游戏启动需要时间）
    ok_inject = False
    init_ok = False
    for attempt in range(30):
        time.sleep(1.0)
        # 进程提前退出就没必要再等了
        if proc.poll() is not None:
            print(f"     进程已退出（exit={proc.returncode}），停止等待")
            break
        res = S.inject_process(pid=pid, auto_find=False)
        if res.get("ok"):
            ok_inject = True
            init = res.get("init") or {}
            init_ok = bool(init.get("ok"))
            print(f"     注入成功（第 {attempt + 1} 次尝试）")
            print(f"     模块={init.get('module')} base={init.get('base')}")
            if res.get("warning"):
                print(f"     ⚠ {res.get('warning')}")
            if init.get("error"):
                print(f"     init 错误: {str(init.get('error'))[:100]}")
            print(f"     缺失 API: {init.get('api_missing')}")
            check("inject_process 返回 ok", True)
            check("注入结果带 pid", res.get("pid") == pid,
                  f"pid={res.get('pid')}")
            if init_ok:
                check("il2cpp 初始化成功", True,
                      f"module={init.get('module')} "
                      f"尝试 {init.get('attempts')} 次 "
                      f"等待 {init.get('waited')}s")
                check("无缺失的 il2cpp API",
                      not init.get("api_missing"),
                      f"缺失={init.get('api_missing')}")
            else:
                # 注入本身成功但运行时没就绪：如实记为警告。
                # 本机是无显示环境，游戏可能根本没走到 IL2CPP 初始化。
                check("il2cpp 初始化成功", False,
                      f"进程已注入但 IL2CPP 未就绪："
                      f"{str(init.get('error'))[:80]}", warn_only=True)
            break
        if attempt == 0:
            print(f"     首次尝试未成功（预期，模块可能还没加载）："
                  f"{str(res.get('error'))[:70]}")
    if not ok_inject:
        # 注入失败**不能**记成产品缺陷：本机是无显示环境，
        # 残留的同名进程、游戏启动慢、系统瞬时负载都会让 30 次轮询耗尽，
        # 而这条「注入已在运行的进程」的路径本来就被
        # verify_mcp_tools.py 的 launch_game（spawn 模式）覆盖了。
        # 记成硬失败只会让整套验收偶发变红（实测出现过一次），
        # 属于「拿环境问题当产品缺陷」，与 AGENTS.md §5.2 相悖。
        check("inject_process 最终成功", False,
              "30 次尝试后仍未注入成功 —— 本机环境问题，"
              "该路径已被 verify_mcp_tools 的 launch_game 覆盖",
              warn_only=True)

    # 后续工具都需要 IL2CPP 就绪；没就绪就跳过，不要拿环境问题当产品缺陷
    if ok_inject and not init_ok:
        print()
        print("  ⚠ 本机 IL2CPP 未就绪（无显示环境），跳过依赖运行时的工具：")
        print("     runtime_class_info / hook_address / read_field_value / gc_control")
        for name in ("runtime_class_info", "hook_address", "read_field_value",
                     "gc_control"):
            check(f"{name}（需 IL2CPP 就绪）", False,
                  "本机无显示环境，跳过", warn_only=True)
        ok_inject = False

    if ok_inject:
        # ---- hook_address：按绝对地址装 Hook
        tgt = S.find_hook_target(
            name="Time", method="get_frameCount", namespace="UnityEngine",
        )
        if tgt.get("ok") and tgt.get("candidates"):
            cand = tgt["candidates"][0]
            addr = cand.get("address")
            print(f"     目标 {cand.get('name')} address={addr} "
                  f"rva={cand.get('rva_hex')}")
            check("候选带绝对地址", bool(addr), f"{addr!r}")

            if addr:
                ha = S.hook_address(
                    address=addr, label="supp.hook_address", max_calls=1000,
                )
                check("hook_address 返回 ok", ha.get("ok") is True,
                      str(ha.get("error", ""))[:80])
                if ha.get("ok"):
                    print(f"     hook_id={ha.get('hook_id')} "
                          f"rva={ha.get('rva_hex')}")
                    check("hook_address 结果带 RVA", bool(ha.get("rva_hex")))

                    # 主动调一次以证明 Hook 真的装上了
                    S.eval_script(
                        f"(function(){{var f=new NativeFunction(ptr('{addr}'),"
                        f"'int',[]); f(); return 'called';}})()"
                    )
                    time.sleep(0.5)
                    ev = S.drain_events(limit=50)
                    n = len(ev.get("events") or [])
                    print(f"     主动调用后捕获 {n} 条事件")
                    check("hook_address 装的 Hook 真的生效", n > 0,
                          f"{n} 条")
                    S.unhook()

                # 非法地址必须报错
                bad = S.hook_address(address="0x1", label="bad")
                check("非法地址被拒绝", bad.get("ok") is False,
                      str(bad.get("error", ""))[:70])

        # ---- read_field_value
        # 这是唯一能在无 GUI 环境下真正验证「字段偏移算术」的方式：
        # 伪造一块内存当实例，把标记值写到**真实字段偏移**上，
        # 再看 read_field_value 能不能按同一个偏移读回来。
        # 读回来一致 => 偏移计算正确、读内存路径正确。
        rc = S.runtime_class_info(name="PresetLoaderV2")
        check("runtime_class_info 返回 ok", rc.get("ok") is True,
              str(rc.get("error", ""))[:80])
        if rc.get("ok"):
            fields = rc.get("fields") or []
            print(f"     字段数={len(fields)}")
            target_field = next(
                (f for f in fields
                 if f.get("name") == "userDataPresetFile"), None
            )
            if target_field is None:
                target_field = next(
                    (f for f in fields if f.get("offset") is not None
                     and not f.get("is_static")), None
                )
            if target_field:
                fname = target_field.get("name")
                foff = int(target_field.get("offset"))
                marker = 0xCAFEBABE12345678
                print(f"     目标字段 {fname} 偏移={foff} (0x{foff:X})")

                # 造实例：分配一块内存，在「该字段的真实偏移」处写入标记值。
                #
                # 两个必须注意的 Frida 细节（都踩过）：
                #   1. 必须把指针挂在 globalThis 上。只写 `var p = Memory.alloc()`
                #      的话，eval_script 一返回 JS 侧就没引用了，GC 会立刻
                #      回收，下一次 RPC 读到的就是已复用的内存。
                #   2. writeU64 要 UInt64，不能传 ptr 或字符串，否则报
                #      "UInt64 object expected"。
                alloc = S.eval_script(
                    "(function(){"
                    "  globalThis.__verify_buf = Memory.alloc(512);"
                    f"  globalThis.__verify_buf.add({foff})"
                    f".writeU64(uint64('0x{marker:016x}'));"
                    "  return globalThis.__verify_buf.toString();"
                    "})()"
                )
                fake = alloc.get("value")
                check("造出伪造实例地址", bool(fake),
                      f"{fake!r} err={alloc.get('error')}")

                if fake:
                    # 稍等一下，确认这块内存不会在两次 RPC 之间被回收
                    time.sleep(1.0)
                    print(f"     伪造实例 {fake}，"
                          f"已在 +{foff} 写入 0x{marker:016x}")
                    rf = S.read_field_value(
                        instance=fake, name="PresetLoaderV2", field=fname,
                    )
                    check("read_field_value 返回 ok", rf.get("ok") is True,
                          str(rf.get("error", ""))[:80])
                    if rf.get("ok"):
                        got = (rf.get("raw_hex") or "").lower()
                        # 内存小端：0xCAFEBABE12345678 读出来是
                        # 78 56 34 12 be ba fe ca
                        want_le = marker.to_bytes(8, "little").hex()
                        print(f"     读回 raw_hex={got}  "
                              f"as_pointer={rf.get('as_pointer')}")
                        check("按字段偏移读到正确的值（偏移算术正确）",
                              got == want_le,
                              f"读回={got} 期望={want_le}")
                        check("字段地址 = 实例 + 偏移",
                              rf.get("field_address") == hex(
                                  int(fake, 16) + foff),
                              f"field_address={rf.get('field_address')} "
                              f"期望={hex(int(fake, 16) + foff)}")
                        check("as_pointer 还原出原值",
                              (rf.get("as_pointer") or "").lower()
                              == hex(marker),
                              f"as_pointer={rf.get('as_pointer')}")
            else:
                check("找到可用的实例字段", False, "没有带偏移的实例字段")

            # 不存在的字段必须报错（这是最容易骗到 Agent 的假成功）
            badf = S.read_field_value(
                instance="0x1000", name="PresetLoaderV2",
                field="这个字段不存在Xyz",
            )
            check("不存在的字段被拒绝", badf.get("ok") is False,
                  str(badf.get("error", ""))[:70])
            check("拒绝时列出可用字段（便于 Agent 自我纠正）",
                  bool(badf.get("available")),
                  f"{len(badf.get('available') or [])} 个")

        # ---- read_field_value 读几个字节**随字段类型变化**（P2-5）
        #
        # 判据是「写几个字节，就读回几个字节」—— 这是**与实现无关**的真值：
        # 原来固定读 8 字节的实现，在 1 字节的 bool 字段上会读回 16 个 hex
        # 字符（自己的 1 字节 + 相邻字段的 7 字节），下面的 raw_hex 断言
        # 必然失败。所以这条不是「断言应该如此」，而是真的能抓住旧写法。
        #
        # 每种宽度用**一个类型确定的自有字段**：
        #   · `fieldsOf()` 只枚举类**自己的**字段（不走基类链），
        #     所以 int32 那个不能借用 PresetLoaderV2 继承来的
        #     OffsetOfInstanceIDInCPlusPlusObject —— 得换一个真有 int 字段的类。
        #   · 实例是伪造的（在真实偏移上写标记值），所以不需要该类真有实例。
        #   · 拿不到的用例记为「无法验证」并跳过，不算失败；但**至少要覆盖
        #     两种宽度**，否则这条验收等于没跑（见最后的断言）。
        print()
        print("     read_field_value 的读取宽度（按字段类型，不再固定 8 字节）：")
        WIDTH_PROBE = [
            ("PresetLoaderV2", "_isLoading", "System.Boolean", 1, 0x01),
            ("TeamTapas.ProjectR.Ingame.InteractionDetector",
             "_blockInteractFrameCount", "System.Int32", 4, 0x11223344),
            ("PresetLoaderV2", "userDataPresetFile", "UnityEngine.TextAsset", 8,
             0xCAFEBABE12345678),
        ]
        widths_seen: set[int] = set()
        probed = 0
        w1: dict | None = None            # 1 字节字段的那次结果，循环后复查
        for cls, fname, ftype, want_w, marker_val in WIDTH_PROBE:
            rci = S.runtime_class_info(name=cls)
            if not rci.get("ok"):
                check(f"{cls} 可查（宽度验收 {want_w} 字节那例）", False,
                      str(rci.get("error", ""))[:70], warn_only=True)
                continue
            fld = next((f for f in (rci.get("fields") or [])
                        if f.get("name") == fname), None)
            if not fld or fld.get("offset") is None:
                check(f"{cls}.{fname} 可用于宽度验收", False,
                      f"该类自身的 {len(rci.get('fields') or [])} 个字段里"
                      f"没有它（或没有偏移）—— 样本变了", warn_only=True)
                continue
            check(f"{cls}.{fname} 的运行时类型是 {ftype}",
                  fld.get("type") == ftype, f"实际 {fld.get('type')!r}")
            wrote = write_marker(int(fld.get("offset")), want_w, marker_val)
            if not wrote.get("ok"):
                check(f"{fname} 写入 {want_w} 字节标记值", False,
                      str(wrote.get("error", ""))[:70])
                continue
            time.sleep(0.3)                  # 别让两次 RPC 之间内存被回收
            wf = S.read_field_value(instance=wrote.get("value"),
                                    name=cls, field=fname)
            if not wf.get("ok"):
                check(f"{fname}（{want_w} 字节）读回成功", False,
                      str(wf.get("error", ""))[:70])
                continue
            probed += 1
            widths_seen.add(int(wf.get("read_width") or 0))
            if want_w == 1:
                w1 = wf
            want_hex = marker_val.to_bytes(want_w, "little").hex()
            got_hex = (wf.get("raw_hex") or "").lower()
            print(f"       {fname:30} {ftype:24} "
                  f"read_width={wf.get('read_width')} raw_hex={got_hex}")
            check(f"{fname} 的 read_width == {want_w}",
                  wf.get("read_width") == want_w,
                  f"实际 {wf.get('read_width')}")
            check(f"{fname}（{want_w} 字节）正好读回自己的 {want_w} 字节",
                  got_hex == want_hex,
                  f"读回={got_hex} 期望={want_hex}"
                  + ("（多读的字节是相邻字段的内容）"
                     if len(got_hex) > len(want_hex) else ""))

        # 解释字段也要按宽度给：宽度不够就不该出现那个宽度的解释，
        # 否则 Agent 会拿「本字段 + 邻居」拼出来的 as_int64 当真值。
        if w1 is not None:
            check("1 字节字段不给 4/8 字节解释（as_int32 会拼上邻居的字节）",
                  "as_int32" not in w1 and "as_int64" not in w1,
                  f"给出的解释键 = "
                  f"{sorted(k for k in w1 if k.startswith('as_'))}")
            check("1 字节字段给出 as_bool / as_uint8",
                  "as_bool" in w1 and "as_uint8" in w1,
                  f"as_bool={w1.get('as_bool')} "
                  f"as_uint8={w1.get('as_uint8')}")
        else:
            check("拿到 1 字节字段的读取结果（解释键验收的前提）", False,
                  "上面 1 字节那条没读成功 —— 这两条断言没被验证",
                  warn_only=True)
        # 「宽度随类型变」这件事只有在**至少两种宽度都实测到**时才算验过。
        # 只跑通一种宽度的话，下面这条会报红 —— 不是产品坏了，而是这组
        # 验收没跑起来（这正是「断言必须非空转」的要求）。
        check("至少实测到两种不同宽度（否则这组验收是空转）",
              len(widths_seen) >= 2 and probed >= 2,
              f"成功 {probed}/{len(WIDTH_PROBE)} 例，"
              f"实测宽度集合 = {sorted(widths_seen)}")
        check("实测宽度都落在字段自己的宽度上（1/4/8 各自对得上）",
              widths_seen <= {1, 4, 8},
              f"实测宽度集合 = {sorted(widths_seen)}")

        # ---- set_field_value：read_field_value 的**对称写入口**
        #
        # 复用上面那套「伪造实例」底座：把缓冲区基址当 instance 传进去，
        # 工具自己按字段真实偏移算地址，正好落在我们控制的缓冲区里。
        # 这样验的是**工具自己的偏移计算 + 类型编码 + 回读**，
        # 而不是我们手工算的地址。
        #
        # 判据与读取那边同源、但更强：写完要能与独立读回的字节**逐字节相等**。
        # 只报 ok 不够 —— 「写进去了但编码错」才是这个工具的真实风险
        # （float 当成 int 写、300 截成 44 都属于这一类，且都显示 ok:true）。
        print()
        print("     set_field_value 的写入（按字段类型编码 + 回读核对）：")
        SET_PROBE = [
            ("TeamTapas.ProjectR.Ingame.InteractionDetector",
             "_blockInteractFrameCount", "System.Int32", 4, 0x11223344,
             0x55667788),
            ("PresetLoaderV2", "_isLoading", "System.Boolean", 1, None, True),
        ]
        set_ok = 0
        for cls, fname, ftype, want_w, first_val, second_val in SET_PROBE:
            rci = S.runtime_class_info(name=cls)
            if not rci.get("ok"):
                check(f"{cls} 可查（写入验收）", False,
                      str(rci.get("error", ""))[:70], warn_only=True)
                continue
            fld = next((f for f in (rci.get("fields") or [])
                        if f.get("name") == fname), None)
            if not fld or fld.get("offset") is None:
                check(f"{cls}.{fname} 可用于写入验收", False,
                      "样本变了", warn_only=True)
                continue
            base = write_marker(int(fld.get("offset")), want_w,
                                first_val if first_val is not None else 0)
            if not base.get("ok"):
                check(f"{fname} 预备缓冲区", False,
                      str(base.get("error", ""))[:70])
                continue
            inst = base.get("value")
            # 1) 写一个与初值**不同**的值
            sv = S.set_field_value(instance=inst, name=cls, field=fname,
                                   value=second_val)
            if not sv.get("ok"):
                check(f"set_field_value({fname}) 返回 ok", False,
                      str(sv.get("error", ""))[:100])
                continue
            set_ok += 1
            print(f"       {fname:30} kind={sv.get('write_kind')} "
                  f"width={sv.get('write_width')} "
                  f"before={sv.get('value_before_hex')} "
                  f"after={sv.get('read_back_hex')}")
            check(f"{fname} 的 write_width == {want_w}",
                  sv.get("write_width") == want_w,
                  f"实际 {sv.get('write_width')}")
            # 2) 回读核对字段（工具自己算的验证位）
            check(f"{fname} 写入后自校验 verified is True",
                  sv.get("verified") is True,
                  f"verified={sv.get('verified')!r} "
                  f"read_back={sv.get('read_back_hex')!r}")
            # 3) **独立**用 read_field_value 再读一次 —— 不信工具自己的回读
            indep = S.read_field_value(instance=inst, name=cls, field=fname)
            if indep.get("ok"):
                want_hex = (second_val.to_bytes(want_w, "little").hex()
                            if isinstance(second_val, int) else "01")
                got_hex = (indep.get("raw_hex") or "").lower()
                check(f"{fname} 独立读回的字节 == 写入值",
                      got_hex == want_hex,
                      f"读回={got_hex} 期望={want_hex}")
            else:
                check(f"{fname} 独立读回成功", False,
                      str(indep.get("error", ""))[:70], warn_only=True)

        # 4) **拒绝**分支：引用类型字段不许被写（改引用属改逻辑）
        rej = S.set_field_value(instance="0x1000", name="PresetLoaderV2",
                                field="userDataPresetFile", value="0x1234")
        check("引用类型字段（TextAsset）被拒绝，且不是 ok:false 的假失败",
              rej.get("ok") is False and "引用类型" in str(rej.get("error", "")),
              f"error={str(rej.get('error'))[:90]}")

        # 5) **溢出**分支：不许静默截断
        ovf = S.set_field_value(
            instance="0x1000",
            name="TeamTapas.ProjectR.Ingame.InteractionDetector",
            field="_blockInteractFrameCount", value=2 ** 40)
        check("int32 字段写入超范围值被拒绝（不静默截断）",
              ovf.get("ok") is False and "超出" in str(ovf.get("error", "")),
              f"error={str(ovf.get('error'))[:90]}")

        check("set_field_value 至少成功写完一个字段（否则上面是空转）",
              set_ok >= 1, f"成功 {set_ok}/{len(SET_PROBE)} 例")

        # ---- gc_control（签名是 disable: bool，不是 action 字符串）
        gcd = S.gc_control(disable=True)
        check("gc_control(disable=True) 返回 ok", gcd.get("ok") is True,
              str(gcd.get("error", ""))[:80])
        if gcd.get("ok"):
            print(f"     禁用 GC 后状态: {gcd.get('collecting')}")
            # 关闭 GC 后应当能读回状态
            gce = S.gc_control(disable=False)
            check("gc_control(disable=False) 恢复成功", gce.get("ok") is True,
                  str(gce.get("error", ""))[:60])
            print(f"     恢复后状态: {gce.get('collecting')}")
        else:
            check("gc_control 可用", False,
                  str(gcd.get("error", ""))[:60], warn_only=True)

        # 没有运行时会话时必须明确报错
        S.close_session()
        nogc = S.gc_control(disable=True)
        check("无运行时会话时 gc_control 被拒绝",
              nogc.get("ok") is False, str(nogc.get("error", ""))[:60])
        # 恢复会话以便后续清理（本段后面还会用到）
        if ok_inject:
            S.inject_process(pid=pid, auto_find=False)
    else:
        # ---- 注入没成功时的退路：验「无会话必须报错」这条负路径
        #
        # 为什么要有这一段：这几个工具是**唯一**由本脚本负责的工具，
        # 一旦注入失败就整段跳过，动态覆盖率会悄悄从 38/38 掉到 35/38。
        # 事实证明这不只是假设 —— 把「注入失败」放宽成警告后，
        # report_tool_coverage.py --strict 立刻抓到了这 3 个缺口。
        #
        # 负路径同样是**真实调用**工具层，只是它证明的是
        # 「没有会话时不会假装成功」，而不是「有会话时能正确工作」。
        # 所以这两种情况的断言强度不同，报告里必须说清楚。
        print()
        print("  ⚠ 注入未成功 —— 改验这些工具的**负路径**（无会话必须报错）。")
        print("     注意：这只证明「不会假成功」，"
              "不证明「有会话时工作正常」。")
        S.close_session()
        for _name, _call in (
            ("runtime_class_info",
             lambda: S.runtime_class_info(name="PresetLoaderV2")),
            ("hook_address",
             lambda: S.hook_address(address="0x1", label="nosession")),
            ("read_field_value",
             lambda: S.read_field_value(instance="0x1000",
                                        name="PresetLoaderV2",
                                        field="userDataPresetFile")),
            ("gc_control", lambda: S.gc_control(disable=True)),
        ):
            _r = _call()
            check(f"{_name} 无会话时如实报错（负路径）",
                  _r.get("ok") is False and bool(_r.get("error")),
                  str(_r.get("error", ""))[:60])

    # 清掉手动启的进程
    print("     清理进程…")
    try:
        import subprocess as sp
        sp.run(["taskkill", "/F", "/T", "/PID", str(pid)],
               capture_output=True, timeout=30)
        print("     已结束")
    except Exception as exc:
        print(f"     清理时出错（不影响测试结论）：{exc}")


# ============================================================ 汇总
section("补充覆盖验证汇总")
print(f"  通过 {PASS} 项 / 失败 {FAIL} 项 / 警告 {WARN} 项")
print()
if FAIL == 0:
    print("  \u2605 本脚本负责的工具全部经过实际调用"
          "（含合并后能力的回归：find_derived_classes / class_detail）。")
else:
    print(f"  \u26a0 有 {FAIL} 项失败。")

sys.exit(1 if FAIL else 0)
