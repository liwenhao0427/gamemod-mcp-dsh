# 收尾探针：嵌套类全名构造 + 字段类型解析的最终覆盖率
#
# 结论基础：
#   field.typeIndex - 1 ∈ 按 byvalTypeIndex 建的反查表 → 声明类型（delta=-1，0 错）
#   其他 delta 的命中都是噪声（delta=-3 有 11,508 错）
# 本探针要解决最后一件事：
#   元数据里嵌套类型的 full_name 是 'EasingType'（无外层前缀），
#   而 dump 写的是 'TransformPingPongMotion.EasingType'，
#   需用 declaringTypeIndex(@12) 逐级上溯补齐前缀，才能正确比对与展示。
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

    # ---------- 构造嵌套感知的全名
    decl_idx = [struct.unpack_from("<i", m._mm, td.offset + i * 88 + 12)[0] for i in range(T)]

    # declaringTypeIndex(@12) 实测指向 Il2CppType 空间（与 byvalTypeIndex 同空间），
    # 不是 typedef 下标 —— 必须经 byval 反查表换算成 typedef 下标才能上溯。
    byval_to_td = {t.byval_type: t.index for t in m.types}

    def nested_full(i: int) -> str:
        """用 declaringTypeIndex 逐级上溯构造 Outer.Inner 形式的名字。"""
        seen = set()
        parts = []
        cur_i = i
        while 0 <= cur_i < T and cur_i not in seen:
            seen.add(cur_i)
            parts.append(m.types[cur_i].name)
            d = decl_idx[cur_i]
            nxt = byval_to_td.get(d, -1)      # Il2CppType 索引 -> typedef 下标
            if nxt < 0 or nxt >= T:
                break
            cur_i = nxt
        parts.reverse()
        # 最外层带命名空间
        outer_i = cur_i if 0 <= cur_i < T else i
        ns = m.types[outer_i].namespace
        out = ".".join(parts)
        if ns and not out.startswith(ns + "."):
            out = ns + "." + out
        return out

    n_nested = sum(1 for i in range(T) if decl_idx[i] >= 0)
    print("=" * 74)
    print(f"1. 嵌套类型命名：declaringTypeIndex>=0 的类型 {n_nested:,} 个")
    print("=" * 74)
    shown = 0
    for i in range(T):
        if decl_idx[i] >= 0 and shown < 10:
            print(f"   [{i}] 原full_name={m.types[i].full_name}")
            print(f"       嵌套全名={nested_full(i)}")
            shown += 1

    name_to_byval: dict[str, int] = {}
    for i in range(T):
        name_to_byval.setdefault(nested_full(i), m.types[i].byval_type)
        name_to_byval.setdefault(m.types[i].full_name, m.types[i].byval_type)
    byval_to_name = {}
    for i in range(T):
        byval_to_name.setdefault(m.types[i].byval_type, nested_full(i))

    byval_set = set(t.byval_type for t in m.types)

    # ---------- 最终覆盖率
    print()
    print("=" * 74)
    print("2. 字段类型解析的最终覆盖率与精度（用嵌套感知的 byval 反查表）")
    print("=" * 74)
    exact = leaf_ok = wrong = cannot = 0
    bad = []
    unresolved_kinds = collections.Counter()
    total = 0
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
            total += 1
            v = struct.unpack_from("<i", m._mm, fe.offset + fi * 12 + 4)[0]
            got = byval_to_name.get(v - 1)
            if got is None:
                cannot += 1
                if ftype.endswith("[]"):
                    unresolved_kinds["数组 T[]"] += 1
                elif "<" in ftype:
                    unresolved_kinds["泛型实例"] += 1
                elif "*" in ftype or "&" in ftype:
                    unresolved_kinds["指针/引用"] += 1
                else:
                    unresolved_kinds["具体类型名(待查)"] += 1
            elif got == ftype:
                exact += 1
            elif got.rsplit(".", 1)[-1] == ftype.rsplit(".", 1)[-1]:
                leaf_ok += 1
            else:
                wrong += 1
                if len(bad) < 10:
                    bad.append(f"{cls}.{fname}: dump={ftype} 解析={got} (v={v})")
    res = exact + leaf_ok + wrong
    print(f"  参与比对字段 {total:,}")
    print(f"  可解析 {res:,}：完全一致 {exact:,}  嵌套写法不同 {leaf_ok:,}  真错 {wrong:,}")
    print(f"  → 精度 {exact*100.0/res:.2f}%，含嵌套归一 {(exact+leaf_ok)*100.0/res:.2f}%")
    print(f"  解析不出 {cannot:,}，构成: {dict(unresolved_kinds)}")
    for s in bad:
        print(f"    真错: {s}")

    # ---------- 全局：v-1 规则能覆盖多少字段
    print()
    print("=" * 74)
    print("3. 全局 81,872 个字段中，v-1 规则可解析的比例")
    print("=" * 74)
    n = fe.size // 12
    vals = [struct.unpack_from("<3i", m._mm, fe.offset + i * 12)[1] for i in range(n)]
    ok = sum(1 for v in vals if (v - 1) in byval_set)
    print(f"  可解析 {ok:,} / {n:,} = {ok*100.0/n:.2f}%")
    print(f"  不可解析 {n-ok:,} = {(n-ok)*100.0/n:.2f}%（运行时构造的泛型/数组/指针类型）")

    # ---------- 抽样展示最终效果
    print()
    print("=" * 74)
    print("4. 效果抽样：UserData_Service 的字段类型")
    print("=" * 74)
    us = m.type_by_name("UserData_Service")
    if us:
        for f in m.fields_of(us):
            v = struct.unpack_from("<i", m._mm, fe.offset + f.index * 12 + 4)[0]
            got = byval_to_name.get(v - 1)
            print(f"  {f.name:<38} -> {got if got else '(运行时类型, v=%d)' % v}")
