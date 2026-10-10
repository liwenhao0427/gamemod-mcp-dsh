# 权威判定：用方法记录自带的 declaring_type 字段核对 method_start
#
# 每个 Il2CppMethodDefinition 都记录了 declaringType 下标（Il2CppType 形式）。
# 通过 byval 反查表可还原成 typedef 下标 —— 这是独立于 method_start 的第二证据源。
# 若两者一致，则 method_start 语义确认为「区间起点，无偏移」。
import sys, io, struct, collections
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\Administrator\Documents\deepseek-harness\默认工作区\GameModMCP\src")

from gamemod_mcp.engine.il2cpp_meta import Metadata

META = r"D:\Projects\Garden of Witches\Garden of Witches_Data\il2cpp_data\Metadata\global-metadata.dat"

with Metadata(META) as m:
    # declaring_type 是 Il2CppType 下标，用 byval 反查表还原成 typedef 下标
    owners: dict[int, list[int]] = collections.defaultdict(list)
    unresolved = 0
    for meth in m.methods:
        ti = m._byval_to_typedef.get(meth.declaring_type)
        if ti is None:
            unresolved += 1
            continue
        owners[ti].append(meth.index)

    print(f"方法总数 = {len(m.methods):,}")
    print(f"declaringType 无法还原的方法数 = {unresolved:,}")
    print()

    # ---- 1. 用 declaring_type 推每个类型的真实区间，与 method_start 比对
    agree = disagree = 0
    off_by = collections.Counter()
    samples = []
    for t in m.types:
        idxs = owners.get(t.index)
        if not idxs:
            continue
        lo, hi = min(idxs), max(idxs)
        contiguous = (hi - lo + 1) == len(idxs)
        if not contiguous:
            continue
        if lo == t.method_start and (hi - lo + 1) == t.method_count:
            agree += 1
        else:
            disagree += 1
            off_by[lo - t.method_start] += 1
            if len(samples) < 10:
                samples.append(
                    f"{t.full_name}: method_start={t.method_start} count={t.method_count} "
                    f"/ declaringType区间=[{lo},{hi}] 共{len(idxs)}"
                )

    print("=== 用 declaring_type 重建区间 vs method_start ===")
    print(f"  完全一致      : {agree:,}")
    print(f"  不一致        : {disagree:,}")
    print(f"  起点差值分布  : {dict(off_by.most_common(6))}")
    for s in samples:
        print(f"    {s}")

    print()
    print("=== UserData_Service 明细 ===")
    uds = m.type_by_name("TeamTapas.ProjectR.UserDatas.Service.UserData_Service")
    idxs = sorted(owners.get(uds.index, []))
    print(f"  method_start = {uds.method_start}")
    print(f"  method_count = {uds.method_count}")
    print(f"  declaring_type 还原出的方法下标({len(idxs)}个): [{idxs[0]}..{idxs[-1]}]")
    print(f"  首个 6 个方法名: {[m.methods[k].name for k in idxs[:6]]}")
    print(f"  末 3 个方法名  : {[m.methods[k].name for k in idxs[-3:]]}")

    print()
    print("=== TreeNode 明细（method_start=1491 的归属者）===")
    for t in m.types:
        if t.name == "TreeNode" and t.method_start <= 1491 < t.method_start + t.method_count:
            print(f"  {t.full_name}  method_start={t.method_start} count={t.method_count}")
            print(f"  区间内方法: {[m.methods[k].name for k in range(t.method_start, t.method_start + t.method_count)]}")
            print(f"  declaring_type 还原出的下标: {sorted(owners.get(t.index, []))}")
            break
