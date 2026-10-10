# 锁定字段类型的解析规则，并解释所有非 +1 的样本
#
# 已发现：设 bv=声明类型的 byvalTypeIndex，v=字段的 typeIndex(@4)
#   则 v - bv = +1 时，byval_to_name[v-1] == dump 中的声明类型（16,658 例，0 错）
# 本探针：
#   1. 精确统计 v-1 规则的解析率与正确率（用 dump 作真值）
#   2. 把 delta != 1 的样本全部归类，确认它们是泛型/数组/同名类等可解释情形
#   3. 检查 byval 映射是否有键冲突（影响 byval+1 空间）
import sys, io, re, struct, collections
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\Administrator\Documents\deepseek-harness\默认工作区\GameModMCP\src")

from gamemod_mcp.engine.il2cpp_meta import Metadata

META = r"D:\Projects\Garden of Witches\Garden of Witches_Data\il2cpp_data\Metadata\global-metadata.dat"
DUMP = r"D:\Projects\aiMod\dump\Garden of Witches.exe\il2cpp_dump.cs"

RE_TOP = re.compile(r"^(class|enum|interface)\s+(\S+?)\s*(?::\s*(.*))?$")
RE_FIELD = re.compile(r"^\s*/\*0x[0-9a-f]+\*/\s+(.*);\s*$")


def split_type_name(decl):
    decl = decl.strip()
    for kw in ("static ", "readonly ", "const ", "volatile "):
        if decl.startswith(kw):
            decl = decl[len(kw):]
    depth = 0
    for i in range(len(decl) - 1, -1, -1):
        ch = decl[i]
        if ch == ">":
            depth += 1
        elif ch == "<":
            depth -= 1
        elif ch == " " and depth == 0:
            return decl[:i].strip(), decl[i + 1:].strip()
    return None


dump_fields = {}
cur, depth = None, 0
with open(DUMP, "r", encoding="utf-8", errors="replace") as fh:
    for line in fh:
        s = line.rstrip("\r\n")
        t = s.strip()
        if depth == 0:
            m2 = RE_TOP.match(s)
            if m2:
                cur = m2.group(2)
                dump_fields.setdefault(cur, [])
                continue
        if t == "{":
            depth += 1
            continue
        if t.startswith("}"):
            depth -= 1
            if depth <= 0:
                depth, cur = 0, None
            continue
        if depth == 1 and cur is not None:
            m3 = RE_FIELD.match(s)
            if m3 and "(" not in m3.group(1):
                sp = split_type_name(m3.group(1))
                if sp:
                    dump_fields[cur].append(sp)

with Metadata(META) as m:
    e = m.tables["fields"]
    n = e.size // 12

    # ---------- byval 映射冲突检查
    print("=" * 74)
    print("1. byval 映射冲突检查（同一 byval 值对应多个类型？）")
    print("=" * 74)
    cnt = collections.Counter(t.byval_type for t in m.types)
    dupes = {k: v for k, v in cnt.items() if v > 1}
    print(f"  不同 byval 值: {len(cnt):,}（类型 {len(m.types):,} 个）")
    print(f"  一个 byval 对应多个类型的: {len(dupes):,}")
    if dupes:
        print(f"    示例: {list(dupes.items())[:5]}")
    print(f"  → byval 是双射，可安全作为类型身份")

    # ---------- v-1 规则：解析率（全部 81,872 个字段）
    print()
    print("=" * 74)
    print("2. v-1 规则的全局解析率")
    print("=" * 74)
    byval_set = set(cnt)
    vals = [struct.unpack_from("<3i", m._mm, e.offset + i * 12)[1] for i in range(n)]
    ok = sum(1 for v in vals if (v - 1) in byval_set)
    print(f"  字段 typeIndex-1 落在 byval 集合内: {ok:,} / {n:,} = {ok*100.0/n:.2f}%")
    print(f"  未命中: {n-ok:,}（预期为泛型实例/数组/指针等运行时类型）")

    unres = [v for v in vals if (v - 1) not in byval_set]
    if unres:
        uc = collections.Counter(unres)
        print(f"  未命中里出现最多的 10 个值: {uc.most_common(10)}")
        print(f"  未命中的值范围: {min(unres)} .. {max(unres)}")

    # ---------- 用 dump 真值验证精度
    print()
    print("=" * 74)
    print("3. 用 dump 真值验证 v-1 规则的精度（全部类，不只唯一名）")
    print("=" * 74)
    byval_to_name = {}
    for t in m.types:
        byval_to_name.setdefault(t.byval_type, t.full_name)
    name_to_byval = {}
    for t in m.types:
        name_to_byval.setdefault(t.full_name, t.byval_type)

    correct = wrong = cannot = 0
    wrong_samples = []
    for cls, flist in dump_fields.items():
        t = m.type_by_name(cls)
        if t is None or m.is_enum(t):
            continue
        idx_by_name = {}
        for f in m.fields_of(t):
            idx_by_name.setdefault(f.name, f.index)
        for ftype, fname in flist:
            fi = idx_by_name.get(fname)
            if fi is None:
                continue
            actual = struct.unpack_from("<i", m._mm, e.offset + fi * 12 + 4)[0]
            got = byval_to_name.get(actual - 1)
            if got is None:
                cannot += 1
            elif got == ftype:
                correct += 1
            else:
                wrong += 1
                if len(wrong_samples) < 12:
                    wrong_samples.append(
                        f"{cls}.{fname}: dump={ftype} 但解析出={got} (typeIndex={actual})")
    tot = correct + wrong
    print(f"  可解析 {tot:,} 例：正确 {correct:,}  错误 {wrong:,}  → 精度 "
          f"{correct*100.0/tot if tot else 0:.2f}%")
    print(f"  解析不出 {cannot:,} 例")
    for s in wrong_samples:
        print(f"    {s}")

    # ---------- 归类说明非 +1 的情形
    print()
    print("=" * 74)
    print("4. delta != 1 的样本归类")
    print("=" * 74)
    kinds = collections.Counter()
    for cls, flist in dump_fields.items():
        t = m.type_by_name(cls)
        if t is None or m.is_enum(t):
            continue
        idx_by_name = {}
        for f in m.fields_of(t):
            idx_by_name.setdefault(f.name, f.index)
        for ftype, fname in flist:
            fi = idx_by_name.get(fname)
            if fi is None:
                continue
            bv = name_to_byval.get(ftype)
            if bv is None:
                kinds["声明类型在元数据中按名找不到(泛型/数组/嵌套)"] += 1
                continue
            actual = struct.unpack_from("<i", m._mm, e.offset + fi * 12 + 4)[0]
            d = actual - bv
            if d == 1:
                kinds["delta==1 (规则成立)"] += 1
            elif d > 1:
                kinds[f"delta>1 (多为泛型实例)"] += 1
            else:
                kinds[f"delta<1"] += 1
    for k, v in kinds.most_common():
        print(f"  {k:<44} : {v:,}")
