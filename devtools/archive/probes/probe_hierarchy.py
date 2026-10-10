# 决定性判定：Il2CppTypeDefinition 中 5 个「类型索引」字段的语义
#
# 事实基础（已实测）：
#   @0  nameIndex     ✓ read_string 得到正确的类名
#   @4  namespaceIndex✓
#   @32 fieldStart = 1268 ✓（fields[1268] == "_data"，与 AiMod dump 首字段一致）
#   @36 methodStart
#   @64 method_count=50 ✓ 、@68 field_count=26 ✓（与 AiMod 回执/dump 完全一致）
#   @80 bitfield、@84 token = 0x02xxxxxx ✓
# 待定：@8 @12 @16 @20 @24 中哪几个是 parentIndex
#
# 判定方法：如果某个位置是 TypeDefinition 索引，其取值应几乎全部落在 [0, 20072)
#          或为 -1（无父类/非嵌套/非数组）；若是 Il2CppType 索引，则会大量越界。
import sys, io, struct, re, collections
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\Administrator\Documents\deepseek-harness\默认工作区\GameModMCP\src")

from gamemod_mcp.engine.il2cpp_meta import Metadata

META = r"D:\Projects\Garden of Witches\Garden of Witches_Data\il2cpp_data\Metadata\global-metadata.dat"
m = Metadata(META)
off, size = m.tables["typeDefinitions"]
rec = 88
N = len(m.types)

print(f"类型总数 N = {N}")
print()

# ---------- 1. 各候选位置的取值分布 ----------
print("=== 各候选位置的取值分布（全部 20,072 个类型）===")
print(f"{'位置':>6}{'∈[0,N)':>10}{'== -1':>10}{'== 0':>9}{'越界':>9}{'越界占比':>10}   样例")
for pos in (8, 12, 16, 20, 24):
    in_range = neg1 = zero = out = 0
    samples = []
    for i in range(N):
        v = struct.unpack_from("<i", m._mm, off + i * rec + pos)[0]
        if v == -1:
            neg1 += 1
        elif v == 0:
            zero += 1
        elif 0 <= v < N:
            in_range += 1
            if len(samples) < 3:
                samples.append(f"{v}->{m.types[v].full_name}")
        else:
            out += 1
            if len(samples) < 3:
                samples.append(f"{v}(越界)")
    pct = out * 100 / N
    print(f"@{pos:<5}{in_range:>10,}{neg1:>10,}{zero:>9,}{out:>9,}{pct:>9.1f}%   {samples}")

# ---------- 2. 用 AiMod dump 作为地面真值交叉验证 ----------
DUMP = r"D:\Projects\aiMod\dump\Garden of Witches.exe\il2cpp_dump.cs"
print(f"\n=== 从 AiMod dump 提取继承关系作为真值: {DUMP} ===")

base_of: dict[str, str] = {}
try:
    with open(DUMP, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("class "):
                # class A.B.C : D.E
                mm = re.match(r"class\s+(\S+)\s*:\s*(\S+)", line)
                if mm:
                    base_of[mm.group(1)] = mm.group(2)
            elif line.startswith("enum "):
                mm = re.match(r"enum\s+(\S+)\s*:\s*(\S+)", line)
                if mm:
                    base_of[mm.group(1)] = mm.group(2)
except OSError as exc:
    print(f"  读取失败: {exc}")

print(f"  dump 中提取到 {len(base_of):,} 条继承关系")

# 取一批「有非 Object 父类」的类型做检验
test_cases = []
for name, base in base_of.items():
    if base in ("System.Object", "System.ValueType", "System.Enum"):
        continue
    t = m.type_by_name(name)
    if t is None:
        continue
    test_cases.append((name, base, t))

print(f"  其中能在元数据中定位到、且父类非 Object/ValueType/Enum 的: {len(test_cases):,}")
test_cases = test_cases[:400]
print(f"  用前 {len(test_cases)} 个做判定\n")

print("=== 各候选位置能否复现 dump 的父类 ===")
for pos in (8, 12, 16, 20, 24):
    hit = miss = unresolvable = 0
    examples = []
    for name, base, t in test_cases:
        v = struct.unpack_from("<i", m._mm, off + t.index * rec + pos)[0]
        if not (0 <= v < N):
            unresolvable += 1
            continue
        got = m.types[v].full_name
        if got == base:
            hit += 1
        else:
            miss += 1
            if len(examples) < 3:
                examples.append(f"{name}: 期望{base} 得到{got}")
    total = hit + miss
    acc = (hit * 100 / total) if total else 0.0
    print(f"  @{pos:<4} 命中 {hit:>4} / 可解析 {total:>4}  准确率 {acc:>5.1f}%  "
          f"(不可解析 {unresolvable})")
    for e in examples:
        print(f"        反例: {e}")

m.close()
