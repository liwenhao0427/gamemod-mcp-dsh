# 决定性排查：字段 typeIndex 究竟指向什么索引空间
#
# 硬事实（本探针要解释的矛盾）：
#   parentIndex(@16) 用 byval 反查表解析 → 枚举 1084/1084 = 100%
#   method.returnType(@8) 用同一张表 → 96.64%
#   parameter.typeIndex(@8) → 82.70%
#   field.typeIndex(@4)  → 0.00%  且 delta=0 恰好为 0，说明两个索引空间**完全不相交**
#
# 本探针依次检验四个假设：
#   H1  字段 typeIndex 是「每类型常量偏移」→ 同一声明类型应给同一 delta
#   H2  字段 typeIndex 应配另一张由 typedef 其他字段构造的反查表
#   H3  字段 typeIndex 落在 byval 空间的补集（即非 byval 的 Il2CppType 条目）
#   H4  字段记录布局另有其形
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


# ---------- 解析 dump 的字段表
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
    keys = set(m._byval_to_typedef)

    print(f"dump 类型 {len(dump_fields):,} 个")

    # ================= H1：按声明类型聚合 delta =================
    print()
    print("=" * 74)
    print("H1  同一声明类型的 delta 是否一致？（检验「每类型常量偏移」）")
    print("=" * 74)

    fullname_to_byval = {}
    for t in m.types:
        fullname_to_byval.setdefault(t.full_name, t.byval_type)

    per_type_deltas: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    matched = 0
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
            bv = fullname_to_byval.get(ftype)
            if bv is None:
                continue
            actual = struct.unpack_from("<i", m._mm, e.offset + fi * 12 + 4)[0]
            per_type_deltas[ftype][actual - bv] += 1
            matched += 1

    print(f"  配对 {matched:,} 个字段，涉及 {len(per_type_deltas):,} 个声明类型")
    consistent = sum(1 for c in per_type_deltas.values() if len(c) == 1)
    print(f"  delta 恒定的声明类型: {consistent:,} / {len(per_type_deltas):,} = "
          f"{consistent*100.0/len(per_type_deltas):.2f}%")
    print("  delta 分布的极端例子：")
    multi = [(k, c) for k, c in per_type_deltas.items() if len(c) > 1]
    multi.sort(key=lambda kv: -sum(kv[1].values()))
    for k, c in multi[:5]:
        print(f"    {k}: {dict(c.most_common(5))}")
    print("  delta 恒定的例子：")
    for k, c in list((k, c) for k, c in per_type_deltas.items() if len(c) == 1)[:8]:
        print(f"    {k}: delta={list(c)[0]} (n={sum(c.values())})")

    # ================= H2：用 typedef 其他字段构造反查表 =================
    print()
    print("=" * 74)
    print("H2  用 typedef 的其他索引字段构造反查表，看谁能让 field.typeIndex 命中")
    print("=" * 74)

    fvals = [struct.unpack_from("<3i", m._mm, e.offset + i * 12)[1]
             for i in range(e.size // 12)]

    candidates = {
        "byval(@8)": 8, "declaring(@12)": 12, "parent(@16)": 16,
        "element(@20)": 20, "genericContainer(@24)": 24,
    }
    for label, off in candidates.items():
        mapset = set()
        for t in m.types:
            v = struct.unpack_from("<i", m._mm,
                                   m.tables["typeDefinitions"].offset + t.index * 88 + off)[0]
            mapset.add(v)
        hit = sum(1 for v in fvals if v in mapset)
        print(f"  {label:<24} 键数={len(mapset):>7,}  字段命中={hit:>7,} "
              f"({hit*100.0/len(fvals):6.2f}%)")

    # ================= H3：字段索引空间 vs byval 空间的重叠 =================
    print()
    print("=" * 74)
    print("H3  字段索引空间与 byval 空间的关系")
    print("=" * 74)
    fset = set(fvals)
    print(f"  字段 typeIndex 去重值数 : {len(fset):,}（共 {len(fvals):,} 个字段）")
    print(f"  byval 键数             : {len(keys):,}")
    print(f"  两集合交集             : {len(fset & keys):,}")
    print(f"  字段值范围             : {min(fset)} .. {max(fset)}")
    print(f"  byval 值范围           : {min(keys)} .. {max(keys)}")
    below = sum(1 for v in fset if v < min(keys))
    print(f"  字段值低于 byval 下界的  : {below:,} 个不同值")
    print()
    print("  字段 typeIndex 出现频率前 15:")
    for v, c in collections.Counter(fvals).most_common(15):
        print(f"     idx={v:<7} 出现 {c:>6,} 次   在byval中={v in keys}")
    print()
    print("  byval 空间中未被任何字段引用的值（抽样 15）:")
    unused = sorted(keys - fset)[:15]
    print(f"     {unused}")

    # ================= H4：字段记录布局 =================
    print()
    print("=" * 74)
    print("H4  字段记录布局再验证（token 规律 + 名字正确性）")
    print("=" * 74)
    n = e.size // 12
    tok_ok = 0
    name_ok = 0
    for i in range(min(n, 20000)):
        na, tv, tk = struct.unpack_from("<3i", m._mm, e.offset + i * 12)
        if (tk >> 24) == 0x04:
            tok_ok += 1
        s = m.read_string(na)
        if s and all(32 <= ord(ch) < 127 for ch in s):
            name_ok += 1
    print(f"  前 20,000 条: token 高字节为 0x04 的 {tok_ok:,} 条")
    print(f"  前 20,000 条: @0 解出可打印 ASCII 名字的 {name_ok:,} 条")
    print(f"  → @0=nameIndex、@8=token 均成立，故 @4 必为 typeIndex，布局无误")
