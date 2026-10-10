# 残差刻画：v-1 规则未覆盖的 45,326 个字段究竟指向什么
#
# 已确证：field.typeIndex - 1 -> typedef（byval 反查）精度 100%，覆盖 44.64%
# 本探针用「方法/参数/父类已引用的 Il2CppType 索引集合」作为运行时类型空间的近似，
# 判断残差字段是否只是「有合法 Il2CppType 但没有 typedef」的数组/泛型/基元类型。
import sys, io, struct, collections
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\Administrator\Documents\deepseek-harness\默认工作区\GameModMCP\src")

from gamemod_mcp.engine.il2cpp_meta import Metadata

META = r"D:\Projects\Garden of Witches\Garden of Witches_Data\il2cpp_data\Metadata\global-metadata.dat"

with Metadata(META) as m:
    td = m.tables["typeDefinitions"]
    T = len(m.types)
    me = m.tables["methods"]
    MN = me.size // 36
    pe = m.tables["parameters"]
    PN = pe.size // 12
    fe = m.tables["fields"]
    FN = fe.size // 12

    byval_set = set(t.byval_type for t in m.types)

    # 运行时 Il2CppType 空间的近似：方法返回类型、参数类型、父类、元素类型引用的全部索引
    tp = set()
    for i in range(MN):
        tp.add(struct.unpack_from("<6i", m._mm, me.offset + i * 36)[2])
    for i in range(PN):
        tp.add(struct.unpack_from("<3i", m._mm, pe.offset + i * 12)[2])
    for i in range(T):
        base = td.offset + i * 88
        tp.add(struct.unpack_from("<i", m._mm, base + 16)[0])
        tp.add(struct.unpack_from("<i", m._mm, base + 20)[0])
    tp = {v for v in tp if v >= 0}

    print(f"byval 键集合           : {len(byval_set):,}   范围 {min(byval_set)} .. {max(byval_set)}")
    print(f"近似运行时类型索引集合 TP: {len(tp):,}   范围 {min(tp)} .. {max(tp)}")
    print(f"  TP 与 byval 交集      : {len(tp & byval_set):,}")
    print()

    fvals = [struct.unpack_from("<3i", m._mm, fe.offset + i * 12)[1] for i in range(FN)]

    in_byval = sum(1 for v in fvals if (v - 1) in byval_set)
    in_tp = sum(1 for v in fvals if (v - 1) in tp)
    in_both = sum(1 for v in fvals if (v - 1) in (tp | byval_set))
    print("=" * 74)
    print("字段 typeIndex-1 的归属")
    print("=" * 74)
    print(f"  落在 byval 键集合（可直接给出 typedef 类型名）: {in_byval:>7,} "
          f"({in_byval*100.0/FN:6.2f}%)")
    print(f"  落在近似运行时类型集合 TP                    : {in_tp:>7,} "
          f"({in_tp*100.0/FN:6.2f}%)")
    print(f"  落在 TP ∪ byval                             : {in_both:>7,} "
          f"({in_both*100.0/FN:6.2f}%)")
    print(f"  两者都不在（无 typedef 的运行时类型）          : {FN-in_both:>7,} "
          f"({(FN-in_both)*100.0/FN:6.2f}%)")

    # ---------- 残差字段的 dump 类型名分布
    print()
    print("=" * 74)
    print("残差字段按 dump 声明类型名统计（前 25，说明缺失的是哪些类型）")
    print("=" * 74)
    import re
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

    resid = collections.Counter()
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
            v = struct.unpack_from("<i", m._mm, fe.offset + fi * 12 + 4)[0]
            if (v - 1) not in byval_set:
                resid[ftype] += 1
    tot_resid = sum(resid.values())
    print(f"  残差字段总数 {tot_resid:,}")
    for nm, c in resid.most_common(25):
        print(f"    {nm:<52} {c:>6,}")

    # ---------- 检查 UserData_Service 为何取不到
    print()
    print("=" * 74)
    print("检查 type_by_name('UserData_Service')")
    print("=" * 74)
    t = m.type_by_name("UserData_Service")
    print(f"  type_by_name -> {t}")
    hits = m.search_types("UserData_Service", limit=5)
    print(f"  search_types -> {[(x.full_name, x.index) for x in hits]}")
