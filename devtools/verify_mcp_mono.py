"""通过 MCP 工具层验证 Mono 游戏 —— 证明「同一套工具也能吃 Mono」。

与 verify_mcp_tools.py（用 IL2CPP 的 Garden of Witches）互补：
本脚本用 Mono 的 Archon Soul，证明跨引擎这件事在**工具层**也成立，
而不只是在 engine 层成立。

分工（P1 起 Mono 已有运行时探针，所以这里也覆盖运行时能力）：

| 脚本 | 覆盖 |
|---|---|
| 本脚本 | 工具层在 Mono 上的**静态**链路 + 运行时能力位/接口 |
| `verify_mono_runtime.py` | 探针的 Mono 后端：导出真值、真实偏移/地址、Hook 端到端 |

注意：Mono 是 JIT 引擎 —— 方法地址必须靠 `mono_compile_method` 触发 JIT 后
才能拿到，且 **JIT 代码不在 mono 模块内，因此没有有意义的 RVA**；
`gc_control` 在本版 Mono 上如实报不支持（没有 `mono_gc_disable` 导出）。
这些都是能力位/探针返回里如实声明的，不是「没实现」。
"""

from __future__ import annotations

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
from gamemod_mcp.engine import state        # noqa: E402

# 样本路径从 samples.py 取；取不到就记为「无法验证」并跳过（跳过不算失败）
_MONO_GAME = samples.find_mono()
MONO_GAME = str(_MONO_GAME) if _MONO_GAME else None
MONO_ASM_NAME = "Assembly-CSharp.dll"

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


# ============================================================ 1. 识别 Mono 游戏
section("1. inspect_game —— 识别 Mono 引擎")

game = Path(MONO_GAME) if MONO_GAME else None
if not samples.require(MONO_GAME, "Mono 样本 Archon Soul") or not game.is_dir():
    print(f"  \u26a0\ufe0f  样本游戏不存在，跳过：{MONO_GAME}")
    sys.exit(0)

ins = S.inspect_game(str(game))
check("inspect_game 返回 ok", ins.get("ok") is True)
print(f"     引擎: {ins.get('engine')}")
print(f"     主程序: {ins.get('main_exe')}")
check("识别为 mono", ins.get("engine") == "mono",
      f"实际={ins.get('engine')}")

mono = ins.get("mono") or {}
print(f"     托管目录: {mono.get('managed_dir')}")
print(f"     程序集数: {mono.get('assembly_count')}")
print(f"     游戏程序集: {mono.get('game_assembly_name')}")
check(f"托管目录里有 {MONO_ASM_NAME}",
      (mono.get("assembly_count") or 0) > 0,
      f"程序集数={mono.get('assembly_count')}")
# inspect_game 应当直接指出「哪个 dll 是游戏代码」，省得 Agent 在几百个里猜
check("inspect_game 指出了游戏程序集",
      (mono.get("game_assembly_name") or "").lower() == MONO_ASM_NAME.lower(),
      f"实际={mono.get('game_assembly_name')!r}")


# ============================================================ 2. open_game
section("2. open_game —— 选定 Mono 游戏并建索引")

t0 = time.time()
op = S.open_game(game_dir=str(game))
dt = time.time() - t0
check("open_game 返回 ok", op.get("ok") is True,
      op.get("error", ""))
if not op.get("ok"):
    print(f"     \u274c 无法继续：{op.get('error')}")
    sys.exit(1)

print(f"     引擎: {op.get('engine')} / {op.get('engine_label')}")
print(f"     元数据: {op.get('metadata_path')}")
check("open_game 报告引擎为 mono", op.get("engine") == "mono",
      f"实际={op.get('engine')}")
check("自动挑中了 Assembly-CSharp.dll",
      Path(op.get("metadata_path", "")).name.lower() == MONO_ASM_NAME.lower(),
      f"实际={Path(op.get('metadata_path','')).name}")
check("open_game 带 engine_label", bool(op.get("engine_label")))
print(f"     耗时 {dt:.2f}s")

idx = op.get("index") or {}
tc = idx.get("type_count")
print(f"     类型={tc} 方法={idx.get('method_count')} "
      f"字段={idx.get('field_count')} 枚举={idx.get('enum_count')}")
check("类型数是合理的整数（非 0、非字符串）",
      isinstance(tc, int) and tc > 0, f"{tc!r}")

cap = idx.get("capabilities") or {}
check("索引带能力声明", bool(cap), f"{cap.get('label')}")
check("能力声明的引擎为 mono", cap.get("engine") == "mono",
      f"实际={cap.get('engine')}")

# 能力位必须承认「偏移/地址都不在元数据里」——这是 AGENTS.md §1 的核心断言。
# Mono 尤其容易说反：程序集里**有**方法的 RVA 字段，但它指向 IL 字节码、
# 不是机器码，所以对 Hook 毫无用处。
print(f"     method_address_in_metadata={cap.get('method_address_in_metadata')} "
      f"field_offset_in_metadata={cap.get('field_offset_in_metadata')}")
