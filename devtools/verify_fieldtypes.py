# 字段类型解析的验收探针
#
# 修复前：字段类型解析率 0.00%（class_detail 里每个字段的 type 都是 None）
# 现规则：真实类型 = 小于 typeIndex 的最大 byval 键（经 27,264 例真值验证 100% 正确）
# 本探针给出「解析率」与「精度」两个指标，并用定点类做人工核对。
import sys, io, re, struct, collections
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
# 仓库路径由 samples.py 推导（不许硬编码工作区绝对路径）
# 用 os.path 推导，避免依赖后面才 import 的 pathlib
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import samples  # noqa: E402
samples.add_src_to_path()

from gamemod_mcp.engine.il2cpp_meta import Metadata

# 样本路径：根目录从 samples.py 取，派生路径用相对路径拼出来
_GAME = samples.find_il2cpp()
META = (_GAME / "Garden of Witches_Data" / "il2cpp_data"
        / "Metadata" / "global-metadata.dat") if _GAME else None
DUMP = samples.find_il2cpp_dump()

# 样本不可用时记为「无法验证」并跳过（跳过不算失败）
if not samples.require(META, "IL2CPP 元数据 Garden of Witches"):
    sys.exit(0)
if not samples.require(DUMP, "IL2CPP 字段类型真值 il2cpp_dump.cs"):
    sys.exit(0)

with Metadata(META) as m:
    fe = m.tables["fields"]
    n = fe.size // 12

    # ---------- 1. 解析率 + 置信度分布
    ok = sum(1 for f in m.fields if m.resolve_field_type(f.type_index))
    print("=" * 74)
    print("1. 字段类型解析率与置信度")
    print("=" * 74)
    print(f"  可解析 {ok:,} / {n:,} = {ok*100.0/n:.2f}%")
    print("  对照：方法返回类型 134,814 / 139,501 = 96.64%")
    print()
    conf = collections.Counter(
        m.resolve_field_type_detail(f.type_index)["confidence"] for f in m.fields
    )
    print(f"  exact（delta=1，直接命中本体，可放心采信）: {conf['exact']:,} "
          f"({conf['exact']*100.0/n:.2f}%)")
    print(f"  derived（delta>1，派生槽位，给出的是元素/泛型实参类型名）: {conf['derived']:,} "
          f"({conf['derived']*100.0/n:.2f}%)")
    print(f"  unknown（typeIndex 小于全部 byval 键，静态无法定位）: {conf['unknown']:,}")
    print("  注意：derived 是把 T[] / List<T> 还原成了 T，不是完整签名。")

    # ---------- 2. 用 dump 真值算精度
    print()
    print("=" * 74)
    print("2. 与 Il2CppDumper 输出比对的精度")
    print("=" * 74)

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

    exact = wrong = cannot = 0
    bad = []
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
            got = m.resolve_field_type(m.fields[fi].type_index)
            if got is None:
                cannot += 1
            elif got == ftype:
                exact += 1
            else:
                wrong += 1
                if len(bad) < 10:
                    bad.append(f"{cls}.{fname}: dump={ftype} 解析={got}")
    res = exact + wrong
    print(f"  可解析 {res:,}：正确 {exact:,}  错误 {wrong:,} → 精度 {exact*100.0/res:.2f}%")
    print(f"  解析不出 {cannot:,}（typeIndex 小于全部 byval 键，静态不可达）")
    for s in bad:
        print(f"    真错: {s}")

    # ---------- 3. 定点核对
    print()
    print("=" * 74)
    print("3. 定点核对：嵌套类型与普通类的字段")
    print("=" * 74)
    for cls in ("TeamTapas.ProjectR.UserDatas.Service.UserData_Service",
                "TransformPingPongMotion",
                "PresetLoaderV2"):
        t = m.type_by_name(cls)
        if t is None:
            print(f"  {cls}: 未找到")
            continue
        print(f"  {t.nested_name}  (原始 full_name={t.full_name})")
        for f in m.fields_of(t)[:6]:
            print(f"      {f.name:<40} -> {m.resolve_field_type(f.type_index)}")

    # ---------- 4. 置信度区分的定点演示
    print()
    print("=" * 74)
    print("4. exact 与 derived 的对比演示（同一个类里两种情形）")
    print("=" * 74)
    t = m.type_by_name("PresetLoaderV2")
    for f in m.fields_of(t):
        d = m.resolve_field_type_detail(f.type_index)
        print(f"  {f.name:<30} delta={d['delta']:>3} "
              f"{d['confidence']:<8} -> {d['name']}")

    # ---------- 5. 嵌套类型命名总览
    print()
    print("=" * 74)
    print("5. 嵌套类型命名（nested_name 与 full_name 不同的类型）")
    print("=" * 74)
    diff = [t for t in m.types if t.nested_name != t.full_name]
    print(f"  共 {len(diff):,} 个类型的 nested_name 与 full_name 不同")
    for t in diff[:8]:
        print(f"    {t.full_name:<44} -> {t.nested_name}")
