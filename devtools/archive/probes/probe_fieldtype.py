# 定点排查：字段记录的 @4 到底是什么？
#
# 关键疑点：方法返回类型 96.64% 能通过 byval 反查表还原，字段类型却 0%。
# 先看一个已知答案的样本（_data 的类型是 UserData），逐步核对。
import sys, io, struct
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\Administrator\Documents\deepseek-harness\默认工作区\GameModMCP\src")

from gamemod_mcp.engine.il2cpp_meta import Metadata

META = r"D:\Projects\Garden of Witches\Garden of Witches_Data\il2cpp_data\Metadata\global-metadata.dat"

with Metadata(META) as m:
    keys = set(m._byval_to_typedef)
    e = m.tables["fields"]
    print(f"fields 表: offset={e.offset:#x} size={e.size} 记录数={e.size//12:,}")
    print(f"byval 键: {len(keys):,} 个，范围 {min(keys)} .. {max(keys)}")
    print()

    # ---------- 用对象模型读 _data
    f = m.fields[1268]
    b = e.offset + 1268 * 12
    a, c, d = struct.unpack_from("<3i", m._mm, b)
    print("=== fields[1268] 原始数据 ===")
    print(f"  @0 name_idx = {a} -> '{m.read_string(a)}'")
    print(f"  @4          = {c}")
    print(f"  @8          = 0x{d:08X}")
    print(f"  对象模型: name={f.name!r} type_index={f.type_index} token=0x{f.token:08X}")
    print(f"  @4 是否在 byval 键集合中: {c in keys}")
    print()

    # ---------- UserData 是什么
    print("=== 名为 UserData 的类型定义 ===")
    for t in m.types:
        if t.name == "UserData":
            print(f"  idx={t.index} full={t.full_name} byval={t.byval_type} "
                  f"byval在键中={t.byval_type in keys}")
    print()

    # ---------- UserData_Service 的全部字段
    uds = m.type_by_name("TeamTapas.ProjectR.UserDatas.Service.UserData_Service")
    print("=== UserData_Service 字段（@4 与 byval 键的关系）===")
    print(f"  idx={uds.index} field_start={uds.field_start} count={uds.field_count}")
    for i in range(uds.field_start, uds.field_start + uds.field_count):
        bb = e.offset + i * 12
        na, v4, v8 = struct.unpack_from("<3i", m._mm, bb)
        print(f"  [{i:>5}] {m.read_string(na):<42} @4={v4:<7} in_keys={v4 in keys}")

    # ---------- 位置 0 是不是 typeIndex？看它能否解释 @0 的语义
    print()
    print("=== 验证 @0 是否可能是 typeIndex（对照：@0 多为小值=字符串偏移）===")
    for i in range(0, 6):
        bb = e.offset + i * 12
        na, v4, v8 = struct.unpack_from("<3i", m._mm, bb)
        print(f"  [{i}] @0={na:<7}（字符串='{m.read_string(na)}'） @4={v4} @8=0x{v8:08X}")

    # ---------- 全部字段的 @4 落在哪些区间
    n = e.size // 12
    below = within = above = 0
    inkeys = 0
    for i in range(n):
        _, v4, _ = struct.unpack_from("<3i", m._mm, e.offset + i * 12)
        if v4 < min(keys):
            below += 1
        elif v4 > max(keys):
            above += 1
        else:
            within += 1
        if v4 in keys:
            inkeys += 1
    print()
    print("=== 全部 81,872 个字段的 @4 分布 ===")
    print(f"  低于 byval 最小值({min(keys)}) : {below:,}")
    print(f"  落在 byval 区间内            : {within:,}")
    print(f"  高于 byval 最大值({max(keys)}) : {above:,}")
    print(f"  命中 byval 键集合            : {inkeys:,}")

    # ---------- 方法返回类型做同样统计，作为对照
    me = m.tables["methods"]
    mn = me.size // 36
    minkeys = 0
    for i in range(mn):
        _, _, ret, _, _, _ = struct.unpack_from("<6i", m._mm, me.offset + i * 36)
        if ret in keys:
            minkeys += 1
    print()
    print(f"=== 对照：{mn:,} 个方法的 returnType(@8) 命中 byval 键集合 = {minkeys:,} "
          f"({minkeys*100.0/mn:.2f}%) ===")

    # ---------- 方法的 @4 与 @8 谁命中
    d4 = sum(1 for i in range(mn)
             if struct.unpack_from("<6i", m._mm, me.offset + i * 36)[1] in keys)
    print(f"    方法 declaringType(@4) 命中 = {d4:,} ({d4*100.0/mn:.2f}%)")
