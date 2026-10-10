# 判定 v31 Il2CppTypeDefinition 前 7 个 int32 的语义
# 决定性检验：CurrencyKey 必须继承 System.Enum；UserData_Service 的 field_count 必须是 26
import sys, io, struct
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\Administrator\Documents\deepseek-harness\默认工作区\GameModMCP\src")

from gamemod_mcp.engine.il2cpp_meta import Metadata

META = r"D:\Projects\Garden of Witches\Garden of Witches_Data\il2cpp_data\Metadata\global-metadata.dat"
m = Metadata(META)

off, size = m.tables["typeDefinitions"]
rec = 88

# 已确认：@28=flags, @32=fieldStart, @36=methodStart, @64起 8 个 ushort, @80=bitfield, @84=token
print("=== 用 token 验证布局（token 高字节应为 0x02 = TypeDef 表）===")
ok = 0
for i in range(0, 2000):
    b = off + i * rec
    token = struct.unpack_from("<I", m._mm, b + 84)[0]
    if (token >> 24) == 0x02:
        ok += 1
print(f"  前 2000 个类型中 token 表号为 0x02 的比例: {ok}/2000")
print(f"  理论上界 {0x02000000:#x}，样例 token: "
      f"{struct.unpack_from('<I', m._mm, off + 343 * rec + 84)[0]:#010x}")
print()

print("=== 候选语义对比（前 7 个 int32）===")
CANDS = [
    ("A: name,ns,byval,byref,decl,parent,element", ["name", "ns", "byval", "byref", "decl", "parent", "element"]),
    ("B: name,ns,byval,decl,parent,element,generic", ["name", "ns", "byval", "decl", "parent", "element", "generic"]),
]

for label, names in CANDS:
    print(f"\n--- 假设 {label} ---")
    for type_name in (
        "TeamTapas.ProjectR.Domain.CurrencyKey",
        "TeamTapas.ProjectR.UserDatas.Service.UserData_Service",
    ):
        t = m.type_by_name(type_name)
        if not t:
            print(f"  {type_name}: 未找到")
            continue
        b = off + t.index * rec
        vals = struct.unpack_from("<7i", m._mm, b)
        kv = dict(zip(names, vals))
        print(f"  {type_name}")
        print(f"    {kv}")
        # 解析各索引指向的类型名
        for k, v in kv.items():
            if k in ("name", "ns"):
                continue
            if 0 <= v < len(m.types):
                print(f"      {k:9} = {v:>7} -> {m.types[v].full_name}")
            else:
                print(f"      {k:9} = {v:>7} -> (无效/空)")

print("\n=== 用「数组类型」判定 elementTypeIndex ===")
# 找一个 elementTypeIndex 有明显含义的类型：名字含 []（数组）不是 typedef；
# 改找泛型容器/嵌套类型做交叉验证
found = 0
for i in range(len(m.types)):
    b = off + i * rec
    v12, v16, v20, v24 = struct.unpack_from("<4i", m._mm, b + 12)
    t = m.types[i]
    # 顶层类型：declaringTypeIndex 应为 -1
    if "." in t.full_name or "+" in t.name:
        continue
    if v12 == -1 and v16 not in (-1,) and v20 == -1:
        print(f"  类型 {t.full_name}: @12={v12} @16={v16} @20={v20} @24={v24}")
        if 0 <= v16 < len(m.types):
            print(f"      @16 -> {m.types[v16].full_name}")
        found += 1
        if found >= 8:
            break

m.close()
