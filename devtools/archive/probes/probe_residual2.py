# 残差决定性探针：对未解析字段，反推 v 与「声明类型 byval」的真实差，找第二规则
#
# 已确证：v-1 ∈ byval 集合时，解析出的类型名与 dump 声明类型 100% 一致（17,795 例 0 错）
# 覆盖 36,546/81,872 = 44.64%
# 本探针：对未解析的字段，用 dump 真值给出真实 byval，看 v 与 byval 的差分布，
#         判断是否存在第二个可判定的规则（例如按类型类别分别偏移）。
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
    td = m.tables["typeDefinitions"]
    T = len(m.types)
    fe = m.tables["fields"]

    byval_set = set(t.byval_type for t in m.types)
    # 名字 -> byval（嵌套感知，取两种写法）
    name_to_byval = {}
    for t in m.types:
        name_to_byval.setdefault(t.full_name, t.byval_type)
        name_to_byval.setdefault(t.name, t.byval_type)
    byval_to_td = {t.byval_type: t.index for t in m.types}

    def nested_full(i):
        parts, seen, cur_i = [], set(), i
        while 0 <= cur_i < T and cur_i not in seen:
            seen.add(cur_i)
            parts.append(m.types[cur_i].name)
            d = struct.unpack_from("<i", m._mm, td.offset + cur_i * 88 + 12)[0]
            nxt = byval_to_td.get(d, -1)
            if nxt < 0:
                break
            cur_i = nxt
        parts.reverse()
        outer_i = cur_i if 0 <= cur_i < T else i
        ns = m.types[outer_i].namespace
        out = ".".join(parts)
        if ns and not out.startswith(ns + "."):
            out = ns + "." + out
        return out

    byval_to_name = {}
    for i in range(T):
        byval_to_name.setdefault(m.types[i].byval_type, nested_full(i))
    for i in range(T):
        byval_to_name.setdefault(m.types[i].byval_type, m.types[i].full_name)

    # ---------- 残差字段的真实 delta
    print("=" * 78)
    print("残差字段（v-1 不在 byval 集合）的 dump 真值 delta = v - byval(声明类型)")
    print("=" * 78)
    deltas = collections.Counter()
    by_kind = collections.defaultdict(collections.Counter)
    total_resid = 0
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
            if (v - 1) in byval_set:
                continue
            total_resid += 1
            bv = name_to_byval.get(ftype)
            if ftype.endswith("[]"):
                kind = "数组"
            elif "<" in ftype:
                kind = "泛型实例"
            elif "*" in ftype or "&" in ftype:
                kind = "指针/引用"
            else:
                kind = "普通类型"
            if bv is None:
                by_kind[kind]["(声明类型无 byval)"] += 1
                continue
            d = v - bv
            deltas[d] += 1
            by_kind[kind][d] += 1

    print(f"  残差字段 {total_resid:,}")
    print(f"  可用 dump 真值算出 delta 的 {sum(deltas.values()):,}")
    print(f"  总体 delta 分布（前 15）: {deltas.most_common(15)}")
    print()
    print("  按类别看 delta 分布:")
    for kind, c in by_kind.items():
        print(f"    {kind:<10} {dict(c.most_common(8))}")

    # ---------- 检验「残差字段的 v 是否等于 其它类型的 byval」
    print()
    print("=" * 78)
    print("检查：残差字段的 (v-1) 是否能在 byval 空间之外被解释")
    print("=" * 78)
    # 若 v-1 落在 [min_byval, max_byval] 之外，说明是独立的索引段
    resid_vals = []
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
                resid_vals.append(v - 1)
    print(f"  残差 (v-1) 范围: {min(resid_vals):,} .. {max(resid_vals):,}")
    print(f"  byval 范围       : {min(byval_set):,} .. {max(byval_set):,}")
    inrange = sum(1 for x in resid_vals if min(byval_set) <= x <= max(byval_set))
    print(f"  残差落在 byval 数值区间内的: {inrange:,} / {len(resid_vals):,}")
    print("  → 若大量残差落在区间内却不在集合里，说明 types 数组存在重复条目，")
    print("     即同一语义类型有多个 Il2CppType，只有 byval 那个在元数据里可见。")

    # ---------- 抽样：残差与已解析的对照
    print()
    print("=" * 78)
    print("抽样对照")
    print("=" * 78)
    shown = 0
    for cls, flist in dump_fields.items():
        if shown >= 12:
            break
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
            if (v - 1) in byval_set:
                continue
            bv = name_to_byval.get(ftype)
            print(f"  {cls}.{fname}")
            print(f"      dump={ftype}  声明类型byval={bv}  v={v}  v-1={v-1} "
                  f"(in byval={v-1 in byval_set})")
            shown += 1
