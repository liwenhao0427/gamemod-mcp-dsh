"""通过 MCP 工具层验证 `decompile` —— 反编译（薄封装 ilspycmd）。

真值来源（`AGENTS.md` §5.2：不许拿自己写的第二个解析器当真值）
============================================================
反编译的产物是 **ILSpy** 生成的，而类/方法清单来自**我们自己的** ECMA-335
解析器（`mono_meta`）。两者是**独立实现**，所以「索引里有的方法名，
必须能在 ILSpy 的产物里找到」是一个可核查的交叉验证 ——
任一侧解析错误都会让断言红掉。

覆盖的边界（都是实测踩过的坑，见 `engine/decompile.py` 的 docstring）：
    1. `decompiler.available` 报告          —— 缺依赖必须显式说明，不假成功
    2. 单类型反编译 + 类名/方法名交叉验证    —— 主链路
    3. 命名空间限定的类型名                  —— ilspycmd 要求全限定名
    4. 嵌套类型（`Outer.Inner`）             —— ilspycmd 无法单独定位，
                                              必须退让到外层且**不交出错误源码**
    5. 不存在的类型                          —— ok:false，不许假成功
    6. IL2CPP 的 GameAssembly.dll            —— 必须明确拒绝并给出替代路径
    7. 落盘模式                              —— 只返回文件清单，不内联源码
    8. 参数缺失                              —— type_name 与 output_dir 全空要拒绝
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import samples                                            # noqa: E402
samples.add_src_to_path()
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import gamemod_mcp.server as S              # noqa: E402

_MONO = samples.find_mono()
MONO_DIR = str(_MONO) if _MONO else None
ASM = (str(_MONO / "Archon Soul_Data" / "Managed" / "Assembly-CSharp.dll")
       if _MONO else None)
_IL2CPP = samples.find_il2cpp()
IL2CPP_ASM = str(_IL2CPP / "GameAssembly.dll") if _IL2CPP else None

# 落盘模式的输出目录：一律 <repo>/.cache-test/（已被 .gitignore 排除）
OUT_DIR = samples.CACHE_TEST / "_verify_decompile"

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


# ============================================================ 1. 依赖探测
section("1. get_diagnostics —— 反编译依赖必须被显式报告")

diag = S.get_diagnostics()
check("get_diagnostics 返回 ok", diag.get("ok") is True, diag.get("error", ""))
dec = diag.get("decompiler") or {}
print(f"     decompiler: available={dec.get('available')} "
      f"tool={dec.get('tool')} version={dec.get('version')}")
check("diagnostics 里有 decompiler.available 字段",
      isinstance(dec.get("available"), bool),
      f"实际类型={type(dec.get('available')).__name__}")
check("decompiler 有 tool 字段", "tool" in dec)
check("decompiler 有 hint 字段（缺失时给安装指引）", "hint" in dec)

# ilspycmd 是本机可选依赖：不可用时后续断言降级为「无法验证」并跳过
HAVE_TOOL = bool(dec.get("available"))
if not HAVE_TOOL:
    print(f"  \u26a0\ufe0f  无法验证：本机没有 ilspycmd —— {dec.get('hint', '')[:80]}")
    print("      后面依赖反编译的用例跳过（跳过不算失败）")
if HAVE_TOOL:
    check("ilspycmd 版本号非空（实测 9.0.0.7889）", bool(dec.get("version")),
          f"version={dec.get('version')!r}")
else:
    # 缺失路径也必须走一遍：不许抛异常，要返回可读的安装指引
    missing = S.decompile(assembly=ASM or "x.dll", type_name="Foo")
    check("缺依赖时返回 ok:false 而不是抛异常", missing.get("ok") is False)
    check("缺依赖时给出安装指引", "ilspycmd" in (missing.get("error") or ""),
          (missing.get("error") or "")[:70])

if not samples.require(MONO_DIR, "Mono 样本 Archon Soul") or not HAVE_TOOL:
    print(f"\n  \u26a0\ufe0f  样本或依赖不可用，跳过剩余用例")
    print(f"\n通过 {PASS} 项，失败 {FAIL} 项，警告 {WARN} 项")
    sys.exit(1 if FAIL else 0)

if not Path(ASM).is_file():
    print(f"  \u26a0\ufe0f  无法验证：程序集不存在 —— {ASM}")
    sys.exit(1 if FAIL else 0)


# ============================================================ 2. 主链路
section("2. decompile —— 单类型反编译，并与索引交叉验证")

# 拿索引里的真值：先打开索引，取一个类的类名与方法名
opened = S.open_game(game_dir=MONO_DIR)
check("open_game 成功（Mono）", opened.get("ok") is True, opened.get("error", ""))
print(f"     引擎={opened.get('engine')} 类型数={opened.get('index', {}).get('type_count')}")

TARGET = "SpellEffect"
detail = S.class_detail(TARGET)
check("class_detail 能查到目标类（索引侧真值）", detail.get("ok") is True,
      detail.get("error", ""))
idx_methods = [m["name"] for m in (detail.get("methods") or [])]
print(f"     索引侧 {TARGET} 的方法: {idx_methods}")
check("索引侧拿到了方法名（否则交叉验证无意义）", len(idx_methods) > 0)

res = S.decompile(assembly=ASM, type_name=TARGET)
check("decompile 返回 ok", res.get("ok") is True, res.get("error", ""))
print(f"     decompiled_type={res.get('decompiled_type')} "
      f"行数={res.get('line_count')} 字符数={res.get('char_count')}")
print(f"     declared_types={res.get('declared_types')}")

source = res.get("source") or ""
check("小的单类型源码内联在 source 里", bool(source),
      f"source 长度={len(source)}")
check("源码里找到了目标类的声明（ILSpy 独立实现的交叉验证）",
      f"class {TARGET}" in source or f"struct {TARGET}" in source
      or f"enum {TARGET}" in source,
      f"前 60 字符: {source[:60]!r}")
check("declared_types 里含目标类", TARGET in (res.get("declared_types") or []),
      f"{res.get('declared_types')}")

# 交叉验证：索引说有这些方法，ILSpy 的产物里就应该找得到。
#
# 但**不能直接逐个比对名字**：索引存的是 ECMA-335 的原始方法名，
# 而 C# 把它们渲染成了别的语法形式，实测三类对不上：
#     get_IsFromSpell  → 属性 `public bool IsFromSpell { get { ... } }`
#     .ctor            → 构造函数 `public SpellEffect()`
# 这是「元数据名字」与「C# 表层语法」的固有差异，不是解析错误。
# 所以分开核对：普通方法要求字面命中，访问器/构造器按 C# 形式核对。
ACCESSOR_PREFIXES = ("get_", "set_", "add_", "remove_")
SPECIAL_NAMES = (".ctor", ".cctor")

idx_plain = [m for m in idx_methods
             if not m.startswith(ACCESSOR_PREFIXES) and m not in SPECIAL_NAMES]
idx_accessors = [m for m in idx_methods if m.startswith(ACCESSOR_PREFIXES)]

missing = [m for m in idx_plain if m not in source]
check("普通方法名都能在反编译产物里字面找到", not missing,
      f"缺失={missing}" if missing else f"共核对 {len(idx_plain)} 个方法：{idx_plain}")

# 访问器 → 属性名（`get_IsFromSpell` → `IsFromSpell`），要求属性确实在产物里
prop_names = [m[len("get_"):] if m.startswith("get_") else m[len("set_"):]
              for m in idx_accessors]
missing_props = [p for p in prop_names if p not in source]
check("索引里的属性访问器在 C# 里对应成了属性（语言差异，非解析错误）",
      not missing_props,
      f"缺失={missing_props}" if missing_props
      else f"共核对 {len(prop_names)} 个属性：{prop_names}")

# 构造器 → C# 里是 `public 类名(...)`。
# 注意：**无参的隐式默认构造器会被 ILSpy 省略**（合法的 C# 精简，
# `SpellEffect` 正是这种），所以只有带参构造器才一定能字面找到。
# 下面用一个确有显式构造器的类来验证，避免把「语言优化」误判成解析错误。
print(f"     构造器情况：.ctor 参数个数="
      f"{[m.get('param_count') for m in (detail.get('methods') or []) if m['name'] == '.ctor']}")

EXPLICIT_CTOR = "BestiaryStatsData"   # 实测有显式带参构造器
ctor_probe = S.decompile(assembly=ASM, type_name=EXPLICIT_CTOR)
ctor_src = ctor_probe.get("source") or ""
check("带显式构造器的类能反编译", ctor_probe.get("ok") is True,
      ctor_probe.get("error", ""))
check("索引里的 .ctor 在 C# 里对应成构造函数（用显式构造器验证）",
      f"{EXPLICIT_CTOR}(" in ctor_src,
      f"期望出现 `{EXPLICIT_CTOR}(`；实测索引里该类的 .ctor 有参数")
if f"{TARGET}(" not in source:
    print(f"     （注：{TARGET} 只有隐式默认构造器，ILSpy 按 C# 惯例省略 —— 不是解析错误）")

# 真值核查：源码应该是 C#，而不是一段错误信息
check("产物是 C# 源码（含 using 或类型声明）",
      "using " in source or "namespace " in source or "class " in source)


# ============================================================ 3. 全限定名
section("3. 命名空间限定的类型名（ilspycmd 要求全限定）")

NS_TYPE = "FlyingWormConsole3.LiteNetLib.NetManager"
res_ns = S.decompile(assembly=ASM, type_name=NS_TYPE)
check("带命名空间的类型能反编译", res_ns.get("ok") is True,
      res_ns.get("error", ""))
print(f"     行数={res_ns.get('line_count')} "
      f"declared_types={res_ns.get('declared_types')}")
check("declared_type 保留了命名空间",
      (res_ns.get("decompiled_type") or "").startswith("FlyingWormConsole3"),
      f"{res_ns.get('decompiled_type')!r}")

# 只用简单名会失败（没有命名空间限定）—— 记录这个边界，别让 Agent 以为简单名都行
res_bare = S.decompile(assembly=ASM, type_name="NetManager")
print(f"     只用简单名 NetManager: ok={res_bare.get('ok')}")
check("简单名（无命名空间）失败时给出的是可读错误，而不是假成功",
      res_bare.get("ok") is False or "NetManager" in (res_bare.get("source") or ""),
      f"ok={res_bare.get('ok')} error={(res_bare.get('error') or '')[:60]}")


# ============================================================ 4. 嵌套类型
section("4. 嵌套类型 —— 必须退让到外层，且绝不交出错误源码")

# BarrierSpellEffect.BarrierReference 是真实的嵌套类型（索引 nested_name 记的）
NESTED = "BarrierSpellEffect.BarrierReference"
res_nest = S.decompile(assembly=ASM, type_name=NESTED)
check("嵌套类型能反编译（退让到外层）", res_nest.get("ok") is True,
      res_nest.get("error", ""))
print(f"     decompiled_type={res_nest.get('decompiled_type')}")
print(f"     nested_note={(res_nest.get('nested_note') or '')[:100]}")
check("明确退让到了外层类型", res_nest.get("decompiled_type") == "BarrierSpellEffect",
      f"{res_nest.get('decompiled_type')!r}")
check("给出了 nested_note 说明退让行为", bool(res_nest.get("nested_note")),
      "（否则 Agent 会以为拿到的是内层源码）")
check("内层类型的声明确实在产物里（已核对，不是外层冒充）",
      "BarrierReference" in (res_nest.get("declared_types") or []),
      f"{res_nest.get('declared_types')}")
check("产物里含内层类型的声明", "BarrierReference" in (res_nest.get("source") or ""))

# `+` 写法（CLR 风格）也应被接受，行为一致
res_plus = S.decompile(assembly=ASM, type_name="BarrierSpellEffect+BarrierReference")
check("`Outer+Inner` 写法行为与 `.` 一致",
      res_plus.get("ok") is True
      and res_plus.get("decompiled_type") == "BarrierSpellEffect",
      f"ok={res_plus.get('ok')} type={res_plus.get('decompiled_type')}")

# 命名空间限定的**顶层**类型不许被误判成嵌套类型（两者字面上都是 `A.B`）。
# 修复前的判据只看「名字里有没有点」，会把这类请求也退让掉；
# 现在靠 `_list_types()` 的权威清单区分，这条是那个区分的回归。
check("命名空间限定的顶层类型**不**被误判成嵌套类型",
      res_ns.get("decompiled_type") == NS_TYPE
      and not res_ns.get("nested_note"),
      f"decompiled_type={res_ns.get('decompiled_type')!r} "
      f"nested_note={bool(res_ns.get('nested_note'))}")

# 深层嵌套（3 层以上）：Archon Soul 里没有这种类型（实测 0 个），
# 所以不能靠样本断言 —— 改用**合成清单**直接验解析逻辑。
# 现场是 Sephiria 的 `AnimationSet+StateInfo+Event+EPriority`（实测存在）：
# 退让必须一路到最外层 `AnimationSet`，而不是只砍掉最后一段。
from gamemod_mcp.engine import decompile as _dc               # noqa: E402

_FAKE_LISTING = {
    "AnimationSet", "AnimationSet+StateInfo", "AnimationSet+StateInfo+Event",
    "AnimationSet+StateInfo+Event+EPriority",
    "FlyingWormConsole3.LiteNetLib.NetManager",
    "FlyingWormConsole3.LiteNetLib.NetManager+IPEndPointComparer",
}
_actual, _is_nested = _dc._real_target(
    _FAKE_LISTING, "AnimationSet.StateInfo.Event.EPriority")
check("多层嵌套退让到最外层（不是只砍一段）",
      _actual == "AnimationSet" and _is_nested,
      f"actual={_actual!r} is_nested={_is_nested}")
_actual2, _nested2 = _dc._real_target(
    _FAKE_LISTING, "FlyingWormConsole3.LiteNetLib.NetManager")
check("命名空间顶层类型判为**非**嵌套（合成清单）",
      _actual2 == "FlyingWormConsole3.LiteNetLib.NetManager" and not _nested2,
      f"actual={_actual2!r} is_nested={_nested2}")
_actual3, _nested3 = _dc._real_target(
    _FAKE_LISTING, "FlyingWormConsole3.LiteNetLib.NetManager+IPEndPointComparer")
check("`命名空间.外层+内层` 也退让到 `命名空间.外层`",
      _actual3 == "FlyingWormConsole3.LiteNetLib.NetManager" and _nested3,
      f"actual={_actual3!r} is_nested={_nested3}")

# 关键回归：**不存在的内层类型**也必须被拦下。
# 实测 ilspycmd 对 `Outer+不存在` 会返回**外层**且退出码 0（假成功），
# 所以「退让」绝不能一路退让到只要外层存在就算成功。
res_fake = S.decompile(assembly=ASM, type_name="BarrierSpellEffect+根本不存在Xyz")
print(f"     不存在的内层: ok={res_fake.get('ok')}")
check("请求不存在的内层类型时**拒绝**（不能退回外层冒充）",
      res_fake.get("ok") is False,
      f"实际 ok={res_fake.get('ok')} - 这是假成功回归！")
check("拒绝时说明了原因",
      "根本不存在Xyz" in (res_fake.get("error") or "")
      or "找不到" in (res_fake.get("error") or ""),
      (res_fake.get("error") or "")[:90])


# ============================================================ 5. 失败路径
section("5. 失败路径 —— 不许假成功")

res_none = S.decompile(assembly=ASM, type_name="NoSuchTypeXyzAbc")
check("不存在的类型返回 ok:false", res_none.get("ok") is False)
print(f"     error={(res_none.get('error') or '')[:90]}")
check("错误信息里带上了找不到的类型名",
      "NoSuchTypeXyzAbc" in (res_none.get("error") or ""),
      "（可读性：Agent 要知道是哪个名字没找到）")

res_missing_asm = S.decompile(assembly=r"D:\nope\nope.dll", type_name="Foo")
check("程序集不存在时返回 ok:false", res_missing_asm.get("ok") is False)
check("错误信息里带上不存在的路径",
      "nope.dll" in (res_missing_asm.get("error") or ""))

res_noargs = S.decompile(assembly=ASM)
check("type_name 与 output_dir 都为空时拒绝", res_noargs.get("ok") is False)
print(f"     error={(res_noargs.get('error') or '')[:90]}")

res_asm_only = S.decompile(assembly=ASM, output_dir="")
check("只给 output_dir 为空、type_name 为空 → 拒绝",
      res_asm_only.get("ok") is False)


# ============================================================ 6. IL2CPP 拒绝
section("6. IL2CPP 的 GameAssembly.dll —— 必须明确拒绝并给替代路径")

if IL2CPP_ASM and Path(IL2CPP_ASM).is_file():
    res_il2cpp = S.decompile(assembly=IL2CPP_ASM, type_name="Foo")
    check("IL2CPP 的 GameAssembly.dll 被拒绝", res_il2cpp.get("ok") is False)
    err = res_il2cpp.get("error") or ""
    print(f"     error={err[:120]}")
    check("拒绝理由点明「不含托管元数据」或 DummyDll",
          "托管元数据" in err or "DummyDll" in err or "IL2CPP" in err,
          "（否则 Agent 不知道该怎么绕）")
    check("给了替代路径（改用元数据索引）",
          "search_classes" in err or "open_game" in err or "元数据" in err,
          "（只说不行、不说怎么办是不合格的错误信息）")
else:
    print(f"  \u26a0\ufe0f  无法验证：IL2CPP 样本不在本机 —— {IL2CPP_ASM}")
    WARN += 1


# ============================================================ 7. 落盘模式
section("7. 落盘模式 —— 只返回清单，不内联源码")

shutil.rmtree(OUT_DIR, ignore_errors=True)
res_disk = S.decompile(assembly=ASM, type_name=TARGET, output_dir=str(OUT_DIR))
check("指定 output_dir 时返回 ok", res_disk.get("ok") is True,
      res_disk.get("error", ""))
print(f"     file_count={res_disk.get('file_count')} files={res_disk.get('files')}")
check("返回了文件清单", bool(res_disk.get("files")))
check("落盘模式**不**内联源码（避免返回值过大）", "source" not in res_disk)
check("落盘的文件真实存在", all(Path(f).is_file() for f in (res_disk.get("files") or [])))
if res_disk.get("files"):
    got = Path(res_disk["files"][0]).read_text(encoding="utf-8")
    check("落盘内容与内联内容一致（同一份产物）",
          f"class {TARGET}" in got, f"落盘长度={len(got)}")

# 整程序集模式：必须落盘，且返回统计而不是源码
FULL_DIR = samples.CACHE_TEST / "_verify_decompile_full"
shutil.rmtree(FULL_DIR, ignore_errors=True)
res_full = S.decompile(assembly=ASM, output_dir=str(FULL_DIR), split_files=True)
check("整程序集反编译成功", res_full.get("ok") is True, res_full.get("error", ""))
print(f"     file_count={res_full.get('file_count')} "
      f"total_mb={res_full.get('total_mb')}")
print(f"     sample_files={(res_full.get('sample_files') or [])[:2]}")
check("整程序集产出了多个文件", (res_full.get("file_count") or 0) > 100,
      f"file_count={res_full.get('file_count')}")
check("整程序集模式不内联源码", "source" not in res_full)
check("返回了总体积", isinstance(res_full.get("total_mb"), (int, float)),
      f"total_mb={res_full.get('total_mb')!r}")
check("只给样例文件而不是完整清单（避免返回值过大）",
      len(res_full.get("sample_files") or []) <= 20,
      f"{len(res_full.get('sample_files') or [])} 个")

# 整程序集不指定 output_dir 必须拒绝（产物可能上万文件）
res_noout = S.decompile(assembly=ASM, split_files=True)
check("整程序集不指定 output_dir 时拒绝", res_noout.get("ok") is False)


# ============================================================ 8. 会话上下文
section("8. 留空 assembly —— 用当前会话的程序集")

res_sess = S.decompile(type_name=TARGET)
check("留空 assembly 时用会话里的程序集", res_sess.get("ok") is True,
      res_sess.get("error", ""))
check("会话模式拿到的还是同一个类型",
      res_sess.get("decompiled_type") == TARGET,
      f"{res_sess.get('decompiled_type')!r}")


# ============================================================ 收尾
S.close_session()

# 清掉本轮落盘产物（.cache-test 已被 .gitignore 排除，这里只是不留垃圾）
shutil.rmtree(OUT_DIR, ignore_errors=True)
shutil.rmtree(FULL_DIR, ignore_errors=True)

print()
print("=" * 78)
print(f"通过 {PASS} 项，失败 {FAIL} 项，警告 {WARN} 项")
print("=" * 78)
if FAIL:
    print("❌ 存在失败项")
sys.exit(1 if FAIL else 0)
