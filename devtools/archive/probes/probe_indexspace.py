# 决定性探针：同一文件内，各类「类型索引」字段的索引空间约定
#
# 观察到的矛盾：若字段 typeIndex 与 byvalTypeIndex 同处一个 Il2CppType 空间，
#   则 delta 0 应有一定命中率；实测恰好 0.00%，且两集合交集为 0（统计上不可能）。
#   而 delta -1 命中 44.64%，且用 dump 真值验证精度 100%。
# 本探针把「谁和谁同空间」一次问清：
#   parentIndex / declaringTypeIndex / elementTypeIndex / genericContainerIndex
#   method.returnType / method.declaringType / parameter.typeIndex / field.typeIndex
#   各自的命中率曲线，用同一张 {@8} 键集合衡量。
import sys, io, struct, collections
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\Administrator\Documents\deepseek-harness\默认工作区\GameModMCP\src")

from gamemod_mcp.engine.il2cpp_meta import Metadata

META = r"D:\Projects\Garden of Witches\Garden of Witches_Data\il2cpp_data\Metadata\global-metadata.dat"
DELTAS = list(range(-3, 4))


def curve(vals, keys):
    n = len(vals)
    return [(d, sum(1 for v in vals if (v + d) in keys) * 100.0 / n if n else 0.0)
            for d in DELTAS]


with Metadata(META) as m:
    byval_set = set(t.byval_type for t in m.types)
    td = m.tables["typeDefinitions"]
    T = len(m.types)

    def tfield(off):
        return [struct.unpack_from("<i", m._mm, td.offset + i * 88 + off)[0]
                for i in range(T)]

    rows = {
        "typedef.parentIndex        @16": tfield(16),
        "typedef.declaringTypeIndex @12": tfield(12),
        "typedef.elementTypeIndex   @20": tfield(20),
        "typedef.genericContainer   @24": tfield(24),
    }
    me = m.tables["methods"]
    MN = me.size // 36
    rows["method.returnType       @8 "] = [
        struct.unpack_from("<6i", m._mm, me.offset + i * 36)[2] for i in range(MN)]
    rows["method.declaringType    @4 "] = [
        struct.unpack_from("<6i", m._mm, me.offset + i * 36)[1] for i in range(MN)]
    pe = m.tables["parameters"]
    PN = pe.size // 12
    rows["param.typeIndex         @8 "] = [
        struct.unpack_from("<3i", m._mm, pe.offset + i * 12)[2] for i in range(PN)]
    fe = m.tables["fields"]
    FN = fe.size // 12
    rows["field.typeIndex         @4 "] = [
        struct.unpack_from("<3i", m._mm, fe.offset + i * 12)[1] for i in range(FN)]

    print("=" * 104)
    print(f"命中率曲线（对同一 @8 byvalTypeIndex 键集合，键数={len(byval_set):,}）")
    print("=" * 104)
    print(f"{'字段':<34}{'样本数':>9} " + "".join(f"{d:+d}".rjust(9) for d in DELTAS))
    print("-" * 104)
    for label, vals in rows.items():
        c = curve(vals, byval_set)
        best = max(c, key=lambda kv: kv[1])
        print(f"{label:<34}{len(vals):>9,} "
              + "".join(f"{r:8.2f}%" for _, r in c)
              + f"   最佳={best[0]:+d} ({best[1]:.2f}%)")

    # ---------- 关键检验：字段索引空间是否与 byval 空间「整段错开」
    print()
    print("=" * 104)
    print("关键检验：各索引空间的取值集合与 byval 键集合的交集")
    print("=" * 104)
    for label, vals in rows.items():
        s = set(vals)
        inter = len(s & byval_set)
        print(f"  {label:<34} 去重值 {len(s):>7,}  与byval交集 {inter:>7,}  "
              f"范围 {min(s):>7,} .. {max(s):>7,}")

    # ---------- 检验「运行时 byval = @8 + 1」假设
    print()
    print("=" * 104)
    print("检验假设：运行时 Il2CppType 的 byval 索引 = @8 + 1（即 @8 是 0 基、运行时 1 基）")
    print("=" * 104)
    shifted = {v + 1 for v in byval_set}
    for label, vals in rows.items():
        hit0 = sum(1 for v in vals if v in byval_set)
        hit1 = sum(1 for v in vals if v in shifted)
        n = len(vals)
        print(f"  {label:<34} 命中@8集合 {hit0*100.0/n:6.2f}%   "
              f"命中@8加1集合 {hit1*100.0/n:6.2f}%")

    # ---------- 直接看几个具体类的自洽性
    print()
    print("=" * 104)
    print("具体类自洽性：某类的 parentIndex 与该类字段的 typeIndex 分别落在哪个空间")
    print("=" * 104)
    for name in ("UnityEngine.MonoBehaviour", "UnityEngine.GameObject",
                 "TeamTapas.ProjectR.SaveDatas.SaveData"):
        t = m.type_by_name(name)
        if t is None:
            print(f"  {name}: 未找到")
            continue
        byv = t.byval_type
        pv = struct.unpack_from("<i", m._mm, td.offset + t.index * 88 + 16)[0]
        pname = m.types[pv].full_name if 0 <= pv < T else "?"
        print(f"  {name}")
        print(f"     typedef idx={t.index}  @8(byval)={byv}  parentIndex(@16)={pv} "
              f"父={'在byval集合内' if pv in byval_set else '不在'} ({pname})")
        fs = m.fields_of(t)
        if fs:
            idxs = [struct.unpack_from("<i", m._mm, fe.offset + f.index * 12 + 4)[0]
                    for f in fs[:6]]
            print(f"     前 6 个字段 typeIndex={idxs} "
                  f"各自在被byval集合内={[v in byval_set for v in idxs]} "
                  f"各自 (v-1) 在被byval集合内={[(v - 1) in byval_set for v in idxs]}")