check("mono 能力位：方法地址不在元数据里",
      cap.get("method_address_in_metadata") is False,
      f"实际={cap.get('method_address_in_metadata')}")
check("mono 能力位：字段偏移不在元数据里",
      cap.get("field_offset_in_metadata") is False,
      f"实际={cap.get('field_offset_in_metadata')}")
_notes = " ".join(cap.get("notes") or [])
check("能力说明点出了「元数据 RVA 指向 IL 字节码」",
      "IL" in _notes and "字节码" in _notes, _notes[:110])
check("能力说明点出了「JIT 代码不在 mono 模块内，拿不到有意义的 RVA」",
      "JIT" in _notes and "RVA" in _notes, _notes[:160])

# 注入前不许谎称有运行时
check("未注入时 runtime_ready 为假", op.get("runtime_ready") in (False, None),
      f"实际={op.get('runtime_ready')}")

# 缓存复用
t1 = time.time()
op2 = S.open_game(game_dir=str(game))
dt2 = time.time() - t1
check("重复 open_game 复用会话", op2.get("reused") is True)
check("二次调用很快（未重建）", dt2 < 1.0, f"{dt2:.2f}s")


# ============================================================ 3. 静态查询
section("3. 静态查询 —— 同一套工具查 Mono 程序集")

sc = S.search_classes(pattern="Player", limit=20)
check("search_classes 返回 ok", sc.get("ok") is True)
print(f"     命中 {sc.get('total')} 个")
check("搜到含 Player 的类", (sc.get("total") or 0) > 0,
      f"total={sc.get('total')}")
if sc.get("items"):
    print(f"     首条: {sc['items'][0]['full_name']}")

# 基类过滤（Mono 要跨程序集解析基类，是最容易出问题的一环）
mb = S.search_classes(subtype_of="UnityEngine.MonoBehaviour", limit=10)
check("按 MonoBehaviour 过滤可用", (mb.get("total") or 0) > 0,
      f"命中 {mb.get('total')}")

en = S.search_classes(pattern="", only_enums=True, limit=10)
check("枚举过滤可用", (en.get("total") or 0) > 0, f"命中 {en.get('total')}")

# 字段搜索（Mono 走签名 blob 解析，与 IL2CPP 完全不同）
sf = S.search_fields(pattern="health", limit=20)
check("search_fields 返回 ok", sf.get("ok") is True)
print(f"     字段命中 {sf.get('total')} 个")
if sf.get("items"):
    it = sf["items"][0]
    print(f"     {it['name']} @ {it['owner']} : {it['field_type']} "
          f"({it.get('type_confidence')})")
    check("字段类型可从签名精确解出", bool(it.get("field_type")),
          f"{it.get('field_type')!r}")
    # Mono 的签名是精确的，不说 derived
    check("Mono 字段类型标记为 exact（不是 derived）",
          it.get("type_confidence") == "exact",
          f"实际={it.get('type_confidence')}")

sm = S.search_methods(pattern="Update", limit=10)
check("search_methods 返回 ok", sm.get("ok") is True)
check("搜到 Update 方法", (sm.get("total") or 0) > 0,
      f"命中 {sm.get('total')}")

# list_images：**工具层**断言。
#
# 为什么补在这里：它是唯一「只有会话对象调用、没有工具层调用」的工具
# （`verify_il2cpp_runtime.py` 走的是 `sess.list_images()`），
# 于是动态覆盖率门禁里它长期是空的 —— AGENTS.md §5.1 记的就是这条。
# Mono 会话同样能列程序集，且**不需要注入**，所以放在这里是稳定的。
li = S.list_images()
check("list_images 返回 ok", li.get("ok") is True, li.get("error", ""))
print(f"     程序集 {li.get('total')} 个")
check("列出了程序集（Mono 游戏通常有多个）", (li.get("total") or 0) > 0,
      f"total={li.get('total')}")
_li_items = li.get("items") or []
if _li_items:
    print(f"     首条: {_li_items[0].get('image')} "
          f"类型数={_li_items[0].get('type_count')}")
    check("每条带程序集名与类型数",
          bool(_li_items[0].get("image"))
          and isinstance(_li_items[0].get("type_count"), int),
          str(_li_items[0])[:80])
    # 分页必须真的生效（不许「收下参数但不干活」—— AGENTS.md §3.1）
    _li_p1 = S.list_images(limit=3, offset=0)
    _li_p2 = S.list_images(limit=3, offset=3)
    if (_li_p1.get("total") or 0) > 3:
        _n1 = [i.get("image") for i in (_li_p1.get("items") or [])]
        _n2 = [i.get("image") for i in (_li_p2.get("items") or [])]
        check("offset 真的改变了返回内容（不是死参数）",
              bool(_n1) and bool(_n2) and _n1 != _n2,
              f"p1={_n1[:2]} p2={_n2[:2]}")
        check("limit 真的生效", len(_n1) <= 3, f"{len(_n1)} 条")
    # 过滤也必须是活的
    _li_f = S.list_images(pattern="Assembly-CSharp")
    check("pattern 过滤生效（命中项都含该子串）",
          _li_f.get("ok") is True
          and all("Assembly-CSharp" in str(i.get("image"))
                  for i in (_li_f.get("items") or [])),
          f"命中 {_li_f.get('total')}")


