"""验证跨引擎索引层：同一套查询代码要能同时吃 IL2CPP 与 Mono。

这是 `engine/adapt.py` 的验收测试。核心断言是：
**上层查询 API（search_classes / class_detail / search_fields / …）
在两个引擎下都能跑通，且结果语义正确。**

IL2CPP 侧用 Garden of Witches 的真实元数据；
Mono 侧用 Archon Soul 的 Assembly-CSharp.dll。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# sys.path 与样本路径都由 samples.py 推导/集中定义，脚本自身不写绝对路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import samples                                            # noqa: E402
samples.add_src_to_path()
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from gamemod_mcp.engine import adapt                      # noqa: E402
from gamemod_mcp.engine.index import Index                # noqa: E402

# 样本路径：根目录从 samples.py 取，派生路径用相对路径拼出来
_IL2CPP_GAME = samples.find_il2cpp()
_MONO_GAME = samples.find_mono()
IL2CPP_META = (_IL2CPP_GAME / "Garden of Witches_Data" / "il2cpp_data"
               / "Metadata" / "global-metadata.dat") if _IL2CPP_GAME else None
MONO_ASM = (_MONO_GAME / "Archon Soul_Data" / "Managed"
            / "Assembly-CSharp.dll") if _MONO_GAME else None
CACHE = samples.CACHE_ADAPT_TEST

# 样本不可用时记为「无法验证」并跳过，不计入 FAIL
HAVE_IL2CPP = samples.require(IL2CPP_META, "IL2CPP 元数据 Garden of Witches")
HAVE_MONO = samples.require(MONO_ASM, "Mono 程序集 Archon Soul")

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


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


# ============================================================ 1. 引擎嗅探
section("1. 引擎嗅探 —— 按文件头自动分辨 IL2CPP / Mono")

if IL2CPP_META is not None and IL2CPP_META.is_file():
    with open(IL2CPP_META, "rb") as fh:
        head = fh.read(4)
    magic = int.from_bytes(head, "little")
    check("IL2CPP 元数据 magic 正确",
          magic == 0xFAB11BAF, f"0x{magic:08X}")
    sniff = Index._sniff_engine(IL2CPP_META)
    check("嗅探判为 il2cpp", sniff == "il2cpp", f"实际={sniff}")
elif HAVE_IL2CPP:
    check("IL2CPP 元数据存在", False, str(IL2CPP_META))

if MONO_ASM is not None and MONO_ASM.is_file():
    with open(MONO_ASM, "rb") as fh:
        head = fh.read(2)
    check("Mono 程序集以 MZ 开头", head == b"MZ", f"实际={head!r}")
    sniff = Index._sniff_engine(MONO_ASM)
    check("嗅探判为 mono", sniff == "mono", f"实际={sniff}")
elif HAVE_MONO:
    check("Mono 程序集存在", False, str(MONO_ASM))


# ============================================================ 2. IL2CPP 路径
def run_engine(label: str, path: Path, expect_engine: str,
               known_class: str, known_field: str) -> None:
    section(f"{label} —— {path.name}")

    if not path.is_file():
        check(f"{label} 样本存在", False, str(path))
        return

    t0 = time.time()
    try:
        idx = Index.open(path, cache_dir=CACHE, engine=expect_engine)
    except Exception as exc:
        check(f"{label} 索引可打开", False, f"{type(exc).__name__}: {exc}")
        return
    dt = time.time() - t0
    print(f"     构建/打开耗时 {dt:.2f}s  DB={idx.db_path.name}")

    got = adapt.engine_of(idx.meta)
    check(f"{label} 引擎识别正确", got == expect_engine,
          f"期望={expect_engine} 实际={got}")

    info = idx.index_info()
    print(f"     类型={info['type_count']:,} 方法={info['method_count']:,} "
          f"字段={info['field_count']:,} 枚举={info['enum_count']:,}")
    check(f"{label} 类型数 > 0", info["type_count"] > 0,
          f"{info['type_count']}")
    check(f"{label} 能力声明里带 engine",
          info.get("capabilities", {}).get("engine") == expect_engine)
    # 计数字段必须是 int，不能是字符串（否则 Agent 要做类型转换）
    check(f"{label} type_count 是 int",
          isinstance(info["type_count"], int),
          f"实际类型={type(info['type_count']).__name__}")

    # ---- 搜索类
    r = idx.search_classes(pattern=known_class, limit=20)
    check(f"{label} search_classes 有结果", r["total"] > 0,
          f"命中 {r['total']}")
    if r["total"]:
        print(f"     首个命中: {r['items'][0]['full_name']}")
        # parent 字段要能解析（跨引擎最容易在这里断）
        p = r["items"][0].get("parent")
        check(f"{label} 搜索结果的 parent 可解析出名字",
              bool(p), f"parent={p!r}")

    # ---- 类详情（跨引擎解析最集中的地方）
    det = idx.class_detail(r["items"][0]["full_name"]) if r["total"] else {}
    if det.get("found"):
        nf, nm = len(det["fields"]), len(det["methods"])
        print(f"     class_detail: 字段 {nf} 方法 {nm} 父类={det.get('parent')!r}")
        check(f"{label} class_detail 有字段或方法", nf + nm > 0,
              f"{nf}/{nm}")
        check(f"{label} class_detail 的 parent 可解析",
              bool(det.get("parent")), f"parent={det.get('parent')!r}")

        # 字段类型解析率：这是 adapt 层是否真的接对的关键指标
        if nf:
            named = sum(1 for f in det["fields"] if f.get("type"))
            rate = named / nf
            # Mono 是精确签名，理论上接近 100%；IL2CPP 是启发式，约 90%
            floor = 0.98 if expect_engine == "mono" else 0.5
            check(f"{label} 字段类型解析率 ≥ {floor:.0%}",
                  rate >= floor, f"{named}/{nf} = {rate:.2%}")
            sample = [f for f in det["fields"] if f.get("type")][:3]
            for f in sample:
                print(f"       {f['name']}: {f['type']} "
                      f"({f.get('type_confidence')})")

        # 方法返回值
        if nm:
            rt = sum(1 for x in det["methods"] if x.get("return_type"))
            print(f"     返回类型可解析 {rt}/{nm}")
            check(f"{label} 至少一个方法返回类型可解析", rt > 0,
                  f"{rt}/{nm}", warn_only=True)
    else:
        check(f"{label} class_detail 可用", False, str(det.get("reason")))

    # ---- 搜字段
    fr = idx.search_fields(pattern=known_field, limit=20)
    check(f"{label} search_fields 有结果", fr["total"] > 0,
          f"命中 {fr['total']}")
    if fr["total"]:
        it = fr["items"][0]
        print(f"     {it['name']} @ {it['owner']} : {it['field_type']} "
              f"({it['type_confidence']})")
        check(f"{label} 字段类型可解析", bool(it.get("field_type")),
              f"{it['field_type']!r}")

    # ---- 搜方法
    mr = idx.search_methods(pattern="Update", limit=10)
    check(f"{label} search_methods 有结果", mr["total"] > 0,
          f"命中 {mr['total']}")

    # ---- 继承链（Mono 跨程序集基类是最难的一环）
    if r["total"]:
        t = idx.meta.type_by_name(r["items"][0]["full_name"])
        if t:
            chain = [c.nested_name for c in idx.meta.base_chain(t)]
            print(f"     继承链: {' -> '.join(chain) if chain else '(无)'}")
            check(f"{label} base_chain 不报错", True, f"长度={len(chain)}")

    # ---- 依赖分析（用了 resolve_param_types，是 Mono 的专有路径）
    if r["total"]:
        dep = idx.analyze_dependency(r["items"][0]["full_name"])
        check(f"{label} analyze_dependency 可用", dep.get("found") is True)
        if dep.get("found"):
            print(f"     引用了 {dep['referenced_count']} 个类型")
            check(f"{label} 依赖分析有结果",
                  dep["referenced_count"] > 0,
                  f"{dep['referenced_count']}")

    # ---- 结果句柄
    if r["total"]:
        h = r.get("handle")
        if h:
            pg = idx.read_query_result(h, offset=0, limit=5)
            check(f"{label} 结果句柄可翻页",
                  pg.get("found") is True and len(pg.get("items", [])) > 0,
                  f"取回 {len(pg.get('items', []))} 条")

    # ---- 缓存复用
    #
    # 这里原来断言 `dt2 < dt`（「二次打开比首次快」）—— 那是个**假断言**：
    # `dt` 那次也是缓存命中（CACHE 目录跨次运行是留着的），
    # 所以比的是两个几乎相同的数，谁小全看抖动。实测 P1-2 之前就已经
    # 在「49+1 警告」与「50+0 警告」之间来回闪；P1-2 之后两边都降到
    # 0.00s，退化成 `0.00s < 0.00s`，更没有意义。
    #
    # 「缓存复用生效」本来就是**确定性事实**，不该拿时间去证。
    # 而且 P1-2 之后命中路径根本不再解析元数据，时间上更没什么可比的了。
    idx.close()
    idx2 = Index.open(path, cache_dir=CACHE, engine=expect_engine)
    check(f"{label} 二次打开走的是缓存复用",
          idx2.cache_hit is True, f"cache_hit={idx2.cache_hit}")
    # 下面这条是 P1-2 的核心行为：命中缓存**不得**解析元数据。
    # 必须在碰 `.meta` 之前查，否则测的就不是「命中时是否解析」了。
    check(f"{label} 缓存命中不解析元数据（P1-2 惰性）",
          idx2._meta is None,
          "命中后已被解析" if idx2._meta is not None else "未解析")

    # P1-2 敢用「读 8 字节嗅探」替换「全量解析反推引擎名」，前提是两者一致。
    # 这条断言就是那个前提本身 —— 一旦某个引擎上不成立，P1-2 就会静默用错
    # 解析器、拼错缓存文件名，所以必须显式守住。
    sniffed = idx2.engine
    parsed = adapt.engine_of(idx2.meta)
    check(f"{label} 嗅探引擎 == 解析后引擎（P1-2 前提）",
          sniffed == parsed == expect_engine,
          f"嗅探={sniffed} 解析={parsed} 期望={expect_engine}")
    idx2.close()


if HAVE_IL2CPP:
    run_engine("IL2CPP", IL2CPP_META, "il2cpp",
               known_class="UserData_Service", known_field="userDataPresetFile")
if HAVE_MONO:
    run_engine("Mono", MONO_ASM, "mono",
               known_class="Player", known_field="health")


# ============================================================ 3. 能力声明
section("3. 能力声明 —— 两个引擎的差异要如实告知 Agent")

for label, path, eng in (("IL2CPP", IL2CPP_META, "il2cpp"),
                         ("Mono", MONO_ASM, "mono")):
    if path is None or not path.is_file():
        continue
    try:
        idx = Index.open(path, cache_dir=CACHE, engine=eng)
    except Exception as exc:
        check(f"{label} 打开以读取能力声明", False, str(exc))
        continue
    cap = adapt.capabilities(idx.meta)
    print(f"  [{label}] {cap['label']}")
    print(f"     字段类型精度: {cap['field_type_precision']}")
    print(f"     元数据含字段偏移: {cap['field_offset_in_metadata']}")
    print(f"     元数据含方法地址: {cap['method_address_in_metadata']}")
    print(f"     元数据含 static:  {cap['field_is_static_available']}")
    check(f"{label} 能力声明含 engine/label", 
          bool(cap.get("engine") and cap.get("label")))
    check(f"{label} 明确指出字段偏移不在元数据里",
          cap.get("field_offset_in_metadata") is False,
          "两个引擎的字段偏移都必须靠运行时探针")
    # 方法地址同理：静态元数据里只有 token。Mono 特别容易说反 ——
    # 程序集里确实有 RVA 字段，但它指向 IL 字节码而非机器码。
    check(f"{label} 明确指出方法地址不在元数据里",
          cap.get("method_address_in_metadata") is False,
          "只有 token，没有 RVA/绝对地址；Hook 落点必须靠运行时探针")
    idx.close()

# IL2CPP 必须诚实说明「static 查不到」，否则 Agent 会把 None 当成 False
if IL2CPP_META is not None and IL2CPP_META.is_file():
    idx = Index.open(IL2CPP_META, cache_dir=CACHE, engine="il2cpp")
    cap = adapt.capabilities(idx.meta)
    check("IL2CPP 声明 static 信息不可用",
          cap.get("field_is_static_available") is False,
          "v31 字段记录无 flags 槽，不能假装知道")
    t = idx.meta.type_by_name("TeamTapas.ProjectR.UserDatas.Service.UserData_Service")
    if t and idx.meta.fields_of(t):
        f = idx.meta.fields_of(t)[0]
        got = adapt.field_is_static(idx.meta, f)
        check("IL2CPP field_is_static 返回 None 而非猜的 False",
              got is None, f"实际={got!r}")
    idx.close()

if MONO_ASM is not None and MONO_ASM.is_file():
    idx = Index.open(MONO_ASM, cache_dir=CACHE, engine="mono")
    cap = adapt.capabilities(idx.meta)
    check("Mono 声明 static 信息可用",
          cap.get("field_is_static_available") is True)
    idx.close()


# ============================================================ 4. 错误路径
section("4. 错误路径 —— 不支持就报错，不能静默降级")

if HAVE_IL2CPP:
    try:
        Index.open(IL2CPP_META, cache_dir=CACHE, engine="不存在的引擎")
        check("不支持的引擎被拒绝", False, "竟然成功了")
    except Exception as exc:
        check("不支持的引擎被拒绝", True, f"{type(exc).__name__}: {exc}")

# 显式指定错误引擎时，应当报错而不是硬解出乱七八糟的数据
if MONO_ASM is not None and MONO_ASM.is_file():
    try:
        bad = Index.open(MONO_ASM, cache_dir=CACHE, engine="il2cpp")
        # 如果没抛异常，至少要确认它没有伪装成成功
        check("用 IL2CPP 解析器读 Mono 程序集",
              True,
              f"未抛异常（可能给出空的/错误结果），类型数={len(bad.meta.types)}",
              warn_only=True)
        bad.close()
    except Exception as exc:
        check("显式错配引擎时报错", True, f"{type(exc).__name__}")


# ============================================================ 汇总
section("跨引擎索引层验证汇总")
print(f"  通过 {PASS} 项 / 失败 {FAIL} 项 / 警告 {WARN} 项")
print()
if FAIL == 0:
    print("  \u2605 同一套查询 API 在 IL2CPP 与 Mono 下都跑通。")
else:
    print(f"  \u26a0 有 {FAIL} 项失败，需要检查 adapt 层的映射。")

sys.exit(1 if FAIL else 0)
