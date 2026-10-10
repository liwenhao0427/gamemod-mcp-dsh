# 追查残差里的「普通类」字段
#
# 之前的结论是「无法解析的 12,600 个字段都是运行时构造的数组/泛型/指针」。
# 但 PresetLoaderV2.userDataPresetFile 的 dump 真值是 UnityEngine.TextAsset
# ——一个普通类，本应能解析。说明残差定性有漏洞，必须查清。
import sys, io, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\Administrator\Documents\deepseek-harness\默认工作区\GameModMCP\src")

from gamemod_mcp.engine.il2cpp_meta import Metadata

META = r"D:\Projects\Garden of Witches\Garden of Witches_Data\il2cpp_data\Metadata\global-metadata.dat"

with Metadata(META) as m:
    byval = m._byval_to_typedef
    keys = sorted(byval)

    print("=" * 76)
    print("1. UnityEngine.TextAsset 在元数据里存在吗？")
    print("=" * 76)
    for nm in ("UnityEngine.TextAsset", "TextAsset"):
        t = m.type_by_name(nm)
        print(f"  type_by_name({nm!r}) -> {t}")
    ta = m.type_by_name("UnityEngine.TextAsset")
    if ta:
        print(f"    index={ta.index}  byval_type={ta.byval_type}  image={m.image_of_type(ta)}")

    print()
    print("=" * 76)
    print("2. PresetLoaderV2 的字段逐个核对")
    print("=" * 76)
    t = m.type_by_name("PresetLoaderV2")
    print(f"  类 index={t.index}  字段数={t.field_count}")
    for f in m.fields_of(t)[:12]:
        ti = f.type_index
        v = ti - 1
        inmap = v in byval
        print(f"  {f.name:<34} typeIndex={ti:<7} -1={v:<7} in_byval={inmap}"
              f"  -> {m.resolve_field_type(ti)}")

    print()
    print("=" * 76)
    print("3. 残差字段按「byval 键缺失」分类")
    print("=" * 76)
    # 找出所有解析失败的字段，看它们的 (typeIndex-1) 落在哪个区间
    miss = [f for f in m.fields if (f.type_index - 1) not in byval]
    print(f"  解析失败字段数 = {len(miss):,}")
    lo, hi = min(keys), max(keys)
    print(f"  byval 键区间 = {lo} .. {hi}  （共 {len(keys):,} 个键）")

    below = sum(1 for f in miss if f.type_index - 1 < lo)
    inside = sum(1 for f in miss if lo <= f.type_index - 1 <= hi)
    above = sum(1 for f in miss if f.type_index - 1 > hi)
    print(f"  残差中 (typeIndex-1) 低于下界 : {below:,}")
    print(f"  残差中 (typeIndex-1) 落在区间内: {inside:,}   <- 数值在范围内但键不存在")
    print(f"  残差中 (typeIndex-1) 高于上界 : {above:,}")

    print()
    print("=" * 76)
    print("4. 残差里能对上「某个已知 typedef 的 byval」的比例")
    print("=" * 76)
    # 若残差是泛型实例，其 typeIndex 附近应能找到同族的 byval 键
    near = 0
    for f in miss[:5000]:
        v = f.type_index - 1
        if any(k in byval for k in (v - 1, v + 1)):
            near += 1
    print(f"  残差前 5,000 个中，邻位 ±1 命中 byval 的: {near:,}")

    print()
    print("=" * 76)
    print("5. PresetLoaderV2 那个字段的邻居是谁（typeIndex 附近）")
    print("=" * 76)
    fa = [f for f in m.fields_of(t) if f.name == "userDataPresetFile"][0]
    print(f"  userDataPresetFile typeIndex = {fa.type_index}")
    kk = keys
    import bisect
    pos = bisect.bisect_left(kk, fa.type_index)
    print(f"  该值在 byval 键序列中的位置 = {pos} / {len(kk)}")
    for d in range(max(0, pos - 3), min(len(kk), pos + 4)):
        k = kk[d]
        td = m.types[byval[k]]
        print(f"    键 {k:<8} -> {td.nested_name}   (与本字段 typeIndex 差 {k - fa.type_index:+d})")
