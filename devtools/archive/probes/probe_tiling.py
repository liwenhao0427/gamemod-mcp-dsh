# 决定性判定：method_start 是否存在 -1 偏移
# 原理：若 method_start 正确，则所有类型的 [start, start+count) 区间应
#       **恰好无缝铺满** [0, 方法总数)，既不重叠也不留空隙。
#       这是比 token 合法性更强的约束（token 检测因方法连续而无区分力）。
import sys, io, struct, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\Administrator\Documents\deepseek-harness\默认工作区\GameModMCP\src")

from gamemod_mcp.engine.il2cpp_meta import Metadata

META = r"D:\Projects\Garden of Witches\Garden of Witches_Data\il2cpp_data\Metadata\global-metadata.dat"
DUMP = r"D:\Projects\aiMod\dump\Garden of Witches.exe\il2cpp_dump.cs"

with Metadata(META) as m:
    TOTAL = len(m.methods)

    for delta in (0, -1):
        cover = [0] * (TOTAL + 2)
        for t in m.types:
            s = t.method_start + delta
            c = t.method_count
            if c <= 0:
                continue
            if s < 0 or s + c > TOTAL:
                continue
            for k in range(s, s + c):
                cover[k] += 1

        zero = sum(1 for k in range(TOTAL) if cover[k] == 0)
        one = sum(1 for k in range(TOTAL) if cover[k] == 1)
        over = sum(1 for k in range(TOTAL) if cover[k] > 1)
        print(f"delta={delta:>3}: 恰好覆盖1次={one:>7,}  未覆盖={zero:>6,}  重叠={over:>7,}  总={TOTAL:,}")
        # 找出未覆盖/重叠的具体位置
        if zero:
            gaps = [k for k in range(TOTAL) if cover[k] == 0][:5]
            print(f"          空隙位置: {gaps} -> 名字: {[m.methods[k].name for k in gaps]}")
        if over:
            ovs = [k for k in range(TOTAL) if cover[k] > 1][:5]
            print(f"          重叠位置: {ovs} -> 名字: {[m.methods[k].name for k in ovs]}")

    # ---- 精确统计 dump 中 UserData_Service 的方法数与首方法
    print()
    print("=== dump 中 UserData_Service 的精确方法列表 ===")
    names = []
    in_class = False
    depth = 0
    with open(DUMP, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("class TeamTapas.ProjectR.UserDatas.Service.UserData_Service "):
                in_class = True
                depth = 1
                continue
            if in_class:
                if line.startswith("{"):
                    continue
                if line.startswith("}"):
                    break
                mm2 = re.match(r"\s*/\*0x[0-9a-f]+\*/\s+.+?\s+(\S+)\(", line)
                if mm2:
                    names.append(mm2.group(1))
    print(f"  方法数 = {len(names)}")
    print(f"  前 5 个 = {names[:5]}")
    print(f"  后 3 个 = {names[-3:]}")

    print()
    print("=== 元数据 method_start=1492 附近 ===")
    for k in range(1488, 1497):
        owner = None
        for t in m.types:
            if t.method_start <= k < t.method_start + t.method_count:
                owner = t.full_name
                break
        print(f"    [{k}] {m.methods[k].name:<24} 归属={owner}")

    print()
    print("=== 元数据对比 ===")
    for label, delta in (("delta=0", 0), ("delta=-1", -1)):
        t = m.type_by_name("TeamTapas.ProjectR.UserDatas.Service.UserData_Service")
        s = t.method_start + delta
        got = [m.methods[s + k].name for k in range(min(5, t.method_count))]
        print(f"  {label}: {got}")
