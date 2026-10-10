# 决定性排查：字段 typeIndex 与 byval 索引空间的关系
#
# 已知事实：
#   方法 returnType(@8) 命中 byval 键集合 96.64%
#   方法 declaringType(@4) 命中 4.90%
#   字段 @4 命中 0.00%   ← 而 73,769 个字段的 @4 数值就落在 byval 区间内
# 两个集合数值区间高度重合却零交集，这只可能是「存在一个固定的整数偏移」。
# 本探针：① 用 dump 真值算偏移分布 ② 全局扫描偏移 ③ 奇偶性分析
import sys, io, re, struct, collections
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\Administrator\Documents\deepseek-harness\默认工作区\GameModMCP\src")

from gamemod_mcp.engine.il2cpp_meta import Metadata

META = r"D:\Projects\Garden of Witches\Garden of Witches_Data\il2cpp_data\Metadata\global-metadata.dat"
DUMP = r"D:\Projects\aiMod\dump\Garden of Witches.exe\il2cpp_dump.cs"

with Metadata(META) as m:
    keys = set(m._byval_to_typedef)
    e = m.tables["fields"]
    n = e.size // 12

    # ---------- 1. 全局偏移扫描
    print("=" * 74)
    print("1. 字段 @4 全局偏移扫描（+delta 后命中 byval 键集合的比例）")
    print("=" * 74)
    vals = [struct.unpack_from("<3i", m._mm, e.offset + i * 12)[1] for i in range(n)]
    for delta in range(-4, 5):
        hit = sum(1 for v in vals if (v + delta) in keys)
        print(f"  delta={delta:+d} : {hit:>7,} / {n:,} = {hit*100.0/n:6.2f}%")

    # ---------- 2. 奇偶性
    print()
    print("=" * 74)
    print("2. 奇偶性分析")
    print("=" * 74)
    odd_keys = sum(1 for k in keys if k % 2 == 1)
    odd_vals = sum(1 for v in vals if v % 2 == 1)
    print(f"  byval 键: 奇数 {odd_keys:,} / {len(keys):,}")
    print(f"  字段 @4 : 奇数 {odd_vals:,} / {n:,}")

    # ---------- 3. 用 dump 真值算偏移
    print()
    print("=" * 74)
    print("3. 用 dump 真值计算偏移（字段声明类型 -> 该类型的 byval -> 与 @4 的差）")
    print("=" * 74)

    # byval -> typedef 反查
    byval_of_typeidx = {i: t.byval_type for i, t in enumerate(m.types)}
    fullname_to_byval: dict[str, int] = {}
    for t in m.types:
        fullname_to_byval.setdefault(t.full_name, t.byval_type)

    RE_TOP = re.compile(r"^(class|enum|interface)\s+(\S+?)\s*(?::\s*(.*))?$")
    RE_FIELD = re.compile(r"^\s*/\*0x[0-9a-f]+\*/\s+(.*);\s*$")


    def split_type_name(decl: str) -> tuple[str, str] | None:
        """把 'System.Byte[] NullableFlags' 拆成 (类型, 名字)，尖括号内空格不算分隔。"""
        decl = decl.replace("static ", "").replace("readonly ", "").strip()
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


    # 收集 dump 中的 (类全名, 字段名, 声明类型)
    dump_fields: dict[str, list[tuple[str, str]]] = {}
    cur = None
    depth = 0
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
                    depth = 0
                    cur = None
                continue
            if depth == 1 and cur is not None:
                m3 = RE_FIELD.match(s)
                if m3 and "(" not in m3.group(1):
                    sp = split_type_name(m3.group(1))
                    if sp:
                        dump_fields[cur].append(sp)

    print(f"  从 dump 提取到 {len(dump_fields):,} 个类型的字段表")

    deltas = collections.Counter()
    matched = 0
    samples = []
    for cls, flist in dump_fields.items():
        t = m.type_by_name(cls)
        if t is None or m.is_enum(t):
            continue
        # 按名字建字段索引
        idx_by_name: dict[str, int] = {}
        for f in m.fields_of(t):
            idx_by_name.setdefault(f.name, f.index)
        for ftype, fname in flist:
            fi = idx_by_name.get(fname)
            if fi is None:
                continue
            bv = fullname_to_byval.get(ftype)
            if bv is None:
                continue  # 泛型/数组等，跳过
            actual = struct.unpack_from("<i", m._mm, e.offset + fi * 12 + 4)[0]
            d = actual - bv
            deltas[d] += 1
            matched += 1
            if len(samples) < 10:
                samples.append(f"{cls}.{fname}: 声明={ftype} byval={bv} @4={actual} delta={d}")

    print(f"  成功配对 {matched:,} 个字段（声明类型在元数据中可精确找到）")
    print(f"  偏移分布（前 10）: {deltas.most_common(10)}")
    print()
    print("  样本:")
    for s in samples:
        print(f"    {s}")

    # ---------- 4. 对照：方法 returnType 的偏移
    print()
    print("=" * 74)
    print("4. 对照：方法 returnType(@8) 的偏移分布")
    print("=" * 74)
    me = m.tables["methods"]
    mn = me.size // 36
    mvals = [struct.unpack_from("<6i", m._mm, me.offset + i * 36)[2] for i in range(mn)]
    for delta in range(-2, 3):
        hit = sum(1 for v in mvals if (v + delta) in keys)
        print(f"  delta={delta:+d} : {hit:>7,} / {mn:,} = {hit*100.0/mn:6.2f}%")

    # ---------- 5. 对照：参数 typeIndex(@8)
    print()
    print("=" * 74)
    print("5. 对照：参数 @8 的偏移分布")
    print("=" * 74)
    pe = m.tables["parameters"]
    pn = pe.size // 12
    pvals = [struct.unpack_from("<3i", m._mm, pe.offset + i * 12)[2] for i in range(pn)]
    for delta in range(-2, 3):
        hit = sum(1 for v in pvals if (v + delta) in keys)
        print(f"  delta={delta:+d} : {hit:>7,} / {pn:,} = {hit*100.0/pn:6.2f}%")
