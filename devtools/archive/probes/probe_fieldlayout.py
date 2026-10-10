# 决定性排查：字段记录的 typeIndex 到底在第几个 int32 位置
#
# 现象：方法返回类型能解析出 96.64%，参数字段 82.70%，但**字段类型 0%**。
# 这种量级的不对称不可能是数据本身造成的 —— 只可能是字段记录的字段顺序读错了。
#
# 做法：对 fields 记录的 3 个 int32 位置，分别统计其取值有多少落在
#       byval 反查表的键集合里。命中率最高的那个位置才是真正的 typeIndex。
import sys, io, struct, collections
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\Administrator\Documents\deepseek-harness\默认工作区\GameModMCP\src")

from gamemod_mcp.engine.il2cpp_meta import Metadata

META = r"D:\Projects\Garden of Witches\Garden of Witches_Data\il2cpp_data\Metadata\global-metadata.dat"

with Metadata(META) as m:
    keys = set(m._byval_to_typedef.keys())
    print(f"byval 键集合大小 = {len(keys):,}，范围 {min(keys)} .. {max(keys)}")
    print()

    entry = m.tables["fields"]
    n = entry.size // 12
    print(f"fields 记录数 = {n:,}（每条 12 字节）")
    print()

    # ---- 统计每个 int32 位置的命中率
    hits = [0, 0, 0]
    samples = []
    for i in range(n):
        b = entry.offset + i * 12
        a, c, d = struct.unpack_from("<3i", m._mm, b)
        for j, v in enumerate((a, c, d)):
            if v in keys:
                hits[j] += 1
        if i < 5:
            samples.append((a, c, d))

    print("=== 各 int32 位置落在 byval 键集合中的次数 ===")
    for j in range(3):
        print(f"  位置 @{j*4:<3} : {hits[j]:>7,} / {n:,} = {hits[j]*100.0/n:6.2f}%")
    print()
    print(f"  前 5 条原始记录: {samples}")
    print()

    # ---- 位置 1（@4）看起来像 typeIndex，验证它能否解析出合理类型
    print("=== 假设 @4 是 typeIndex：抽样解析 ===")
    shown = 0
    for t in m.types:
        for i in range(t.field_start, t.field_start + t.field_count):
            if i < 0 or i >= n:
                continue
            b = entry.offset + i * 12
            name_idx, v1, v2 = struct.unpack_from("<3i", m._mm, b)
            nm = m.read_string(name_idx)
            if v1 in keys and shown < 12:
                print(f"  {t.full_name}.{nm}  @4={v1} -> {m.resolve_il2cpp_type(v1)}"
                      f"   @8=0x{v2:08X}")
                shown += 1
        if shown >= 12:
            break

    print()
    print("=== 假设 @4 是 typeIndex：非编译器生成类的字段解析率 ===")
    ok = tot = 0
    for t in m.types:
        if t.name.startswith("<"):
            continue
        for i in range(t.field_start, t.field_start + t.field_count):
            if i < 0 or i >= n:
                continue
            b = entry.offset + i * 12
            _, v1, _ = struct.unpack_from("<3i", m._mm, b)
            tot += 1
            if v1 in keys:
                ok += 1
    print(f"  {ok:,} / {tot:,} = {ok*100.0/tot:.2f}%")