# ============================================================ 4. 类详情
section("4. class_detail —— 字段/方法/继承链")

target = None
if sc.get("items"):
    target = sc["items"][0]["full_name"]
if target:
    cd = S.class_detail(target)
    check("class_detail 返回 ok", cd.get("ok") is True,
          cd.get("error", ""))
    if cd.get("ok"):
        nf = len(cd.get("fields") or [])
        nm = len(cd.get("methods") or [])
        print(f"     {target}: 字段 {nf} 方法 {nm} 父类={cd.get('parent')!r}")
        check("class_detail 有内容", nf + nm > 0, f"{nf}/{nm}")
        check("父类名可解析", bool(cd.get("parent")),
              f"parent={cd.get('parent')!r}")
        if nf:
            named = sum(1 for f in cd["fields"] if f.get("type"))
            rate = named / nf
            check("字段类型解析率高（Mono 应接近 100%）", rate >= 0.95,
                  f"{named}/{nf} = {rate:.2%}")
        # 同一个类的完整继承链（跨程序集基类）
        ch = S.class_hierarchy(target)
        check("class_hierarchy 返回 ok", ch.get("ok") is True)
        print(f"     上溯: {ch.get('up')}")
        check("继承链非空", bool(ch.get("up")) or bool(cd.get("parent")))

# 子类查找
if target:
    fd = S.find_derived_classes("UnityEngine.MonoBehaviour", limit=10)
    check("find_derived_classes 可用", fd.get("ok") is True,
          fd.get("error", ""))
    check("MonoBehaviour 有子类", (fd.get("total") or 0) > 0,
          f"{fd.get('total')}")

# 依赖分析（走 resolve_param_types，是 Mono 的专有路径）
if target:
    ad = S.analyze_dependency(target)
    check("analyze_dependency 可用", ad.get("ok") is True, ad.get("error", ""))
    check("依赖分析有结果", (ad.get("referenced_count") or 0) > 0,
          f"{ad.get('referenced_count')}")


# ============================================================ 5. 结果句柄
section("5. 结果句柄 —— 大结果集翻页")

if sc.get("handle"):
    pg = S.read_query_result(sc["handle"], offset=0, limit=5)
    check("read_query_result 可用", pg.get("ok") is True, pg.get("error", ""))
    check("翻页取回数据", len(pg.get("items") or []) > 0,
          f"{len(pg.get('items') or [])} 条")
    sq = S.read_query_result(sc["handle"], pattern="a")
    check("read_query_result 的结果内过滤可用", sq.get("ok") is True,
          sq.get("error", ""))
    print(f"     结果内过滤命中 {sq.get('total')}")

bad = S.read_query_result("不存在的句柄-abc")
check("无效句柄被正确拒绝", bad.get("ok") is False, bad.get("error", ""))


# ============================================================ 6. 错误路径
section("6. 错误路径 —— 不能假装成功")

bad1 = S.class_detail("这个类根本不存在XyzAbc")
check("不存在的类被拒绝", bad1.get("ok") is False, bad1.get("error", ""))

bad2 = S.search_fields(pattern="zzz_绝对不存在的字段名_zzz")
check("搜不到字段时返回 0 而非报错（空结果是合法结果）",
      bad2.get("ok") is True and (bad2.get("total") or 0) == 0,
      f"total={bad2.get('total')}")

# 用 IL2CPP 解析器读 Mono 文件必须报错（不能静默给错数据）
bad3 = S.open_game(metadata_path=op.get("metadata_path", ""), engine="il2cpp")
check("显式错配引擎时报错（不静默降级）",
      bad3.get("ok") is False, str(bad3.get("error", ""))[:100])
if bad3.get("ok") is False:
    print(f"     错误信息: {bad3.get('error')}")

# 显式指定正确引擎时应当成功
ok3 = S.open_game(metadata_path=op.get("metadata_path", ""), engine="mono")
check("显式指定正确引擎时成功", ok3.get("ok") is True,
      str(ok3.get("error", ""))[:100])


# ============================================================ 收尾
section("7. 收尾")

S.close_session()
sess = S.get_session()
check("close_session 后索引已关闭",
      sess.get("index_ready") is False,
      f"index_ready={sess.get('index_ready')}")

print()
print("=" * 78)
print("Mono 游戏的 MCP 工具层验证汇总")
print("=" * 78)
print(f"  通过 {PASS} 项 / 失败 {FAIL} 项 / 警告 {WARN} 项")
print()
if FAIL == 0:
    print("  \u2605 同一套 MCP 工具在 Mono 游戏上同样走通（跨引擎成立）。")
else:
    print(f"  \u26a0 有 {FAIL} 项失败。")

sys.exit(1 if FAIL else 0)
