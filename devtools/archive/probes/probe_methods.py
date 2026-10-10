# 决定性校验：methods 表布局与 methodStart 语义 + 全局一致性
#
# 已确认：
#   typedef v31 = 88 字节，name@0 ns@4 byval@8 decl@12 parent@16 elem@20 generic@24
#                  flags@28 fieldStart@32 methodStart@36 ... counts@64..78 bitfield@80 token@84
#   methods 表 5,022,036 / 36 = 139,501（整除；/32 不整除，故 36 正确）
#   token 位于方法记录 @24（观察到 0x06000001 连续递增）
# 待判定：methodStart=1492 是否指向真实首方法（疑似差 1）
import sys, io, struct, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\Administrator\Documents\deepseek-harness\默认工作区\GameModMCP\src")

from gamemod_mcp.engine.il2cpp_meta import Metadata

META = r"D:\Projects\Garden of Witches\Garden of Witches_Data\il2cpp_data\Metadata\global-metadata.dat"
DUMP = r"D:\Projects\aiMod\dump\Garden of Witches.exe\il2cpp_dump.cs"

m = Metadata(META)
mm = m._mm
N = len(m.types)
off_td, _ = m.tables["typeDefinitions"]
rec = 88
moff, msz = m.tables["methods"]
MSTRIDE = 36
foff, fsz = m.tables["fields"]
FTOTAL = fsz // 12
MTOTAL = msz // MSTRIDE

print(f"methods 记录数 = {MTOTAL:,}   fields 记录数 = {FTOTAL:,}")

def tok(mi: int) -> int:
    return struct.unpack_from("<I", mm, moff + mi * MSTRIDE + 24)[0]

def mname(mi: int) -> str:
    ni = struct.unpack_from("<i", mm, moff + mi * MSTRIDE)[0]
    return m.read_string(ni)

# ---------- 检查 B：token==1 的记录下标 ----------
print("\n=== 检查 B: token 0x06000001 所在下标 ===")
first_idx = -1
for i in range(0, MTOTAL):
    if tok(i) == 0x06000001:
        first_idx = i
        break
print(f"  首个 token=0x06000001 的 methods 下标 = {first_idx}")
print(f"  该下标前 3 条: {[(i, mname(i), hex(tok(i))) for i in range(max(0,first_idx-2), first_idx+1)]}")

# ---------- 检查 A：methodStart 是否差 1 ----------
print("\n=== 检查 A: methodStart(+delta) 处 token 是否合法(高字节 0x06) ===")
print(f"{'delta':>6}{'合法':>10}{'总数':>10}{'比例':>9}")
for delta in (-2, -1, 0, 1, 2):
    ok = tot = 0
    for i in range(N):
        b = off_td + i * rec
        mc = struct.unpack_from("<H", mm, b + 64)[0]
        ms = struct.unpack_from("<i", mm, b + 36)[0]
        if mc == 0:
            continue
        idx = ms + delta
        if idx < 0 or idx >= MTOTAL:
            continue
        tot += 1
        if (tok(idx) >> 24) == 0x06:
            ok += 1
    pct = ok * 100.0 / tot if tot else 0.0
    print(f"{delta:>6}{ok:>10,}{tot:>10,}{pct:>8.1f}%")

# ---------- 检查 C：全局计数一致性 ----------
print("\n=== 检查 C: 全局计数一致性 ===")
s_mc = s_fc = s_pc = s_nc = s_vc = s_ic = 0
for i in range(N):
    b = off_td + i * rec
    s_mc += struct.unpack_from("<H", mm, b + 64)[0]
    s_pc += struct.unpack_from("<H", mm, b + 66)[0]
    s_fc += struct.unpack_from("<H", mm, b + 68)[0]
    s_nc += struct.unpack_from("<H", mm, b + 72)[0]
    s_vc += struct.unpack_from("<H", mm, b + 74)[0]
    s_ic += struct.unpack_from("<H", mm, b + 76)[0]

print(f"  Σmethod_count  = {s_mc:>9,}   / methods 表 = {MTOTAL:>9,}   差 {MTOTAL - s_mc:>6,}")
print(f"  Σfield_count   = {s_fc:>9,}   / fields  表 = {FTOTAL:>9,}   差 {FTOTAL - s_fc:>6,}")
print(f"  Σproperty_count= {s_pc:>9,}")
print(f"  Σnested_count  = {s_nc:>9,}")
print(f"  Σvtable_count  = {s_vc:>9,}")
print(f"  Σiface_count   = {s_ic:>9,}")

# ---------- 检查 D：UserData_Service 方法对照 dump ----------
print("\n=== 检查 D: UserData_Service 方法（元数据）vs dump ===")
uds = m.type_by_name("TeamTapas.ProjectR.UserDatas.Service.UserData_Service")
ms = struct.unpack_from("<i", mm, off_td + uds.index * rec + 36)[0]
mc = struct.unpack_from("<H", mm, off_td + uds.index * rec + 64)[0]
print(f"  method_start={ms}  method_count={mc}")
print("  --- 按 delta=0 ---")
for k in range(4):
    print(f"      [{ms+k}] {mname(ms+k)}  token={tok(ms+k):#010x}")
print("  --- 按 delta=1 ---")
for k in range(4):
    print(f"      [{ms+1+k}] {mname(ms+1+k)}  token={tok(ms+1+k):#010x}")

# 从 dump 提取该类的方法名
print("  --- dump 真值 ---")
in_class = False
count = 0
with open(DUMP, "r", encoding="utf-8", errors="replace") as fh:
    for line in fh:
        if line.startswith("class TeamTapas.ProjectR.UserDatas.Service.UserData_Service "):
            in_class = True
            continue
        if in_class:
            if line.startswith("}"):
                break
            mm3 = re.match(r"\s*/\*0x[0-9a-f]+\*/\s+(.+?)\s+(\S+)\(", line)
            if mm3:
                print(f"      {mm3.group(2)}")
                count += 1
                if count >= 4:
                    break
m.close()
