# 定位 Il2CppType 表，并打通 parentIndex -> 基类 的解析链路
#
# 背景：Il2CppTypeDefinition 里的 byvalType/declaringType/parent/element/genericContainer
#       以及 Field.typeIndex、Method.returnType、Parameter.typeIndex
#       都是 Il2CppType 表的下标，而不是 typeDefinitions 的下标。
#       Il2CppType 内部的 data.klassIndex 才指向 typeDefinitions。
# 本脚本用 AiMod dump 的真值暴力搜索：(表偏移, 步长, klassIndex 字段偏移, typeEnum 偏移)
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

# ---------- 1. 收集所有「指向 Il2CppType」的索引，确定上界 ----------
max_byval = max_decl = max_parent = max_elem = max_generic = 0
for i in range(N):
    b = off_td + i * rec
    v8, v12, v16, v20, v24 = struct.unpack_from("<5i", mm, b + 8)
    max_byval = max(max_byval, v8)
    max_decl = max(max_decl, v12)
    max_parent = max(max_parent, v16)
    max_elem = max(max_elem, v20)
    max_generic = max(max_generic, v24)

max_field_type = max(f.type_index for f in m.fields)
max_method_ret = max(mt.return_type for mt in m.methods)
max_param_type = max(p.type_index for p in m.parameters)

print("=== 各类 Il2CppType 索引的最大值 ===")
for label, v in [
    ("typedef.byvalTypeIndex      @8 ", max_byval),
    ("typedef.declaringTypeIndex  @12", max_decl),
    ("typedef.parentIndex         @16", max_parent),
    ("typedef.elementTypeIndex    @20", max_elem),
    ("typedef.genericContainer    @24", max_generic),
    ("field.typeIndex               ", max_field_type),
    ("method.returnType             ", max_method_ret),
    ("parameter.typeIndex          ", max_param_type),
]:
    print(f"  {label} = {v:>12,}")

overall = max(max_byval, max_decl, max_parent, max_elem, max_generic,
              max_field_type, max_method_ret, max_param_type)
print(f"\n  全局最大值 = {overall:,}  → Il2CppType 表至少需要 {overall + 1:,} 项")

# ---------- 2. 候选 Il2CppType 表 ----------
print("\n=== 候选表（size 能被 16/24 整除且项数 > 上界）===")
candidates: list[tuple[str, int, int, int]] = []  # (name, offset, size, stride)
for name, (toff, tsize) in sorted(m.tables.items()):
    if not toff or tsize < 16:
        continue
    for stride in (16, 24, 32):
        n = tsize // stride
        if tsize % stride == 0 and n > overall:
            candidates.append((name, toff, tsize, stride))
            print(f"  {name:<42} off=0x{toff:08X} size={tsize:>10,} "
                  f"stride={stride} → {n:,} 项")

# ---------- 3. 用真值暴力搜索 ----------
print(f"\n=== 读取真值: {DUMP} ===")
base_of: dict[str, str] = {}
with open(DUMP, "r", encoding="utf-8", errors="replace") as fh:
    for line in fh:
        if line.startswith("class ") or line.startswith("enum "):
            mm2 = re.match(r"(?:class|enum)\s+(\S+)\s*:\s*(\S+)", line)
            if mm2:
                base_of[mm2.group(1)] = mm2.group(2)

test_cases: list[tuple[str, str, int]] = []
for name, base in base_of.items():
    t = m.type_by_name(name)
    if t is None or base in ("System.Object",):
        continue
    test_cases.append((name, base, t.index))
print(f"  可用真值对: {len(test_cases):,}（含 Object 父类）")

# 只取父类不是 Object 的，信号更强
strong = [c for c in test_cases if c[1] != "System.Object"][:600]
print(f"  强信号（父类非 Object）: {len(strong):,}")

print("\n=== 暴力搜索 Il2CppType 表与 klassIndex 偏移 ===")
best: list[tuple[float, str, int, int, int, int]] = []
for name, toff, tsize, stride in candidates:
    for koff in (0, 4, 8):
        for pidx_pos in (16, 12, 8, 20, 24):
            hit = miss = bad = 0
            for _tname, base, tidx in strong:
                p = struct.unpack_from("<i", mm, off_td + tidx * rec + pidx_pos)[0]
                if p < 0 or p >= tsize // stride:
                    bad += 1
                    continue
                k = struct.unpack_from("<i", mm, toff + p * stride + koff)[0]
                if not (0 <= k < N):
                    bad += 1
                    continue
                if m.types[k].full_name == base:
                    hit += 1
                else:
                    miss += 1
            total = hit + miss
            if total < 50:
                continue
            acc = hit * 100.0 / total
            best.append((acc, name, stride, koff, pidx_pos, total))

best.sort(reverse=True)
print(f"\n{'准确率':>8}  {'表名':<34}{'步长':>5}{'klass@':>8}{'pidx@':>7}{'可解析':>8}")
for acc, name, stride, koff, pidx_pos, total in best[:12]:
    print(f"{acc:>7.1f}%  {name:<34}{stride:>5}{koff:>8}{pidx_pos:>7}{total:>8}")

m.close()
