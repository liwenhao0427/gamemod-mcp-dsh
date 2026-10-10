# 重新测定字段 typeIndex 的真实规则
#
# 推翻了旧结论：残差里存在普通类字段（如 UnityEngine.TextAsset，
# 字段 typeIndex=35060，其 byval=35057，delta=-3 而非 -1）。
# 旧结论「残差全是运行时数组/泛型」是错的，必须用 dump 真值重新测定。
#
# 本探针用 dump 真值反推每个字段的真实 byval，再统计 delta 分布，
# 判断是否存在可静态复现的确定性规则。
import sys, io, re, bisect, collections
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


def parse_dump_fields(path):
    out = {}
    cur, depth = None, 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            s = line.rstrip("\r\n")
            t = s.strip()
            if depth == 0:
                m2 = RE_TOP.match(s)
                if m2:
                    cur = m2.group(2)
                    out.setdefault(cur, [])
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
                        out[cur].append(sp)
    return out


with Metadata(META) as m:
    byval = m._byval_to_typedef
    keys = sorted(byval)
    # 名字 -> byval（归一化：去掉前面的 "Namespace." 前缀也能匹配）
    name_to_byval = {}
    for t in m.types:
        name_to_byval.setdefault(t.nested_name, t.byval_type)
        name_to_byval.setdefault(t.full_name, t.byval_type)

    print("=" * 78)
    print("1. 用 dump 真值反推 delta 分布（只统计能唯一定位的字段）")
    print("=" * 78)
    dump = parse_dump_fields(DUMP)

    deltas = collections.Counter()
    total = resolved = 0
    unresolved_samples = []

    for cls, flist in dump.items():
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
            total += 1
            ti = m.fields[fi].type_index
            tv = name_to_byval.get(ftype)
            if tv is None:
                if len(unresolved_samples) < 12:
                    unresolved_samples.append(f"{cls}.{fname}: dump类型 {ftype} 不在元数据")
                continue
            resolved += 1
            deltas[ti - tv] += 1

    print(f"  可比对字段 {total:,}，其中 dump 类型能在元数据中定位的 {resolved:,}")
    print(f"  delta 分布（字段typeIndex - 真实类型byval）：")
    for d, c in deltas.most_common(14):
        print(f"     delta={d:+5d}  {c:>7,}  ({c*100.0/resolved:5.2f}%)")

    print()
    print("=" * 78)
    print("2. 关键检验：delta 是否等于「两集合间的排名差」")
    print("=" * 78)
    # 假设：字段 typeIndex 与 byval 是两个交错的道（同一空间的两条轨道）。
    # 若如此，delta 应等于两者之间「被另一道占用的槽位数」。
    fields_set = {f.type_index for f in m.fields}
    byval_set = set(keys)
    print(f"  字段 typeIndex 去重后 {len(fields_set):,} 个值")
    print(f"  byval 键 {len(byval_set):,} 个值")
    print(f"  两者交集 = {len(fields_set & byval_set):,}")
    u = fields_set | byval_set
    print(f"  并集 {len(u):,}，占区间 {min(u)}..{max(u)} 的"
          f" {(len(u))*100.0/(max(u)-min(u)+1):.2f}%")
    print("  -> 交集为 0 说明两条道完全互补（像奇偶分道）")

    print()
    print("=" * 78)
    print("3. 检验：字段 typeIndex 与 byval 是否满足「并集内排名」关系")
    print("=" * 78)
    # 若两条道互补地填满同一区间，则把并集排序后，每个 byval 键有一个排名，
    # 每个字段值也有一个排名。真实映射可能就是排名。
    ys = sorted(byval_set)
    fs = sorted(fields_set)
    print(f"  byval   排名区间 0..{len(ys)-1}")
    print(f"  字段值  排名区间 0..{len(fs)-1}")
    sample = sorted(fields_set)[:5]
    for v in sample:
        print(f"    字段值 {v} 排名 {bisect.bisect_left(fs, v)}"
              f" | 该排名处的 byval 键 = {ys[bisect.bisect_left(fs, v)] if bisect.bisect_left(fs, v) < len(ys) else 'N/A'}")

    print()
    print("=" * 78)
    print("4. 直接检验「同一排名」假设的真实精度")
    print("=" * 78)
    hit = wrong = miss = 0
    wrong_samples = []
    for cls, flist in dump.items():
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
            ti = m.fields[fi].type_index
            r = bisect.bisect_left(fs, ti)
            if r >= len(ys):
                miss += 1
                continue
            got = m.types[byval[ys[r]]].nested_name
            if got == ftype:
                hit += 1
            else:
                wrong += 1
                if len(wrong_samples) < 8:
                    wrong_samples.append(f"{cls}.{fname}: 真值 {ftype} 排名法得 {got}")
    print(f"  排名法：正确 {hit:,}  错误 {wrong:,}  越界 {miss:,}"
          f"  → 精度 {hit*100.0/max(1,hit+wrong):.2f}%")
    for s in wrong_samples:
        print(f"     真错: {s}")

    if unresolved_samples:
        print()
        print("  dump 类型名在元数据中找不到的样例：")
        for s in unresolved_samples:
            print(f"     {s}")
