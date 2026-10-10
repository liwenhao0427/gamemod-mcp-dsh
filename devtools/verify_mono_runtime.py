"""Mono 运行时探针验收 —— 证明 Mono 游戏上「注入 → 真实偏移/地址 → Hook」整条链走得通。

与 `verify_mcp_mono.py` 的分工：

| 脚本 | 覆盖 |
|---|---|
| `verify_mcp_mono.py` | 工具层在 Mono 游戏上的**静态**链路（inspect_game → open_game → 查询） |
| 本脚本 | 探针的 **Mono 后端**：能力位、导出真值、真实游戏端到端 |

三层证据（缺一层都不算证明）：

  §1 能力位与探针代码的一致性（纯函数 + 源码静态检查，**不开游戏**）
  §2 四个 Mono 样本的 mono 模块导出真值（PE 解析，**离线**）
     —— 这是「哪些导出有、哪些确实没有」的唯一依据；
     `agent.js` 里那些「四个样本一致核实」的说法由这一节复验
  §3 真实游戏端到端（Archon Soul；样本不存在则记为「无法验证」并跳过）

历史（这一版修掉的三个真缺陷，都在 §1/§3 里留了回归断言）：

  ① `MONO_SIGS` 把数值返回登记成 `'int'`/`'uint32'`，而调用方按 IL2CPP 的约定
     写 `.toInt32()` —— Frida 对 `'int'` 返回的是 **JS number**，于是
     `mono_field_get_offset(f).toInt32()` 抛 `TypeError: not a function`，
     **整个 Mono 后端初始化失败**；另有 4 处在 `try/catch` 里会静默变成 null
     （表现成「字段偏移查不到」）。
  ② 程序集清单被**永久缓存**：Mono 是懒加载，注入后 0.5 秒只枚举到 13 个
     （全是 BepInEx 自己的），缓存下来后 `UnityEngine.*` 永远查不到，
     而工具报的是「类不存在」。
  ③ `session.agent_source()` 把探针源码缓存在模块级全局：长时间运行的
     MCP 服务器改了 `agent.js` 之后**永远**注入旧探针，磁盘上是新代码 ——
     调试的人对着新代码找问题，跑的是旧行为。
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import samples                                              # noqa: E402
samples.add_src_to_path()
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import gamemod_mcp.server as S                              # noqa: E402
from gamemod_mcp.engine import adapt, pe                     # noqa: E402
from gamemod_mcp.runtime import session as sess_mod           # noqa: E402

AGENT_JS = ROOT / "src" / "gamemod_mcp" / "runtime" / "agent.js"

# 用运行时**自己**的枚举当真值，而不是拿「应该有 1 个」当断言：
# 短名 Time 到底有几个顶层类，取决于哪些程序集已加载（实测同一游戏
# 3 秒时 1 个、90 秒时 2 个），所以断言必须与真值对账而不是写死。
_SHORTNAME_TRUTH_JS = r"""
(function () {
  var want = 'Time';
  var out = { nesting_fn: typeof monoFuncs.mono_class_get_nesting_type,
              all: [], top: [], rows: [], raw_all: 0, raw_top: 0, images: 0 };
  var imgs = BE.allImages();
  out.images = imgs.length;
  var seenAll = {}, seenTop = {};
  for (var i = 0; i < imgs.length; i++) {
    var total = 0;
    try {
      var ti = monoFuncs.mono_image_get_table_info(imgs[i].image, 2);
      total = monoFuncs.mono_table_info_get_rows(ti).toInt32();
    } catch (e) { continue; }
    for (var r = 1; r <= total; r++) {
      var k = null;
      try { k = monoFuncs.mono_class_get(imgs[i].image, 0x02000000 | r); }
      catch (e) { continue; }
      if (!k || k.isNull()) { continue; }
      var nm = null;
      try { nm = monoFuncs.mono_class_get_name(k).readUtf8String(); }
      catch (e) { nm = null; }
      if (nm !== want) { continue; }
      var fn = BE.fullNameOf(k);
      var outer = null;
      try { outer = monoFuncs.mono_class_get_nesting_type(k); }
      catch (e) { outer = null; }
      var isTop = (!outer || outer.isNull());
      var tag = imgs[i].name + ' :: ' + fn;
      var ns = '';
      try { ns = monoFuncs.mono_class_get_namespace(k).readUtf8String() || ''; }
      catch (e) { ns = ''; }
      // 按 (程序集名, 全名) 去重：实测同一个类会被枚举到两次，两条的
      // 行号/token/命名空间/程序集名**完全相同**，只有 klass 指针不同 ——
      // 即同一个程序集被加载出了两个映像。raw_* 保留原始条数，便于看见它。
      out.raw_all++;
      if (isTop) { out.raw_top++; }
      if (!seenAll[tag]) {
        seenAll[tag] = true;
        out.all.push(tag);
      }
      if (isTop && !seenTop[fn]) { seenTop[fn] = true; out.top.push(fn); }
      // 逐行留证：行号 / token / 命名空间 / 是否顶层
      out.rows.push({ image: imgs[i].name, row: r,
                      token: '0x' + (0x02000000 | r).toString(16),
                      klass: k.toString(), ns: ns, top: isTop, full_name: fn });
    }
  }
  return out;
})()
"""

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


def _agent_text() -> str:
    return AGENT_JS.read_text(encoding="utf-8")


def _agent_slice(start_pat: str, end_pat: str) -> str:
    """取 agent.js 里 start_pat 到 end_pat 之间的片段（找不到返回空串）。"""
    text = _agent_text()
    m = re.search(start_pat, text)
    if not m:
        return ""
    rest = text[m.end():]
    m2 = re.search(end_pat, rest)
    return rest[:m2.start()] if m2 else rest


# ============================================================ 1. 能力位与代码一致性
section("1. 能力位与探针代码的一致性（不开游戏）")

cap_il2cpp = adapt.capabilities_of("il2cpp")
cap_mono = adapt.capabilities_of("mono")
print(f"  il2cpp.method_address_in_metadata = {cap_il2cpp.get('method_address_in_metadata')}")
print(f"  mono.method_address_in_metadata   = {cap_mono.get('method_address_in_metadata')}")
print(f"  mono.field_offset_in_metadata     = {cap_mono.get('field_offset_in_metadata')}")

# §1 的核心断言：静态元数据**没有**方法地址（只有 token）
check("mono 的能力位承认「方法地址不在元数据里」",
      cap_mono.get("method_address_in_metadata") is False,
      f"实际={cap_mono.get('method_address_in_metadata')}")
check("il2cpp 同样为 False（两个引擎口径一致）",
      cap_il2cpp.get("method_address_in_metadata") is False,
      f"实际={cap_il2cpp.get('method_address_in_metadata')}")
check("mono 的字段偏移也不在元数据里",
      cap_mono.get("field_offset_in_metadata") is False,
      f"实际={cap_mono.get('field_offset_in_metadata')}")
# 非空转：这条说明必须真的写出来，否则「RVA 指向 IL」这个细微差别会被 Agent 误解
mono_notes = " ".join(cap_mono.get("notes") or [])
print(f"  mono 能力说明：{mono_notes[:160]}")
check("能力说明里点出了「元数据 RVA 指向 IL 字节码、不是机器码」",
      "IL" in mono_notes and "字节码" in mono_notes,
      mono_notes[:120])

# ---- 探针源码的静态一致性
text = _agent_text()
api_block = re.search(r"var MONO_API_NAMES = \[(.*?)\];", text, re.S)
sig_block = re.search(r"var MONO_SIGS = \{(.*?)\n    \};", text, re.S)
check("agent.js 里能定位 MONO_API_NAMES 与 MONO_SIGS（非空转）",
      bool(api_block) and bool(sig_block))
api_names = re.findall(r"'([a-zA-Z0-9_]+)'", api_block.group(1) if api_block else "")
sig_names = re.findall(r"(?m)^\s*([a-zA-Z0-9_]+):", sig_block.group(1) if sig_block else "")
print(f"  MONO_API_NAMES={len(api_names)} 条  MONO_SIGS={len(sig_names)} 条")
check("两个表一一对应（不多不少）",
      sorted(api_names) == sorted(sig_names),
      f"只在 API 表: {sorted(set(api_names) - set(sig_names))[:5]} / "
      f"只在签名表: {sorted(set(sig_names) - set(api_names))[:5]}")
check("登记数量合理（非空转：至少 40 个导出）", len(api_names) >= 40, str(len(api_names)))

# ---- 数值返回类型必须统一（踩过的坑）
numeric_returns = re.findall(r"(?m)^\s*([a-zA-Z0-9_]+): \['(int|uint32|bool|long|uint64)',",
                             sig_block.group(1) if sig_block else "")
print(f"  返回类型登记为 int/uint32/bool 的条目：{numeric_returns}")
check("MONO_SIGS 里没有 int/uint32/bool 返回（数值统一按 pointer + toInt32）",
      not numeric_returns,
      f"{len(numeric_returns)} 条：{numeric_returns[:6]}")

# ---- 每个数值 API 的调用点都必须带 toInt32/toUInt32
NUMERIC_APIS = [
    "mono_field_get_offset", "mono_class_get_flags", "mono_field_get_flags",
    "mono_method_get_flags", "mono_method_get_token",
    "mono_signature_get_param_count", "mono_signature_is_instance",
    "mono_image_get_table_rows", "mono_class_num_fields", "mono_class_num_methods",
    "mono_class_instance_size", "mono_class_get_type_token",
]
call_sites = 0
bad_sites: list[str] = []
for api in NUMERIC_APIS:
    for m in re.finditer(re.escape(f"monoFuncs.{api}("), text):
        # 取该调用之后 200 字符内是否出现 toInt32/toUInt32
        tail = text[m.end():m.end() + 200]
        call_sites += 1
        if ".toInt32()" not in tail and ".toUInt32()" not in tail:
            bad_sites.append(api)
print(f"  数值 API 调用点 {call_sites} 处，未做截断的 {len(bad_sites)} 处")
check("数值 API 的每个调用点都按整数取值（非空转：调用点 > 0）",
      call_sites > 0 and not bad_sites,
      f"调用点={call_sites} 未截断={bad_sites}")

# ---- 回退分支的守卫（那次 TypeError 的直接原因）
check("字符串布局回退分支的守卫里含 mono_field_get_offset",
      "monoFuncs.mono_class_get_fields && monoFuncs.mono_field_get_name &&\n"
      "            monoFuncs.mono_field_get_offset" in text
      or re.search(r"mono_class_get_fields && monoFuncs\.mono_field_get_name &&\s*\n?\s*"
                   r"monoFuncs\.mono_field_get_offset", text) is not None)
# 字符串布局探测失败不许拖垮初始化
check("字符串布局探测被 try/catch 包住（失败只降级，不掐断 init）",
      re.search(r"try \{\s*\n\s*monoStringLayout = monoDiscoverStringLayout\(\);", text)
      is not None)

# ---- RPC 层必须走 BE.*（引擎差异只允许在 backend 里）
rpc_tail = text[text.index("rpc.exports = {"):] if "rpc.exports = {" in text else ""
direct = re.findall(r"funcs\.il2cpp_[a-zA-Z0-9_]+", rpc_tail)
print(f"  rpc.exports 之后直接调 funcs.il2cpp_* 的次数：{len(direct)}")
check("RPC 层不再直接调 IL2CPP 导出（统一走后端对象）",
      not direct and ("be." in rpc_tail or "BE." in rpc_tail),
      f"direct={sorted(set(direct))[:5]}")
check("RPC 层确实经过后端对象（非空转）",
      rpc_tail.count("be.") >= 8, f"be.* 出现 {rpc_tail.count('be.')} 次")
# 非空转反证：同一个正则在后端实现区必须**大量命中**，
# 否则「RPC 层 0 次」可能只是因为正则写错了（永远匹配不上）。
# 正则提到 f-string 外面：`pyproject.toml` 声明 requires-python >=3.10，
# 而 3.12 之前 f-string 的 {} 里不允许出现反斜杠。
# 顺带订正一处真错：原来 print 里写的是 `funcs\\.il2cpp_`（**两个**反斜杠），
# 在非 raw 的 Python 字符串里那是「字面反斜杠 + 任意字符」，
# 匹配不到 agent.js 里实际存在的 `funcs.il2cpp_`；而下面断言用的是
# 单个反斜杠（正确）。于是「打印 0 次 / 断言 >20」自相矛盾 ——
# 现在两边共用同一个常量，不可能再各写各的。
backend_zone = (text[:text.index("rpc.exports = {")]
                if "rpc.exports = {" in text else "")
_BACKEND_CALL_RE = r"funcs\.il2cpp_[a-zA-Z0-9_]+"
_backend_hits = len(re.findall(_BACKEND_CALL_RE, backend_zone))
print(f"  同一正则在后端实现区的命中次数：{_backend_hits}")
check("「RPC 层 0 次」不是正则写错（后端实现区里同一正则命中很多）",
      _backend_hits > 20,
      f"后端区命中 {_backend_hits} 次")

# ---- 假 RVA 防护
desc = _agent_slice(r"function describeAddress\(addr\)", r"\n    \}")
print("  describeAddress 片段：")
for line in desc.splitlines()[:16]:
    print(f"    {line.strip()}")
check("describeAddress 只在 in_module 为真时才输出 RVA",
      "in_module" in desc and desc.index("in_module") < desc.index("rva_hex")
      if "rva_hex" in desc else False)

# ---- 程序集清单必须按 RPC 刷新
check("exports 包装里清空程序集缓存（Mono 懒加载，跨 RPC 缓存会看不到新程序集）",
      "monoImagesCache = null;" in text and "for (var _k in rpc.exports)" in text)

# ---- 短名解析（与 IL2CPP 对齐）：三个按 TypeDef 取类的导出 + 兜底路径
for need in ("mono_class_get", "mono_image_get_table_info", "mono_table_info_get_rows"):
    check(f"登记了短名解析所需的导出 {need}", need in api_names)
check("RPC 包装里也清空短名索引缓存",
      "monoTypeIndexCache = null;" in text)
resolve_slice = _agent_slice(r"function monoResolveClass\(", r"\n    function monoResolveClassOrFail")
print(f"  monoResolveClass 片段长度：{len(resolve_slice)}")
check("monoResolveClass 里有短名兜底路径（精确写法查不到才走）",
      "monoBuildTypeIndex" in resolve_slice and "短名扫描" in resolve_slice)
# 非空转：兜底必须在「精确写法循环之后」，否则会拖慢每次查类
if "monoBuildTypeIndex" in resolve_slice and "mono_class_from_name" in resolve_slice:
    check("短名兜底排在精确写法之后（不在热路径上）",
          resolve_slice.index("mono_class_from_name") < resolve_slice.index("monoBuildTypeIndex"))
else:
    check("短名兜底排在精确写法之后（不在热路径上）", False, "片段里缺少关键调用")

# ---- GC 控制：Mono 上如实报不支持（IL2CPP 上则真的调 il2cpp_gc_disable）
gc_slice = _agent_slice(r"function monoGcControl", r"\n    // =+ 后端装配")
if len(gc_slice) < 200:
    gc_slice = text[text.index("function monoGcControl"):][:900]
print(f"  monoGcControl 片段长度：{len(gc_slice)}")
check("Mono 的 gcControl 走 fail(...) 而不是假装成功",
      "return fail(" in gc_slice, gc_slice[:80])
check("Mono 的 gcControl 说明缺的是 mono_gc_disable / mono_gc_enable",
      "mono_gc_disable" in gc_slice and "mono_gc_enable" in gc_slice)
check("Mono 的 gcControl 带 unsupported: true（客户端可据此区分「不支持」与「出错」）",
      "unsupported: true" in gc_slice)
# 非空转反证：IL2CPP 后端**必须**真的去调 il2cpp_gc_disable，
# 否则「如实报不支持」可能只是两边都没实现。
il2cpp_zone = _agent_slice(r"var BE_IL2CPP = \{", r"\n    var BE_MONO = \{")
print(f"  IL2CPP 后端片段长度：{len(il2cpp_zone)}，"
      f"出现 il2cpp_gc_disable：{'il2cpp_gc_disable' in il2cpp_zone}")
check("IL2CPP 侧确实实现了 GC 暂停（对照，证明 Mono 是真的缺导出）",
      "il2cpp_gc_disable" in il2cpp_zone and "gcSupported: true" in il2cpp_zone)

# ---- 探针源码不许被缓存（改了 agent.js 必须立刻生效）
section("1b. 探针源码读取不许缓存（改了 agent.js 必须立刻生效）")
tmp = Path(tempfile.mkdtemp(prefix="gamemod-agent-src-"))
try:
    fake = tmp / "agent.js"
    fake.write_text("// 第一版\n", encoding="utf-8")
    real_path = sess_mod._AGENT_PATH
    sess_mod._AGENT_PATH = fake
    first = sess_mod.agent_source()
    fake.write_text("// 第二版\n", encoding="utf-8")
    second = sess_mod.agent_source()
    print(f"  第一次读：{first.strip()!r}")
    print(f"  改文件后再读：{second.strip()!r}")
    check("改文件后读到的是新内容（非空转：内容确实变了）",
          first.strip() == "// 第一版" and second.strip() == "// 第二版",
          f"{first.strip()!r} → {second.strip()!r}")
finally:
    sess_mod._AGENT_PATH = real_path
    try:
        fake.unlink()
        tmp.rmdir()
    except Exception:
        pass

# ============================================================ 2. 四个样本的导出真值
section("2. 四个 Mono 样本的 mono 模块导出真值（PE 解析，离线）")

mono_games = samples.mono_games()
print(f"  可用 Mono 样本：{len(mono_games)} 个")
for g in mono_games:
    print(f"    {g}")

# 「已核实不存在」的导出：agent.js 的注释里点名说没有的那几个。
# 它们缺席是**有意的设计依据**，所以必须逐个复验，不能靠记忆。
KNOWN_ABSENT = [
    "mono_gc_disable", "mono_gc_enable",
    "mono_class_is_interface", "mono_class_is_abstract",
    "mono_method_get_param_count", "mono_class_get_declaring_type",
]

if not mono_games:
    print("  \u26a0\ufe0f  没有可用的 Mono 样本，跳过 §2（记为无法验证）")
    WARN += 1
else:
    for game in mono_games:
        dlls = sorted(game.glob("MonoBleedingEdge/EmbedRuntime/mono-*.dll"))
        if not dlls:
            check(f"{game.name}：找到 mono 运行时 dll", False, "没找到 mono-*.dll")
            continue
        dll = dlls[0]
        info = pe.parse(dll)
        exports = {e.name for e in info.exports}
        print()
        print(f"  --- {game.name} ---")
        print(f"      {dll.name}：{len(exports)} 个导出")
        missing = [n for n in api_names if n not in exports]
        check(f"{game.name}：探针登记的全部 {len(api_names)} 个导出都在",
              not missing, f"缺 {missing[:8]}")
        still_there = [n for n in KNOWN_ABSENT if n in exports]
        print(f"      已核实不存在的导出：{KNOWN_ABSENT}")
        check(f"{game.name}：'已核实不存在'的那几个确实不存在",
              not still_there, f"居然有：{still_there}")
        # 非空转：编一个名字必须报不存在，且导出表本身非空
        check(f"{game.name}：假名字确实不在导出表里（非空转）",
              "mono_field_get_offset_typo" not in exports and len(exports) > 1000,
              f"导出总数={len(exports)}")

# ============================================================ 3. 真实游戏端到端
section("3. 真实游戏端到端（Archon Soul）")

mono_game = samples.find_mono()
if not samples.require(mono_game, "Mono 样本 Archon Soul"):
    print("  \u26a0\ufe0f  样本不存在，§3 记为「无法验证」并跳过（不算失败）")
    WARN += 1
else:
    op = S.open_game(game_dir=str(mono_game))
    check("open_game 认出 mono 引擎", op.get("engine") == "mono",
          f"实际={op.get('engine')}")

    # 开场先清掉可能残留的同名进程。
    #
    # 为什么必须做：`launch_game` 对「已有同名进程在跑」是**故意拒绝**的
    # （Unity 单实例锁，新进程不会加载 mono/GameAssembly，只会白等到超时）。
    # 而本脚本跑完只 `close_session()`（那只是 detach，**不结束游戏进程**），
    # 于是**第二次运行必然失败**：报「已经有同名进程在运行」。
    # 实测就踩到了 —— 上一轮留下的 Archon Soul.exe 让下一轮的
    # `launch_game` 直接 ok:False，后面所有运行时断言全部连坐报红。
    #
    # 只清**本项目样本游戏自己**那一个 exe（按精确路径核对），
    # 不做按进程名的宽泛清理 —— 那种写法会误伤同名的无关进程。
    _exe_path = Path(str(op.get("exe") or ""))
    if _exe_path.is_file():
        try:
            import subprocess as _sp
            _found = _sp.run(
                ["tasklist", "/FI", f"IMAGENAME eq {_exe_path.name}",
                 "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=30,
            )
            for _line in (_found.stdout or "").splitlines():
                if '"' not in _line:
                    continue
                _parts = [p.strip('"') for p in _line.split('","')]
                if len(_parts) < 2 or not _parts[1].isdigit():
                    continue
                _stale = int(_parts[1])
                # 核对可执行文件路径，确保结束的确实是本样本游戏
                _m = _sp.run(
                    ["powershell", "-NoProfile", "-Command",
                     f"(Get-CimInstance Win32_Process -Filter "
                     f"'ProcessId={_stale}').ExecutablePath"],
                    capture_output=True, text=True, timeout=30,
                )
                _path = (_m.stdout or "").strip()
                if _path and os.path.normcase(_path) == os.path.normcase(str(_exe_path)):
                    print(f"  清理上一轮残留的样本游戏进程 pid={_stale}")
                    _sp.run(["taskkill", "/F", "/T", "/PID", str(_stale)],
                            capture_output=True, timeout=30)
            time.sleep(1.0)
        except Exception as exc:                       # noqa: BLE001
            print(f"  残留检查跳过：{exc}")

    lg = S.launch_game()
    init = lg.get("init") or {}
    print(f"  launch_game ok={lg.get('ok')} pid={lg.get('pid')} "
          f"warning={lg.get('warning')!r}")
    print(f"  init: engine={init.get('engine')} module={init.get('module')} "
          f"api_available={init.get('api_available')} attempts={init.get('attempts')} "
          f"waited={init.get('waited')}")
    print(f"  api_missing={init.get('api_missing')}")
    print(f"  string_layout={init.get('string_layout')}")
    if not lg.get("ok"):
        # 启动失败就把原因打全（含 hint），否则后面几十条断言会一起报红，
        # 真正的原因被埋在中间。
        print(f"  ❌ launch_game 失败：{str(lg.get('error'))[:200]}")
        print(f"     hint={str(lg.get('hint'))[:200]}")
    _spawned_pid = lg.get("pid")
    check("Mono 游戏上探针初始化成功", init.get("ok") is True,
          str(init.get("error"))[:120])
    check("探针报告引擎为 mono", init.get("engine") == "mono",
          f"实际={init.get('engine')}")
    check("解析到的是 mono 运行时模块",
          "mono" in str(init.get("module", "")).lower(),
          str(init.get("module")))
    check("没有导出缺失（api_missing 为空）",
          not (init.get("api_missing") or []),
          str(init.get("api_missing")))
    # 字符串布局是**运行时查出来**的，不是硬编码
    sl = init.get("string_layout") or {}
    print(f"  字符串长度字段：{sl.get('field')} @ {sl.get('offset')} "
          f"（{sl.get('via')}）")
    check("字符串布局是运行时查出来的（有字段名与偏移）",
          bool(sl.get("field")) and isinstance(sl.get("offset"), int),
          str(sl))
    check("查到的偏移是正数且在合理范围（实测 _stringLength @16）",
          isinstance(sl.get("offset"), int) and 0 < sl["offset"] < 64,
          str(sl.get("offset")))

    # 等 Unity 的程序集加载出来（Mono 懒加载）
    #
    # 历史订正：这里原本断言「image_count 随游戏运行变多」来证明懒加载。
    # 探针补上「注入后等程序集加载稳定」之后，`launch_game` 返回时数量**已经**
    # 稳定，再测 90 秒也只会得到 164 → 164 —— 断言变成了空转（实测就是这么红的）。
    # 懒加载的证据因此换到两处更直接的地方：
    #   · `init.assemblies`：等的过程里采样了 N 次、数量从十几涨到 144；
    #   · 注入后**立刻**就能解析出 UnityEngine.Time（修复前这里必然失败）。
    print("  等 UnityEngine.* 程序集加载（Mono 懒加载）…")
    asm = init.get("assemblies") or {}
    print(f"  init.assemblies = {asm}")
    check("注入时确实等了程序集加载（采样多轮）",
          (asm.get("samples") or 0) > 1, str(asm))
    check("等完之后程序集数量是可用的规模（不是刚注入时的十几个）",
          (asm.get("images") or 0) > 20, str(asm))
    t0 = time.time()
    counts = []
    tinfo = {}
    first_ok = None
    # 不能「一 ok 就 break」：实测同一个类会在运行约 90 秒后被枚举出第二遍
    # （同一个程序集加载出两个映像：行号/token/命名空间全同，只有 klass 指针
    # 不同），那才是这条断言真正要覆盖的时刻。提前跳出会让它空转通过。
    while time.time() - t0 < 90:
        counts.append((S.runtime_info().get("image_count") or 0))
        tinfo = S.runtime_class_info(name="Time")
        if tinfo.get("ok") and first_ok is None:
            first_ok = time.time() - t0
        time.sleep(3)
    print(f"  image_count 轨迹：{counts[0]} → {counts[-1]}（等待 {time.time()-t0:.1f}s）")
    print(f"  首次解析成功于 t={first_ok if first_ok is None else round(first_ok, 1)}s")
    # 90 秒里数量应当**稳定**（不再有大批程序集涌进来）—— 若还在猛涨，
    # 说明「等稳定」的判据（连续 4 次不变）太松，得调紧。
    check("注入后程序集数量已稳定（90 秒内涨幅 < 30%）",
          counts[-1] <= max(counts[0], 1) * 1.3,
          f"{counts[0]} → {counts[-1]}")
    check("注入后**立刻**就能解析 UnityEngine.Time（修复前必然失败）",
          first_ok is not None and first_ok < 15.0,
          f"首次成功于 t={first_ok}s")
    check("UnityEngine.Time 能解析出来（短名兜底）", tinfo.get("ok") is True,
          str(tinfo.get("error"))[:120])
    # 失败时把候选打出来 —— 否则「命中 2 个同名类」这种报错要靠再跑一次才知道是谁
    if not tinfo.get("ok"):
        cands = (tinfo.get("detail") or {}).get("candidates") or []
        print(f"    ⚠️ 未唯一命中，候选 {len(cands)} 个：")
        for c in cands:
            print(f"       - {c.get('full_name')} @ {c.get('image')} "
                  f"({c.get('via')})")
    # 短名解析的正确性**不能靠「程序集加载得快」**：实测同一个游戏，
    # 启动 3 秒时 `Time` 只命中 UnityEngine.Time（InputForUI 模块还没加载），
    # 90 秒时多出一个同名类 → 报歧义。所以这里用**运行时自己的枚举当真值**：
    # 短名有几个顶层类，工具就必须给出对应的答案（唯一 → ok；
    # 多个 → 如实报歧义并列出同样多的候选）。两个方向都卡住。
    truth = (S.eval_script(code=_SHORTNAME_TRUTH_JS).get("value") or {})
    top = truth.get("top") or []
    all_hits = truth.get("all") or []
    print(f"  运行时枚举短名 Time：去重后 {len(all_hits)} 个 / 其中顶层 {len(top)} 个"
          f"（原始条数 {truth.get('raw_all')} / 顶层 {truth.get('raw_top')}）")
    for x in all_hits:
        print(f"     - {x}")
    for r_ in (truth.get("rows") or []):
        print(f"     · {r_.get('image')} TypeDef 行 {r_.get('row')} "
              f"token={r_.get('token')} ns={r_.get('ns')!r} "
              f"顶层={r_.get('top')} klass={r_.get('klass')}")
    check("mono_class_get_nesting_type 已绑定（顶层过滤依赖它，缺了会静默失效）",
          truth.get("nesting_fn") == "function", str(truth.get("nesting_fn")))
    # 回归（实测缺陷）：同一个程序集被枚举出两个 MonoImage* 时，同一个类会
    # 出现两条一模一样的候选，把可解析的查询变成「命中 2 个同名类」——
    # 连显式给 namespace 也消不掉。所以候选里不许有重复。
    if tinfo.get("ok") is False:
        cands0 = (tinfo.get("detail") or {}).get("candidates") or []
        shown = [f"{c.get('full_name')} @ {c.get('image')}" for c in cands0]
        check("候选里没有两条一模一样的记录（同一程序集的多个映像已合并）",
              len(shown) == len(set(shown)), str(shown[:4]))
    if len(top) == 1:
        check("短名唯一时能解析，且指向运行时枚举出的那个类",
              tinfo.get("ok") is True and tinfo.get("full_name") == top[0],
              f"ok={tinfo.get('ok')} full_name={tinfo.get('full_name')} 期望={top[0]}")
    elif len(top) > 1:
        cands = (tinfo.get("detail") or {}).get("candidates") or []
        check("短名不唯一时如实报歧义，候选数与运行时枚举一致",
              tinfo.get("ok") is False
              and (tinfo.get("detail") or {}).get("ambiguous") is True
              and len(cands) == len(top),
              f"ok={tinfo.get('ok')} 候选={len(cands)} 期望={len(top)}")
    else:
        check("运行时能枚举出顶层 Time（非空转前提）", False, "枚举到 0 个")
    # 带命名空间必须唯一命中 —— 这条与程序集加载时机无关，是确定性的
    t2 = S.runtime_class_info(name="Time", namespace="UnityEngine")
    print(f"  带命名空间：ok={t2.get('ok')} full_name={t2.get('full_name')}")
    check("给了 namespace 就能确定性地命中 UnityEngine.Time",
          t2.get("ok") is True and t2.get("full_name") == "UnityEngine.Time",
          f"ok={t2.get('ok')} full_name={t2.get('full_name')}")
    tinfo = t2 if t2.get("ok") else tinfo

    if tinfo.get("ok"):
        print(f"  Time: full_name={tinfo.get('full_name')} parent={tinfo.get('parent')} "
              f"instance_size={tinfo.get('instance_size')} "
              f"字段 {tinfo.get('field_count')} 个 / 方法 {tinfo.get('method_count')} 个")
        ms = [m for m in (tinfo.get("methods") or [])
              if m.get("name") == "get_frameCount"]
        check("找到 Time.get_frameCount", bool(ms))
        if ms:
            m = ms[0]
            print(f"    get_frameCount: token={m.get('token')} pointer={m.get('pointer')} "
                  f"in_module={m.get('in_module')} rva={m.get('rva')} "
                  f"rva_hex={m.get('rva_hex')} return_type={m.get('return_type')}")
            check("方法给出了真实机器码地址（Mono 靠 mono_compile_method）",
                  bool(m.get("pointer")) and str(m.get("pointer")) != "0x0",
                  str(m.get("pointer")))
            # 关键：Mono 的 JIT 代码不在 mono 模块内 → 不许给假 RVA
            check("JIT 代码不在模块内，因此**不给** RVA（不给假数据）",
                  m.get("in_module") is False and m.get("rva") is None
                  and m.get("rva_hex") is None,
                  f"in_module={m.get('in_module')} rva={m.get('rva')}")
            check("给出了 token（用于与静态元数据对账）",
                  bool(m.get("token")), str(m.get("token")))

    # 继承链：Transform 自己 0 个字段，必须能看到基类的。
    # 这里显式给 namespace —— 短名是否唯一同样受程序集加载时机影响，
    # 而这一段要验证的是**继承链**，不该被短名的歧义干扰。
    tr = S.runtime_class_info(name="Transform", namespace="UnityEngine")
    print(f"  Transform: ok={tr.get('ok')} full_name={tr.get('full_name')} "
          f"parent={tr.get('parent')} own_field_count={tr.get('own_field_count')} "
          f"field_count={tr.get('field_count')}")
    if tr.get("ok"):
        inherited = [f for f in (tr.get("fields") or []) if f.get("inherited_from")]
        print(f"    继承来的字段 {len(inherited)} 个，例如："
              f"{[(f.get('name'), f.get('inherited_from')) for f in inherited[:3]]}")
        check("Transform 的字段里含继承自基类的（自己 0 个字段也能查到）",
              len(inherited) > 0,
              f"own={tr.get('own_field_count')} inherited={len(inherited)}")
        check("继承来的字段带 inherited_from 标注",
              all(f.get("inherited_from") for f in inherited) if inherited else False,
              str([f.get("name") for f in inherited[:3]]))

    # 非空转反证：短名兜底不能变成「什么都能命中」
    bogus = S.runtime_class_info(name="TimeXYZNotExistAnywhere")
    print(f"  非空转反证：TimeXYZNotExistAnywhere -> ok={bogus.get('ok')} "
          f"error={str(bogus.get('error'))[:60]}")
    check("不存在的类名仍然查不到（短名兜底不是通配）",
          bogus.get("ok") is False, str(bogus.get("error"))[:80])

    # 静态 token ↔ 运行时 token 对账
    cd = S.class_detail(full_name="RunLoadingManager")
    static_tokens = {m.get("name"): m.get("token") for m in (cd.get("methods") or [])}
    rt = S.runtime_class_info(name="RunLoadingManager")
    print(f"  RunLoadingManager: 静态 token={cd.get('token')} / 运行时 token={rt.get('token')}")
    if rt.get("ok"):
        agree = []
        for m in (rt.get("methods") or []):
            st = static_tokens.get(m.get("name"))
            if st and str(st).lower() == str(m.get("token")).lower():
                agree.append(m.get("name"))
        print(f"    同名方法 token 一致：{len(agree)} 个 {agree[:5]}")
        check("静态 token == 运行时 token（落点正确的唯一证据）",
              len(agree) > 0, f"一致 {len(agree)} 个")
    else:
        check("RunLoadingManager 能在运行时解析", False,
              str(rt.get("error"))[:120])

    # Hook 端到端（同样显式给 namespace，避免被短名歧义干扰；
    # 短名解析本身已在上面用运行时真值单独卡过）
    hk = S.hook_method(name="Time", namespace="UnityEngine", method="get_frameCount",
                       max_calls=50)
    print(f"  hook_method: ok={hk.get('ok')} chosen={hk.get('chosen')} "
          f"retval_type={hk.get('retval_type')}")
    check("Mono 上能挂上 Hook", hk.get("ok") is True, str(hk.get("error"))[:120])
    if hk.get("ok"):
        time.sleep(1.5)
        hs = S.hook_status()
        ev = S.drain_events(limit=5)
        print(f"  hook_status={hs}")
        print(f"  事件数={ev.get('returned')}")
        check("Hook 真的被触发了（事件数 > 0）",
              (ev.get("returned") or 0) > 0, str(ev.get("returned")))
        S.unhook()

    # gc_control：Mono 上必须如实报不支持
    gc = S.gc_control(disable=True)
    print(f"  gc_control: ok={gc.get('ok')} error={str(gc.get('error'))[:160]}")
    check("gc_control 在 Mono 上如实报不支持（没有假成功）",
          gc.get("ok") is False and "mono_gc" in str(gc.get("error", "")),
          str(gc.get("error"))[:120])
    if gc.get("ok"):
        S.gc_control(disable=False)

    # ------------------------------------------------------------------
    # 其余运行时工具（inject_process / find_hook_target / hook_address /
    # read_memory / write_memory / scan_pattern / read_field_value）
    #
    # 为什么补在这里：这些工具此前**只有 IL2CPP 侧**验过
    # （`verify_mcp_extra.py` / `verify_mcp_tools.py` 都用 Garden of Witches）。
    # 该样本不在本机时那两条脚本整段跳过，于是动态覆盖率门禁里它们长期是空的
    # —— 实测基线只有 21/38。而它们**与引擎无关**（读内存 / 扫特征码 /
    # 按绝对地址挂钩），Mono 会话照样能跑，且本机有 Mono 样本，稳定可复验。
    #
    # ------------------------------------------------------------------
    # 其余运行时工具（inject_process / find_hook_target / hook_address /
    # read_memory / write_memory / scan_pattern / read_field_value）
    #
    # 为什么补在这里：这些工具此前**只有 IL2CPP 侧**验过
    # （`verify_mcp_extra.py` / `verify_mcp_tools.py` 都用 Garden of Witches）。
    # 该样本不在本机时那两条脚本整段跳过，于是动态覆盖率门禁里它们长期是空的
    # —— 实测基线只有 21/38。而它们**与引擎无关**（读内存 / 扫特征码 /
    # 按绝对地址挂钩），Mono 会话照样能跑，且本机有 Mono 样本，稳定可复验。
    section("3b. 其余运行时工具（Mono 会话下，与引擎无关的那几个）")

    # ---- inject_process：目标不存在时必须**如实失败且不动现有会话**
    #
    # 这里用**一个不存在的 pid**（不是本进程 pid）。两个原因：
    #   · 本进程 pid 是真进程，Frida 能 attach 上去 —— 那会把会话从游戏
    #     切到我们自己身上，后续断言全部失去意义（实测踩过）；
    #   · 不存在的 pid 才能测出下面这条**真缺陷的回归**：
    #     修复前 `inject_process(pid=不存在)` 是「先断开旧会话、再去连新目标」，
    #     于是返回失败的同时**把正在用的游戏会话弄没了** ——
    #     调用方只是想试一个 pid，却发现所有 runtime_* 都报「没有已注入的会话」。
    _live_before = (S.get_session().get("runtime") or {}).get("pid")
    _bad_pid = 999999
    _bad_inj = S.inject_process(pid=_bad_pid, auto_find=False)
    print(f"  inject_process(pid={_bad_pid}): ok={_bad_inj.get('ok')} "
          f"error={str(_bad_inj.get('error'))[:70]}")
    check("inject_process 对不存在的 pid 如实失败（不假成功）",
          _bad_inj.get("ok") is False and bool(_bad_inj.get("error")),
          str(_bad_inj.get("error"))[:100])
    check("失败时如实说明「原会话未受影响」",
          _bad_inj.get("kept_pid") == _live_before
          and "未受影响" in str(_bad_inj.get("hint") or ""),
          f"kept_pid={_bad_inj.get('kept_pid')} hint={str(_bad_inj.get('hint'))[:60]}")
    # 关键回归：旧会话必须**还在**
    _live_after = (S.get_session().get("runtime") or {}).get("pid")
    check("失败的注入**没有**把正在用的会话弄丢（旧会话仍在）",
          bool(_live_after) and _live_after == _live_before,
          f"注入前 pid={_live_before}，注入后 pid={_live_after}"
          "（不一致 = 失败还带副作用）")

    # ---- find_hook_target：拿真实绝对地址
    _tgt = S.find_hook_target(name="Time", method="get_frameCount",
                              namespace="UnityEngine")
    print(f"  find_hook_target: ok={_tgt.get('ok')} "
          f"候选={len(_tgt.get('candidates') or [])}")
    check("find_hook_target 返回 ok", _tgt.get("ok") is True,
          str(_tgt.get("error"))[:100])
    _cands = _tgt.get("candidates") or []
    _addr = _cands[0].get("address") if _cands else None
    if _cands:
        _c0 = _cands[0]
        print(f"     {_c0.get('name')} address={_addr} rva={_c0.get('rva_hex')} "
              f"in_module={_c0.get('in_module')} token={_c0.get('token')}")
        check("候选带可直接挂钩的绝对地址", bool(_addr), f"{_addr!r}")
        # Mono 的关键差异：JIT 代码不在 mono 模块内 → 不许给假 RVA
        check("Mono 上 in_module=False 且**不给**假 RVA",
              _c0.get("in_module") is False and _c0.get("rva") is None,
              f"in_module={_c0.get('in_module')} rva={_c0.get('rva')}")

    # ---- hook_address：按绝对地址挂钩（与 hook_method 不同的代码路径）
    if _addr:
        _ha = S.hook_address(address=_addr, label="mono.hook_address",
                             max_calls=100)
        print(f"  hook_address({_addr}): ok={_ha.get('ok')} "
              f"hook_id={_ha.get('hook_id')} error={str(_ha.get('error'))[:60]}")
        check("hook_address 返回 ok", _ha.get("ok") is True,
              str(_ha.get("error"))[:100])
        if _ha.get("ok"):
            time.sleep(1.0)
            _hev = S.drain_events(limit=10)
            print(f"     hook_address 事件数={_hev.get('returned')}")
            S.unhook()

    # ---- read_memory / write_memory：在**自建缓冲区**上验（不碰游戏内存）
    #
    # 为什么不用游戏模块头：写游戏内存有副作用，而这两个工具的正确性
    # （地址解析、hex 编解码、宽度）在一块自己分配的内存上就能完整验证。
    _buf = S.eval_script(
        "(function(){"
        "  globalThis.__mono_verify_buf = Memory.alloc(64);"
        "  globalThis.__mono_verify_buf.writeByteArray([0x11,0x22,0x33,0x44]);"
        "  return globalThis.__mono_verify_buf.toString();"
        "})()"
    ).get("value")
    check("分配了自建缓冲区", bool(_buf), f"{_buf!r}")
    if _buf:
        _rm = S.read_memory(address=_buf, size=4)
        print(f"  read_memory({_buf}, 4): ok={_rm.get('ok')} hex={_rm.get('hex')}")
        check("read_memory 返回 ok", _rm.get("ok") is True,
              str(_rm.get("error"))[:80])
        check("read_memory 读回写入的字节", (_rm.get("hex") or "").lower()
              == "11223344", f"实际={_rm.get('hex')!r}")
        # 写回一组新字节，再读出来核对（写→读闭环）
        _wm = S.write_memory(address=_buf, hex_data="deadbeef")
        print(f"  write_memory({_buf}, 'deadbeef'): ok={_wm.get('ok')} "
              f"error={str(_wm.get('error'))[:60]}")
        check("write_memory 返回 ok", _wm.get("ok") is True,
              str(_wm.get("error"))[:80])
        _rm2 = S.read_memory(address=_buf, size=4)
        check("写进去的字节能原样读回（写→读闭环）",
              (_rm2.get("hex") or "").lower() == "deadbeef",
              f"实际={_rm2.get('hex')!r}")

    # ---- scan_pattern：兜底手段，但必须真的能扫
    #
    # 怎么做到「非空转」：先从**目标模块自己的内存**里读一小段字节，
    # 再把这段字节当特征码去扫同一个模块 —— 这些字节一定存在
    # （就在我们刚读的那个地址上），扫不到就说明扫描路径坏了，
    # 而不是「游戏里恰好没有这个模式」。
    #
    # 注意不能拿前面 `Memory.alloc` 的缓冲区当特征码：那段内存在
    # Frida 自己分配的页里，**不在目标模块内**，而 scan_pattern 只扫
    # 目标模块内的可读段 —— 扫不到是正确行为，断言会假红（实测踩过）。
    _ri = S.runtime_info()
    _mod_name = _ri.get("module") or ""
    _mod_base = _ri.get("base") or _ri.get("module_base") or ""
    print(f"  目标模块={_mod_name} base={_mod_base}")
    if _mod_base:
        # 取模块头之后 4 字节（模块头通常是 MZ/PE，取靠后一点更有代表性）
        _probe_at = hex(int(_mod_base, 16) + 0x1000)
        _probe = S.read_memory(address=_probe_at, size=4)
        _probe_hex = (_probe.get("hex") or "").lower()
        if _probe.get("ok") and len(_probe_hex) == 8:
            _pat = " ".join(_probe_hex[i:i + 2] for i in range(0, 8, 2)).upper()
            _sp = S.scan_pattern(pattern=_pat, module=_mod_name, max_results=64)
            _n = len(_sp.get("results") or [])
            print(f"  scan_pattern('{_pat}', module={_mod_name}): "
                  f"ok={_sp.get('ok')} matched={_sp.get('matched')} "
                  f"results={_n}")
            check("scan_pattern 返回 ok", _sp.get("ok") is True,
                  str(_sp.get("error"))[:100])
            check("扫到了刚从该模块读出的已知字节（非空转，证明扫描真的在跑）",
                  _n > 0, f"results={_n}（期望 ≥1，含 {_probe_at}）")
            check("报出的 scanned_module 就是被扫的模块",
                  _sp.get("scanned_module") == _mod_name,
                  f"实际={_sp.get('scanned_module')!r}")
            # 命中的地址里应当包含我们读的那个地址（同源证据）
            _addrs = {str(r.get("address", "")).lower()
                      for r in (_sp.get("results") or [])}
            check("命中列表里含我们读取的那个地址",
                  _probe_at.lower() in _addrs,
                  f"{_probe_at} in {sorted(_addrs)[:4]}")
        else:
            check("能读出模块内 4 字节用于构造特征码", False,
                  f"hex={_probe_hex!r} err={str(_probe.get('error'))[:60]}",
                  warn_only=True)
    else:
        check("runtime_info 给出模块基址（scan_pattern 的前置）", False,
              f"module={_mod_name!r} base={_mod_base!r}", warn_only=True)

    # ---- read_field_value：伪造实例 + 真实字段偏移（与 IL2CPP 侧同一手法）
    #
    # 选一个**游戏自有**的类，取其真实字段偏移，在一块自建内存上按该偏移
    # 写标记值，再看 read_field_value 能否按同一偏移读回来。
    # 这验证了「偏移算术 + 按类型宽度读」整条链路。
    #
    # 必须挑**基础类型**字段（int/bool/float）：引用类型字段按 8 字节读，
    # 拿它断言 4 字节会假红（实测踩过 —— 一开始挑到
    # `m_CancellationTokenSource`，它是引用类型，读 8 字节是**正确**行为）。
    # 所以先用静态索引找一个真有 System.Int32 字段的非编译器生成类，
    # 而不是在运行时字段表里随便取第一个。
    _WANT_TYPES = ("System.Int32", "System.Single", "System.UInt32")
    _cands: dict[str, list[str]] = {}
    for _ch in "abcdefghijklmnopqrstuvwxyz":
        _sf = S.search_fields(pattern=_ch, limit=2000)
        for _it in (_sf.get("items") or []):
            _own = str(_it.get("owner") or "")
            # 跳过编译器生成的状态机（属性名带尖括号），它们不是稳定的样本
            if "<" in _own or "d__" in _own or ">" in _own:
                continue
            if _it.get("field_type") in _WANT_TYPES:
                _cands.setdefault(_own, []).append(_it["name"])
    print(f"  宽度验收候选类 {len(_cands)} 个（按基础类型字段数排序）")
    # 逐个试到**运行时真能解析**为止。
    #
    # 为什么不能只按静态字段数取第一名：静态索引里有这个类，不代表运行时
    # 也解析得出来 —— 实测第一名 `DiceBody` 静态有 60+ 个 Int32 字段，
    # 但运行时 `runtime_class_info` 拿不到（该类型在本轮程序集加载状态下
    # 没有实例化/未加载）。只取第一名会让这条断言**空转跳过**，
    # 而「跳过」看起来和「通过」一样绿。
    _cls = ""
    _rfc: dict = {"ok": False}
    for _cand_cls in [c for c, _f in sorted(_cands.items(),
                                            key=lambda kv: -len(kv[1]))][:12]:
        _r = S.runtime_class_info(name=_cand_cls)
        _has = [f for f in (_r.get("fields") or [])
                if f.get("offset") is not None and not f.get("is_static")
                and f.get("type") in _WANT_TYPES]
        if _r.get("ok") and _has:
            _cls, _rfc = _cand_cls, _r
            break
    print(f"  运行时可用且含基础类型实例字段的类：{_cls!r}")
    if _rfc.get("ok"):
        _flds = [f for f in (_rfc.get("fields") or [])
                 if f.get("offset") is not None and not f.get("is_static")]
        _pick = next((f for f in _flds if f.get("type") in _WANT_TYPES), None)
        print(f"  {_cls} 实例字段 {len(_flds)} 个，选中 "
              f"{_pick and (_pick['name'], _pick.get('type'), _pick['offset'])}")
        if _pick:
            _off = int(_pick["offset"])
            _marker = 0x11223344
            _inst = S.eval_script(
                "(function(){"
                "  globalThis.__mono_verify_inst = Memory.alloc(512);"
                f"  globalThis.__mono_verify_inst.add({_off})"
                f".writeU32(0x{_marker:08x});"
                "  return globalThis.__mono_verify_inst.toString();"
                "})()"
            ).get("value")
            if _inst:
                _rf = S.read_field_value(instance=_inst, name=_cls,
                                        field=_pick["name"])
                print(f"     read_field_value: ok={_rf.get('ok')} "
                      f"read_width={_rf.get('read_width')} "
                      f"raw_hex={_rf.get('raw_hex')} as_int32={_rf.get('as_int32')}")
                check("read_field_value 返回 ok", _rf.get("ok") is True,
                      str(_rf.get("error"))[:90])
                if _rf.get("ok"):
                    check("按字段类型宽度读（4 字节基础类型 → read_width=4）",
                          _rf.get("read_width") == 4,
                          f"字段类型={_pick.get('type')} "
                          f"实际 read_width={_rf.get('read_width')}")
                    check("读回的正是写在真实偏移上的标记值",
                          (_rf.get("raw_hex") or "").lower() == "44332211",
                          f"raw_hex={_rf.get('raw_hex')!r} 期望=44332211")

                # ---- set_field_value：**写**入口（read_field_value 的对称面）
                #
                # 这是动态覆盖率门禁要求的「真的调用过」证据，但它不只是为了
                # 覆盖率 —— 这里同样验行为：写完要能**独立读回**同一个值。
                # 用同一个伪造实例：初值 0x11223344，写一个新值再读回。
                _sv = S.set_field_value(instance=_inst, name=_cls,
                                        field=_pick["name"],
                                        value="0x55667788")
                print(f"     set_field_value: ok={_sv.get('ok')} "
                      f"kind={_sv.get('write_kind')} "
                      f"width={_sv.get('write_width')} "
                      f"before={_sv.get('value_before_hex')} "
                      f"after={_sv.get('read_back_hex')} "
                      f"verified={_sv.get('verified')}")
                check("set_field_value 返回 ok", _sv.get("ok") is True,
                      str(_sv.get("error"))[:110])
                if _sv.get("ok"):
                    check("写入宽度按字段类型（4 字节字段 → write_width=4）",
                          _sv.get("write_width") == 4,
                          f"实际 {_sv.get('write_width')}")
                    check("写前原值被如实记录（0x11223344）",
                          (_sv.get("value_before_hex") or "").lower() == "44332211",
                          f"value_before_hex={_sv.get('value_before_hex')!r}")
                    check("写入后自校验 verified is True",
                          _sv.get("verified") is True,
                          f"read_back_hex={_sv.get('read_back_hex')!r}")
                    # **独立**再读一次：不信工具自己的回读
                    _rf2 = S.read_field_value(instance=_inst, name=_cls,
                                              field=_pick["name"])
                    check("独立读回的字节 == 写入值（0x55667788 → 88776655）",
                          (_rf2.get("raw_hex") or "").lower() == "88776655",
                          f"raw_hex={_rf2.get('raw_hex')!r} 期望=88776655")

                # ---- 拒绝分支：超范围值不许静默截断
                _ovf = S.set_field_value(instance=_inst, name=_cls,
                                         field=_pick["name"], value=2 ** 40)
                check("写超范围值被拒绝（不静默截断）",
                      _ovf.get("ok") is False
                      and "超出" in str(_ovf.get("error", "")),
                      f"error={str(_ovf.get('error'))[:90]}")
        else:
            check("找到可用于 read_field_value 的实例字段", False,
                  f"{_cls} 没有带偏移的基础类型实例字段")
    else:
        # **硬失败**，不是警告：上面已经逐个试过若干候选类，只要有一个能在
        # 运行时解析出来就够了。全部失败说明 Mono 的类解析/字段枚举真的坏了
        # （这正是本脚本该拦的那类缺陷）。记成 warn_only 会让这条断言
        # 「看起来绿、实际没跑」—— 那是本文件反复强调要避免的空转。
        check("能找到运行时可用、且含基础类型实例字段的类", False,
              f"试过 {min(12, len(_cands))} 个候选，均无法在运行时取得"
              f"带偏移的基础类型字段")

    # ---- find_instances 在 Mono 上的**契约**（不是「必须成功」）
    #
    # Mono 没有 liveness 导出，唯一相关的是 `mono_gc_walk_heap`，而官方
    # 头文件写明堆遍历「只在 pre-stop-world 回调内有效」。所以这里**不能**
    # 断言「一定能拿到实例」——那会把一个已知的引擎限制写成产品缺陷。
    #
    # 要断言的是**契约**：两种结果都合法，但不合法的是「假成功」——
    #   · 拿到实例 → ok:true 且 count>0 且 pointers 非空
    #   · 拿不到   → ok:false 且 unsupported:true（**不能**是 count:0，
    #                那会被读成「这个类没有实例」）
    # 用 `_cls` 的**短名 + 命名空间**传参（工具的 name 收的是短名；
    # 传整串 "Ns.Class" 也能解析，但这里按规范写法来，避免把
    # 「参数写法不对」误判成「Mono 不支持」）。
    _fi = {}
    if _cls:
        _short = _cls.rsplit(".", 1)[-1]
        _ns = _cls.rsplit(".", 1)[0] if "." in _cls else ""
        _fi = S.find_instances(name=_short, namespace=_ns)
    if _fi:
        print(f"     find_instances({_cls}): ok={_fi.get('ok')} "
              f"count={_fi.get('count')} unsupported={_fi.get('unsupported')} "
              f"scanned={_fi.get('scanned_objects')}")
        if _fi.get("ok"):
            check("Mono 拿到实例时 count>0 且 pointers 非空",
                  (_fi.get("count") or 0) > 0 and bool(_fi.get("pointers")),
                  f"count={_fi.get('count')}")
        else:
            check("Mono 拿不到实例时是 ok:false + unsupported:true"
                  "（不是 count:0 冒充「没有实例」）",
                  _fi.get("unsupported") is True,
                  f"unsupported={_fi.get('unsupported')!r} "
                  f"count={_fi.get('count')!r}")
            check("拿不到时**不返回** count（不给会被误读成结果的值）",
                  _fi.get("count") is None, f"count={_fi.get('count')!r}")
        print(f"     （Mono 的这条限制是引擎侧的，见 mono-gc.h 注释；"
              f"不构成本项目缺陷）")
    else:
        check("find_instances 可调用（Mono 路径）", False,
              "没有可用类名，跳过", warn_only=True)

    # ---- 收尾（含上面 hook_address 可能留下的 hook）
    S.unhook()

    S.close_session()

    # 结束**本脚本 spawn 出来**的那个游戏进程。
    #
    # `close_session()` 只是 detach（卸载 Hook、断开连接），**不结束进程** ——
    # 不把它收掉，下一轮 `launch_game` 就会被「已有同名进程」拦下，
    # 本脚本变成「只能跑一次」。其他运行时脚本
    # （verify_mcp_tools / verify_mcp_extra）都是这么收尾的，这里对齐。
    if _spawned_pid:
        print(f"  清理：结束本脚本启动的游戏进程 pid={_spawned_pid} …")
        try:
            import subprocess as _sp
            _sp.run(["taskkill", "/F", "/T", "/PID", str(_spawned_pid)],
                    capture_output=True, timeout=30)
            print("  已结束")
        except Exception as exc:                       # noqa: BLE001
            print(f"  结束失败，请手动检查：{exc}")

# ============================================================ 4. 程序集懒加载
section("4. 「注入成功 ≠ 可以 Hook」：等 Mono 程序集加载稳定（纯函数，不开游戏）")

from gamemod_mcp.engine import adapt as _adapt          # noqa: E402
from gamemod_mcp.engine import state as _state          # noqa: E402
from gamemod_mcp.engine.index import Index as _Index    # noqa: E402

# 4.1 能力位：这是「要不要等」的唯一依据，两个引擎必须表态且相反
_cap_mono = _adapt.capabilities_of("mono")
_cap_il2 = _adapt.capabilities_of("il2cpp")
print(f"  mono.lazy_assemblies={_cap_mono.get('lazy_assemblies')} "
      f"il2cpp.lazy_assemblies={_cap_il2.get('lazy_assemblies')}")
check("mono 声明 lazy_assemblies=True", _cap_mono.get("lazy_assemblies") is True)
check("il2cpp 声明 lazy_assemblies=False", _cap_il2.get("lazy_assemblies") is False)
print(f"  mono.static_index_covers_frameworks="
      f"{_cap_mono.get('static_index_covers_frameworks')} "
      f"il2cpp.static_index_covers_frameworks="
      f"{_cap_il2.get('static_index_covers_frameworks')}")
check("mono 声明静态索引不含框架类型",
      _cap_mono.get("static_index_covers_frameworks") is False)
check("il2cpp 声明静态索引含框架类型",
      _cap_il2.get("static_index_covers_frameworks") is True)


class _StubSession:
    """假会话：按给定的数量序列报 image_count，并记下被问了几次。"""

    def __init__(self, seq: list[int], tail: int | None = None):
        self.seq = list(seq)
        self.tail = tail
        self.calls = 0

    def runtime_info(self):
        self.calls += 1
        if self.seq:
            return {"ok": True, "image_count": self.seq.pop(0)}
        if self.tail is None:
            raise RuntimeError("探针挂了")
        return {"ok": True, "image_count": self.tail}


# 4.2 数量在涨 → 必须一直等到稳定，且报出的数量是**最后**那个
_grow = _StubSession([13, 40, 90, 164], tail=164)
_res = _state._settle_assemblies(_grow, "mono")
print(f"  增长的序列 [13,40,90,164] → {_res}（问了 {_grow.calls} 次）")
check("数量增长时会等到稳定", _res.get("settled") is True, str(_res))
check("报出的数量是稳定后的那个", _res.get("images") == 164, str(_res))
check("确实等了多轮（不是一次就放行）", _grow.calls > 4, f"问了 {_grow.calls} 次")

# 4.3 非空转反证之一：数量一直涨且预算很小 → 必须**如实**说没稳定
class _EverGrowing:
    def __init__(self):
        self.n = 0

    def runtime_info(self):
        self.n += 1
        return {"ok": True, "image_count": self.n}


_eg = _EverGrowing()
_res2 = _state._settle_assemblies(_eg, "mono", budget=1.2)
print(f"  一直增长（预算 1.2s）→ settled={_res2.get('settled')} "
      f"waited={_res2.get('waited')} note={str(_res2.get('note'))[:40]}")
check("一直增长时不谎报稳定", _res2.get("settled") is False, str(_res2))
check("没稳定时给出说明", bool(_res2.get("note")), str(_res2))

# 4.4 非空转反证之二：同一个「一直增长」的假会话，引擎换成 IL2CPP →
#     必须**立刻**返回且一次都不问（证明是能力位在决定，不是碰巧）
_eg2 = _EverGrowing()
_res3 = _state._settle_assemblies(_eg2, "il2cpp", budget=1.2)
print(f"  il2cpp + 一直增长 → {_res3}（问了 {_eg2.n} 次）")
check("il2cpp 不等待（返回空）", _res3 == {}, str(_res3))
check("il2cpp 一次都没查程序集数量（非空转反证）", _eg2.n == 0, f"问了 {_eg2.n} 次")

# 4.5 探针报错不该让注入失败，只如实说明
_broken = _StubSession([], tail=None)
_res4 = _state._settle_assemblies(_broken, "mono")
print(f"  探针报错 → {str(_res4)[:100]}")
check("查询失败时如实报告而不是抛异常",
      bool(_res4.get("error")) and "settled" not in _res4, str(_res4))

# 4.6 `_with_settled_assemblies`：两条注入路径共用的那层
init_in = {"engine": "mono", "module": "mono-2.0-bdwgc.dll"}
_out = _state._with_settled_assemblies(_StubSession([13, 164], tail=164),
                                       "mono", init_in)
print(f"  _with_settled_assemblies → assemblies={_out.get('assemblies')}")
check("合并后保留了原 init 字段", _out.get("module") == "mono-2.0-bdwgc.dll")
check("合并后带上 assemblies 结论",
      (_out.get("assemblies") or {}).get("settled") is True, str(_out))
_out2 = _state._with_settled_assemblies(_StubSession([1], tail=1), "il2cpp",
                                        init_in)
check("非懒加载引擎上原样返回（不添字段）", "assemblies" not in _out2, str(_out2))

# 4.7 静态索引查不到框架类型时的提示（这句话由能力位驱动）
_ix_mono = _Index.__new__(_Index)
_ix_mono.engine = "mono"
_ix_il2 = _Index.__new__(_Index)
_ix_il2.engine = "il2cpp"
_hint_m = _ix_mono._framework_hint()
_hint_i = _ix_il2._framework_hint()
print(f"  mono 提示={_hint_m[:60]}…")
print(f"  il2cpp 提示={_hint_i!r}")
check("Mono 上会说明静态索引不含框架类型并指向 runtime_class_info",
      "只覆盖游戏自己的程序集" in _hint_m and "runtime_class_info" in _hint_m,
      _hint_m[:120])
check("IL2CPP 上不加这句（它静态查得到）", _hint_i == "", repr(_hint_i))

# ============================================================ 汇总
print()
print("=" * 78)
print("Mono 运行时探针验收汇总")
print("=" * 78)
print(f"  通过 {PASS} 项 / 失败 {FAIL} 项 / 警告 {WARN} 项")
print()
if FAIL == 0:
    print("  \u2605 Mono 运行时探针可用：注入 → 真实偏移/地址 → Hook 全部走通。")
else:
    print(f"  \u26a0 有 {FAIL} 项失败。")

sys.exit(1 if FAIL else 0)
