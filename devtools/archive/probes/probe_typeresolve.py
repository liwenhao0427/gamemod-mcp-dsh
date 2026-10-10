# 诊断：类型索引解析率（field.typeIndex / method.returnType / parameter.typeIndex 能解析出多少）
# 这直接决定工具输出对 Agent 的可用性 —— 若大量返回 None，AI 无法理解字段到底什么类型。
import sys, io, collections
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\Administrator\Documents\deepseek-harness\默认工作区\GameModMCP\src")

from gamemod_mcp.engine.il2cpp_meta import Metadata

META = r"D:\Projects\Garden of Witches\Garden of Witches_Data\il2cpp_data\Metadata\global-metadata.dat"

with Metadata(META) as m:
    bmap = m._byval_to_typedef
    print(f"byval 反查表条目数 = {len(bmap):,}（元数据中类型 {len(m.types):,} 个）")
    print()

    def rate(label, indices):
        total = len(indices)
        if total == 0:
            print(f"  {label}: 无数据")
            return
        ok = sum(1 for i in indices if i in bmap)
        neg = sum(1 for i in indices if i < 0)
        print(f"  {label:<14} 可解析 {ok:>7,} / {total:>7,} = {ok*100.0/total:6.2f}%   "
              f"（其中负值 {neg:,}）")

    print("=== 解析率 ===")
    rate("字段类型", [f.type_index for f in m.fields])
    rate("方法返回类型", [x.return_type for x in m.methods])
    rate("参数类型", [p.type_index for p in m.parameters])
    print()

    # 看看无法解析的字段类型索引长什么样
    unres = [f.type_index for f in m.fields if f.type_index not in bmap]
    c = collections.Counter(unres)
    print(f"=== 无法解析的字段类型索引（共 {len(unres):,} 个，去重 {len(c):,} 个）===")
    print(f"  出现最多的索引: {c.most_common(10)}")
    mn, mx = (min(unres), max(unres)) if unres else (0, 0)
    print(f"  范围: {mn} .. {mx}")
    print(f"  byval 表范围: {min(bmap)} .. {max(bmap)}")
    print()

    # 关键假设：字段的类型索引可能指向「泛型实例」或「数组」，这些 Il2CppType 运行时才建
    # 验证：无法解析的索引是否 > byval 最大值（说明是运行时扩充的类型）
    above = sum(1 for i in unres if i > max(bmap))
    inside = sum(1 for i in unres if 0 <= i <= max(bmap))
    print(f"  超出 byval 最大值的: {above:,}（运行时生成的类型，静态不可还原）")
    print(f"  在 byval 范围内但查不到的: {inside:,}（可能是数组/泛型/指针等衍生类型）")
    print()

    # 抽几个具体例子，人工判断
    print("=== 抽样：无法解析的字段（前 15 个）===")
    shown = 0
    for t in m.types:
        for f in m.fields_of(t):
            if f.type_index not in bmap and shown < 15:
                print(f"  {t.full_name}.{f.name}  typeIndex={f.type_index}")
                shown += 1
        if shown >= 15:
            break

    print()
    print("=== 抽样：可以解析的字段（前 10 个，作为对照）===")
    shown = 0
    for t in m.types:
        for f in m.fields_of(t):
            if f.type_index in bmap and shown < 10:
                print(f"  {t.full_name}.{f.name}  typeIndex={f.type_index} "
                      f"-> {m.resolve_il2cpp_type(f.type_index)}")
                shown += 1
        if shown >= 10:
            break

    print()
    print("=== 泛型类型占比（byval 缺失的主因）===")
    generic = sum(1 for t in m.types if t.generic_container >= 0)
    print(f"  带 genericContainer 的类型: {generic:,} / {len(m.types):,}")
    neg_byval = sum(1 for t in m.types if t.byval_type < 0)
    print(f"  byval_type 为负（无自身 Il2CppType）的类型: {neg_byval:,}")
