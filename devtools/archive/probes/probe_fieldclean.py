# 决定性排查：用「唯一名类」的干净样本，测定 field.typeIndex 的解析规则
#
# 已确立的硬事实：
#   typedef.byvalTypeIndex(@8) 构成的键集合，与 field.typeIndex(@4) **交集恰为 0**
#   而 method.returnType(@8) 命中同一集合 96.64%、parameter.typeIndex(@8) 82.70%
#   → 字段用的是另一个索引空间，或存在固定平移
#
# 本探针排除「同名类配对错误」的干扰：只用元数据中 full_name 唯一的类，
# 这样 dump 的类 ↔ 元数据的类一定是一对一，字段配对必然正确。
# 然后统计真实 delta，并对 delta 扫描给出**准确率**（而非命中率）。
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

    # ---------- 唯一名类集合
    name_count = collections.Counter(t.full_name for t in m.types)
    unique_names = {n for n, c in name_count.items() if c == 1}
    print(f"元数据类型 {len(m.types):,} 个，其中 full_name 唯一的 {len(unique_names):,} 个")

    # byval -> fullname  与  fullname -> byval
    byval_to_name: dict[int, str] = {}
    for t in m.types:
        byval_to_name.setdefault(t.byval_type, t.full_name)
    name_to_byval: dict[str, int] = {}
    for t in m.types:
        name_to_byval.setdefault(t.full_name, t.byval_type)

    # ---------- 干净配对：只取唯一名类
    deltas = collections.Counter()
    pairs = []          # (field_index, expected_byval, actual_typeindex, class, field)
    for cls, flist in dump_fields.items():
        if cls not in unique_names:
            continue
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
                continue
            actual = struct.unpack_from("<i", m._mm, e.offset + fi * 12 + 4)[0]
            deltas[actual - bv] += 1
            pairs.append((fi, bv, actual, cls, fname, ftype))

    print(f"干净配对字段数 = {len(pairs):,}")
    print(f"真实 delta 分布（前 12）: {deltas.most_common(12)}")
    top_delta, top_n = deltas.most_common(1)[0]
    print(f"→ 主导 delta = {top_delta:+d}，占 {top_n*100.0/len(pairs):.2f}%")

    # ---------- 对每个 delta 计算「解析出的类型名 == dump 声明类型」的准确率
    print()
    print("=" * 74)
    print("按 delta 平移后，用 byval 反查表解析字段类型，与 dump 声明类型比对的准确率")
    print("=" * 74)
    for delta in range(-3, 4):
        ok = bad = miss = 0
        for fi, bv, actual, cls, fname, ftype in pairs:
            nm = byval_to_name.get(actual + delta)
            if nm is None:
                miss += 1
            elif nm == ftype:
                ok += 1
            else:
                bad += 1
        tot = len(pairs)
        print(f"  delta={delta:+d}: 正确 {ok:>6,}  错误 {bad:>6,}  解析不出 {miss:>6,}"
              f"   → 准确率 {ok*100.0/tot:6.2f}%")

    # ---------- 抽样看 delta=主导值时解析出的名字对不对
    print()
    print("=== 抽样对比（直接看名字是否吻合）===")
    shown = 0
    for fi, bv, actual, cls, fname, ftype in pairs:
        if shown >= 15:
            break
        got0 = byval_to_name.get(actual)
        got1 = byval_to_name.get(actual + top_delta)
        print(f"  {cls}.{fname}")
        print(f"      dump声明={ftype}  byval={bv}  @4={actual}")
        print(f"      delta={top_delta:+d} 解析={got1}   (delta=0 解析={got0})")
        shown += 1
