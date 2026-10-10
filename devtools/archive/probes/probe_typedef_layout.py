# 用地面真值反推 Il2CppTypeDefinition 的精确字段布局
# 真值来源：AiMod 对同一游戏的运行时回执
#   UserData_Service: 26 个字段（_data, <storeStatSync>k__BackingField, ... , messageHub）
#                     50 个方法
#   CurrencyKey: 枚举，成员 {Gold, JungleSeed, Lapis}
import sys, io, struct
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\Administrator\Documents\deepseek-harness\默认工作区\GameModMCP\src")

from gamemod_mcp.engine.il2cpp_meta import Metadata

META = r"D:\Projects\Garden of Witches\Garden of Witches_Data\il2cpp_data\Metadata\global-metadata.dat"
m = Metadata(META)

# ---- 1. 找到 fields 表里 "_data" + "<storeStatSync>k__BackingField" 的连续序列
target_start = -1
for i in range(len(m.fields) - 3):
    if (m.fields[i].name == "_data"
            and m.fields[i + 1].name == "<storeStatSync>k__BackingField"
            and m.fields[i + 2].name == "<contents>k__BackingField"):
        target_start = i
        break

print(f"fields 表中真实起点索引 = {target_start}")
if target_start >= 0:
    for k in range(28):
        print(f"    [{target_start + k:>7}] {m.fields[target_start + k].name}")

# ---- 2. 找到该类型记录，dump 原始字段
t = m.type_by_name("TeamTapas.ProjectR.UserDatas.Service.UserData_Service")
print(f"\n类型索引 = {t.index}")
print(f"解析得到的 field_start  = {t.field_start}")
print(f"解析得到的 method_start = {t.method_start}")
print(f"解析得到的 field_count  = {t.field_count}")
print(f"解析得到的 method_count = {t.method_count}")

# 重新按原始字节读这条记录
off, size = m.tables["typeDefinitions"]
rec = 88
b = off + t.index * rec
raw = m._mm[b:b + rec]
print(f"\n记录原始 hex ({rec} 字节):")
print("  " + " ".join(f"{x:02x}" for x in raw))

print("\n按 int32 解读（前 17 个）:")
for i in range(18):
    v = struct.unpack_from("<i", raw, i * 4)[0]
    u = struct.unpack_from("<I", raw, i * 4)[0]
    print(f"    int32[{i:2}] @{i*4:2} = {v:>12}  (0x{u:08X})")

print("\n从偏移 60 起按 int16 解读:")
for base in range(60, 80, 2):
    vals = struct.unpack_from("<8H", raw, base)
    print(f"    @{base:2}: {vals}")

# ---- 3. CurrencyKey 的父链（验证 is_enum）
print("\n=== CurrencyKey 父链 ===")
ck = m.type_by_name("TeamTapas.ProjectR.Domain.CurrencyKey")
if ck:
    print(f"  parent_index = {ck.parent_index}")
    if 0 <= ck.parent_index < len(m.types):
        p = m.types[ck.parent_index]
        print(f"  父类型 = {p.full_name}")
    cur = ck
    for _ in range(6):
        p = m.parent_of(cur)
        if p is None:
            print("  (父链终止)")
            break
        print(f"    {cur.full_name} -> {p.full_name}")
        cur = p

m.close()
