# 决定性验证：用 byvalTypeIndex 反查表解析父类 + 校验 v31 方法记录布局
#
# 原理：typedef.parentIndex(@16) 指向「父类的 Il2CppType」。
#       typedef.byvalTypeIndex(@8) 指向「自己的 Il2CppType」。
#       非泛型/非数组类型的 byvalTypeIndex 就是它自己的 Il2CppType 下标。
#       因此构造 byvalTypeIndex -> typedefIndex 的反查表，
#       就能把 parentIndex 还原成 typedefIndex。
import sys, io, struct, re, collections
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

# ---------- 构造 byval 反查表 ----------
byval_map: dict[int, int] = {}
dup = 0
for i in range(N):
    v = struct.unpack_from("<i", mm, off_td + i * rec + 8)[0]
    if v in byval_map:
        dup += 1
    else:
        byval_map[v] = i

print(f"=== byvalTypeIndex 反查表 ===")
print(f"  条目数 = {len(byval_map):,}（类型 {N:,}，重复 {dup}）")

def resolve_parent(tidx: int) -> int | None:
    """把 typedef 的 parentIndex 解析成 typedef 下标。"""
    p = struct.unpack_from("<i", mm, off_td + tidx * rec + 16)[0]
    if p < 0:
        return None
    return byval_map.get(p)

# ---------- 用 dump 真值评估 ----------
print(f"\n=== 读取真值 {DUMP} ===")
base_of: dict[str, str] = {}
with open(DUMP, "r", encoding="utf-8", errors="replace") as fh:
    for line in fh:
        if line.startswith("class ") or line.startswith("enum "):
            mm2 = re.match(r"(?:class|enum)\s+(\S+)\s*:\s*(\S+)", line)
            if mm2:
                base_of[mm2.group(1)] = mm2.group(2)
print(f"  真值对 = {len(base_of):,}")

hit = miss = unresolved = 0
miss_examples: list[str] = []
for name, base in base_of.items():
    t = m.type_by_name(name)
    if t is None:
        continue
    pi = resolve_parent(t.index)
    if pi is None:
        unresolved += 1
        continue
    got = m.types[pi].full_name
    if got == base:
        hit += 1
    else:
        miss += 1
        if len(miss_examples) < 12:
            miss_examples.append(f"{name}: 期望 {base} / 得到 {got}")

total = hit + miss
print(f"\n=== 父类解析准确率 ===")
print(f"  命中 {hit:,} / 可解析 {total:,} = {hit*100.0/total:.2f}%")
print(f"  无法解析 {unresolved:,}（父类为泛型实例/数组，其 Il2CppType 在运行时生成）")
for e in miss_examples:
    print(f"    反例: {e}")

# ---------- 枚举检测 ----------
print(f"\n=== 枚举检测（基类应为 System.Enum）===")
enum_ok = enum_bad = 0
for name, base in base_of.items():
    if base != "System.Enum":
        continue
    t = m.type_by_name(name)
    if t is None:
        continue
    pi = resolve_parent(t.index)
    got = m.types[pi].full_name if pi is not None else "(未解析)"
    if got == "System.Enum":
        enum_ok += 1
    else:
        enum_bad += 1
print(f"  正确识别枚举 {enum_ok:,}，失败 {enum_bad}")

# ---------- CurrencyKey 明细 ----------
print(f"\n=== CurrencyKey 明细 ===")
ck = m.type_by_name("TeamTapas.ProjectR.Domain.CurrencyKey")
if ck:
    pi = resolve_parent(ck.index)
    print(f"  parentIndex(@16) = {struct.unpack_from('<i', mm, off_td + ck.index*rec + 16)[0]}")
    print(f"  → 解析为 {m.types[pi].full_name if pi is not None else '?'}")
    fs, fc = ck.field_start, ck.field_count
    print(f"  field_start={fs} field_count={fc}")
    for k in range(fc):
        print(f"      {m.fields[fs+k].name}")

# ---------- v31 方法记录布局 ----------
print(f"\n=== v31 方法记录布局校验 (rec=36) ===")
uds = m.type_by_name("TeamTapas.ProjectR.UserDatas.Service.UserData_Service")
print(f"  UserData_Service: method_start={uds.method_start} method_count={uds.method_count}")
print(f"  dump 真值: 首方法 .ctor, 共 50 个")
print(f"  --- 按 (6×int32, token, 4×uint16) = 36 字节解析 ---")
moff, msz = m.tables["methods"]
for k in range(6):
    b = moff + (uds.method_start + k) * 36
    (name_i, decl, ret, pstart, generic, extra) = struct.unpack_from("<6i", mm, b)
    token = struct.unpack_from("<I", mm, b + 24)[0]
    flags, iflags, slot, pcount = struct.unpack_from("<4H", mm, b + 28)
    print(f"    [{k}] {m.read_string(name_i):<28} decl={decl} ret={ret} "
          f"extra={extra} token={token:#010x} flags={flags:#06x} pcount={pcount}")

print(f"\n  --- 按 (5×int32, token, 4×uint16) = 32 字节解析（对照）---")
for k in range(6):
    b = moff + (uds.method_start + k) * 32
    (name_i, decl, ret, pstart, generic) = struct.unpack_from("<5i", mm, b)
    token = struct.unpack_from("<I", mm, b + 20)[0]
    flags, iflags, slot, pcount = struct.unpack_from("<4H", mm, b + 24)
    print(f"    [{k}] {m.read_string(name_i):<28} decl={decl} ret={ret} "
          f"token={token:#010x} flags={flags:#06x} pcount={pcount}")

m.close()
