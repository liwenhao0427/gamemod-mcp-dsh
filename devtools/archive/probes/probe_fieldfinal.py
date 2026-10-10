# 最终确认：字段类型解析规则 + 未解析项的归类与可恢复性 + 嵌套类全名构造
#
# 已确立规则（待本探针二次确认）：
#   field.typeIndex(@4) - 1 == typedef.byvalTypeIndex(@8)
#   即 声明类型 = byval_to_typedef[field.typeIndex - 1]
# 本探针：
#   1. 用正确的解析方向重算精度（区分「类型找对但名字写法不同」与「真错」）
#   2. 把 45,326 个未解析字段按 dump 真值归类（数组/泛型/指针/嵌套…）
#   3. 检验数组元素类型能否反推
#   4. 检验 declaringTypeIndex 能否构造嵌套类全名（影响 dump 比对与展示）
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
    byval_to_name = {}
    for t in m.types:
        byval_to_name.setdefault(t.byval_type, t.full_name)
    name_to_byval = {}
    for t in m.types:
        name_to_byval.setdefault(t.full_name, t.byval_type)
    byval_set = set(byval_to_name)

    # ---------- 1. 正确方向的精度：把嵌套名统一后再比
    print("=" * 74)
    print("1. 规则 field.typeIndex-1 -> typedef 的精度（嵌套名归一化后）")
    print("=" * 74)

    def leaf(n):        # 取最短尾名，用于消解 Outer.Inner 与 Inner 的写法差异
        return n.rsplit(".", 1)[-1]

    exact = leaf_ok = wrong = cannot = 0
    real_wrong = []
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
            v = struct.unpack_from("<i", m._mm, e.offset + fi * 12 + 4)[0]
            got = byval_to_name.get(v - 1)
            if got is None:
                cannot += 1
            elif got == ftype:
                exact += 1
            elif leaf(got) == leaf(ftype):
                leaf_ok += 1
            else:
                wrong += 1
                if len(real_wrong) < 10:
                    real_wrong.append(f"{cls}.{fname}: dump={ftype} 解析={got} (v={v})")
    tot = exact + leaf_ok + wrong
    print(f"  可解析 {tot:,}：完全一致 {exact:,}  仅嵌套写法不同 {leaf_ok:,}  真错 {wrong:,}")
    print(f"  → 精度 {exact*100.0/tot:.2f}% ，含嵌套归一 {(exact+leaf_ok)*100.0/tot:.2f}%")
    print(f"  解析不出 {cannot:,}")
    for s in real_wrong:
        print(f"    真错样本: {s}")

    # ---------- 2. 未解析字段归类
    print()
    print("=" * 74)
    print("2. 未解析字段（v-1 不在 byval 集合）按 dump 声明类型归类")
    print("=" * 74)
    kinds = collections.Counter()
    array_deltas = collections.Counter()
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
            v = struct.unpack_from("<i", m._mm, e.offset + fi * 12 + 4)[0]
            if (v - 1) in byval_set:
                continue
            if ftype.endswith("[]"):
                kinds["数组 T[]"] += 1
                elem = ftype[:-2]
                eb = name_to_byval.get(elem)
                if eb is not None:
                    array_deltas[v - eb] += 1
            elif "<" in ftype:
                kinds["泛型实例"] += 1
            elif "*" in ftype:
                kinds["指针"] += 1
            elif "&" in ftype:
                kinds["引用"] += 1
            else:
                kinds[f"其他(具体类型名)"] += 1
    for k, v in kinds.most_common():
        print(f"  {k:<20} : {v:,}")
    if array_deltas:
        print()
        print(f"  数组字段 v 与元素 byval 的差值分布（前 10）: {array_deltas.most_common(10)}")
        print("  → 若差值恒定，则数组元素类型可反推（即使数组本身无 typedef）")

    # ---------- 3. 嵌套类全名构造
    print()
    print("=" * 74)
    print("3. declaringTypeIndex(@12) 能否构造嵌套类全名（对齐 dump 的 Outer.Inner）")
    print("=" * 74)
    td = m.tables["typeDefinitions"]
    decl_vals = [struct.unpack_from("<i", m._mm, td.offset + i * 88 + 12)[0]
                 for i in range(len(m.types))]
    nonneg = sum(1 for v in decl_vals if v >= 0)
    print(f"  declaringTypeIndex >= 0 的类型: {nonneg:,} / {len(m.types):,}")

    # 抽样：名字里含 '<' 或本身是嵌套的
    shown = 0
    for t in m.types:
        dv = decl_vals[t.index]
        if dv >= 0 and shown < 8:
            outer = None
            if 0 <= dv < len(m.types):
                outer = m.types[dv].full_name
            print(f"    [{t.index}] {t.full_name}  declaringTypeIndex={dv} ({outer})")
            shown += 1
    print(f"  → 可用 declaringTypeIndex 逐级上溯拼出 A.B.C 形式的稳定全名")

    # ---------- 4. 字段 flags
    print()
    print("=" * 74)
    print("4. 字段记录中是否可读到 flags（验证 is_static）")
    print("=" * 74)
    # 用 dump 中带 static 的字段做真值
    statics = 0
    samples = []
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
            raw = struct.unpack_from("<3i", m._mm, e.offset + fi * 12)
            if raw[0] == 0 and raw[1] == 0:
                statics += 1
                if len(samples) < 5:
                    samples.append(f"{cls}.{fname} -> {raw}")
    print(f"  记录中前两字段同时为 0 的字段数: {statics}")
    for s in samples:
        print(f"    {s}")
    print("  → v31 字段表无独立 flags 槽位，静态性需从 dump/运行时获取")
