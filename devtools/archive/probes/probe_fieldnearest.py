# 决定性实验：真实类型是否为「ti 之前最近的 byval 键」
#
# 已知事实：
#   · 字段 typeIndex(ti) 与 byval 键两个集合**交集恰好为 0**（完全互补的两条轨道）
#   · delta = ti - 真实byval 恒为正，且 +1 占 65.25%
# 推论假设：Il2CppType 里除「byval 本体」外还有若干派生槽位（数组/泛型实例/
# byref 等），字段指向其中某个派生槽；而**本体**总是它前面最近的那个 byval 键。
# 若成立，则规则为：type = byval_to_name[小于 ti 的最大 byval 键]。
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
    name_to_byval = {}
    for t in m.types:
        name_to_byval.setdefault(t.nested_name, t.byval_type)
        name_to_byval.setdefault(t.full_name, t.byval_type)

    dump = parse_dump_fields(DUMP)

    # 收集可比对样本：(ti, 真值类型名)
    samples = []
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
            samples.append((m.fields[fi].type_index, ftype))

    print("=" * 80)
    print("1. delta 是否恒为正（有无负 delta / 零 delta）")
    print("=" * 80)
    ds = collections.Counter()
    for ti, ftype in samples:
        tv = name_to_byval.get(ftype)
        if tv is None:
            continue
        ds[ti - tv] += 1
    neg = sum(c for d, c in ds.items() if d < 0)
    zero = ds.get(0, 0)
    pos = sum(c for d, c in ds.items() if d > 0)
    print(f"  delta<0 : {neg:,}     delta==0 : {zero:,}     delta>0 : {pos:,}")
    print(f"  最小 delta = {min(ds)}   最大 delta = {max(ds)}")

    print()
    print("=" * 80)
    print("2. 假设检验：type = 小于 ti 的最大 byval 键")
    print("=" * 80)
    hit = wrong = oob = 0
    dbg = collections.Counter()
    wrong_samples = []
    for ti, ftype in samples:
        p = bisect.bisect_left(keys, ti) - 1      # 小于 ti 的最大键的下标
        if p < 0:
            oob += 1
            continue
        got = m.types[byval[keys[p]]].nested_name
        if ftype not in name_to_byval:
            # 真值类型不在元数据里（数组/泛型实例）——不计入精度，只记录命中形状
            if got == ftype:
                hit += 1
            continue
        if got == ftype:
            hit += 1
            dbg[ti - name_to_byval[ftype]] += 1
        else:
            wrong += 1
            if len(wrong_samples) < 12:
                wrong_samples.append(
                    f"{ftype}: ti={ti} 真byval={name_to_byval[ftype]} "
                    f"最近前键={keys[p]} 得 {got}"
                )
    tot = hit + wrong
    print(f"  可判定样本 {tot:,}：正确 {hit:,}  错误 {wrong:,}  越界 {oob:,}")
    print(f"  → 精度 {hit*100.0/max(1,tot):.2f}%")
    print(f"  命中样本的 delta 分布（证明每个 delta 都成立，不只是 +1）：")
    for d, c in sorted(dbg.items()):
        print(f"     delta={d:+4d}  正确 {c:>7,}")

    for s in wrong_samples:
        print(f"     真错: {s}")
