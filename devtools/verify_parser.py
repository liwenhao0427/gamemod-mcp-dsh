# 重写后的 IL2CPP 解析器 —— 对照 AiMod dump 的完整验收
#
# 验收分五关，逐关给出可证伪的量化结论：
#   1. 内部自检：Σmethod_count / Σfield_count 必须等于表条数（布局正确性的最强内部证据）
#   2. 定点核对：UserData_Service 的 26 字段 / 50 方法、CurrencyKey 枚举
#   3. 继承关系：对 dump 中全部顶层类做父类比对
#   4. 枚举识别：对 dump 中判定为 System.Enum 的类型全量比对
#   5. 方法归属：对 dump 中全部顶层类做方法多重集比对（用铺满性质证明无偏移）
import sys, io, re, collections
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
if not samples.require(DUMP, "IL2CPP 真值 dump il2cpp_dump.cs"):
    sys.exit(0)

RE_TOP = re.compile(r"^(class|enum|interface)\s+(\S+?)\s*(?::\s*(.*))?$")
RE_DECL = re.compile(r"^\s*/\*0x[0-9a-f]+\*/\s+(.*)\((.*)\)\s*;?\s*$")
RE_GENERIC = re.compile(r"<[^<>]*>")


def member_name(pre_paren: str) -> str:
    """从「返回类型 + 方法名」里取出方法名。

    难点：泛型方法名自身可能含空格，如 FromAsync<TArg1, TArg2>。
    做法：从右往左找**尖括号深度为 0** 的最后一个空格，其后即为方法名。
    """
    depth = 0
    for i in range(len(pre_paren) - 1, -1, -1):
        ch = pre_paren[i]
        if ch == ">":
            depth += 1
        elif ch == "<":
            depth -= 1
        elif ch == " " and depth == 0:
            return pre_paren[i + 1:].strip()
    return pre_paren.strip()


def parse_dump(path):
    """解析 dump，返回 (父类表, 方法表)，且**只取顶层类型**（排除嵌套类干扰）。"""
    base_of: dict[str, str] = {}
    methods_of: dict[str, list[str]] = {}
    cur = None
    depth = 0

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            s = line.rstrip("\r\n")
            t = s.strip()

            if depth == 0:
                m2 = RE_TOP.match(s)
                if m2:
                    kind, name, bases = m2.group(1), m2.group(2), m2.group(3)
                    cur = name
                    if bases:
                        # 形如 "Base, IFoo, IBar" —— 第一个才是基类，其余是接口
                        first = bases.split(",")[0].strip()
                        # 泛型基类形如 Foo<T>，元数据里 parent 是泛型实例，静态无法还原，跳过
                        if first and "<" not in first:
                            base_of[name] = first
                    methods_of.setdefault(name, [])
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
                m3 = RE_DECL.match(s)
                if m3:
                    nm = member_name(m3.group(1))
                    if nm:
                        methods_of[cur].append(nm)

    return base_of, methods_of


print("正在解析 dump（19 MB，稍候）...")
base_of, dump_methods = parse_dump(DUMP)
print(f"  顶层类型 {len(base_of):,} 个（含继承信息）")

with Metadata(META) as m:
    # ---------------------------------------------------------- 1. 内部自检
    print()
    print("=" * 74)
    print("1. 布局自检：Σmethod_count / Σfield_count 是否等于表条数")
    print("=" * 74)
    v = m.validate()
    print(f"  Σmethod_count = {v['sum_method_count']:,}   methods 表 = {v['methods_table_count']:,}")
    print(f"  Σfield_count  = {v['sum_field_count']:,}   fields  表 = {v['fields_table_count']:,}")
    assert v["methods_ok"] and v["fields_ok"], "计数不自洽，记录布局解析错误！"
    print("  ✅ 两者均完全相等（差 0）—— 88 字节 Typedef 布局正确")

    # 铺满性质：所有类型的方法区间应恰好无缝覆盖 [0, 总数)
    cover = [0] * len(m.methods)
    for t in m.types:
        if t.method_count > 0 and 0 <= t.method_start:
            for k in range(t.method_start, min(t.method_start + t.method_count, len(cover))):
                cover[k] += 1
    zero = cover.count(0)
    one = cover.count(1)
    over = sum(1 for c in cover if c > 1)
    print(f"  铺满检验：恰好覆盖 1 次 = {one:,}，空隙 = {zero}，重叠 = {over}")
    assert zero == 0 and over == 0, "方法区间未无缝铺满，说明 method_start 有偏移！"
    print("  ✅ 区间无缝铺满 —— method_start 就是区间起点，无 ±1 偏移")

    # ---------------------------------------------------------- 2. 定点核对
    print()
    print("=" * 74)
    print("2. 定点核对（对照 dump 真值）")
    print("=" * 74)
    uds = m.type_by_name("TeamTapas.ProjectR.UserDatas.Service.UserData_Service")
    fs, ms = m.fields_of(uds), m.methods_of(uds)
    print(f"  UserData_Service: 字段 {len(fs)}（真值 26）  方法 {len(ms)}（真值 50）")
    assert len(fs) == 26 and len(ms) == 50
    print(f"    首字段={fs[0].name}  末字段={fs[-1].name}（真值 _data / messageHub）")
    assert fs[0].name == "_data" and fs[-1].name == "messageHub"
    print(f"    基类={m.parent_of(uds).full_name}（真值 System.Object）")
    assert m.parent_of(uds).full_name == "System.Object"

    ck = m.type_by_name("TeamTapas.ProjectR.Domain.CurrencyKey")
    print(f"  CurrencyKey: is_enum={m.is_enum(ck)}  基类={m.parent_of(ck).full_name}")
    assert m.is_enum(ck) and m.parent_of(ck).full_name == "System.Enum"
    print(f"    成员={[f.name for f in m.fields_of(ck) if f.name != 'value__']}")
    print("  ✅ 全部与 dump 一致")

    # ---------------------------------------------------------- 3. 继承关系
    print()
    print("=" * 74)
    print("3. 继承关系全量比对")
    print("=" * 74)
    hit = miss = unres = notfound = 0
    bad = []
    for name, base in base_of.items():
        tt = m.type_by_name(name)
        if tt is None:
            notfound += 1
            continue
        p = m.parent_of(tt)
        if p is None:
            unres += 1
            continue
        if p.full_name == base:
            hit += 1
        else:
            miss += 1
            if len(bad) < 6:
                bad.append(f"{name}: 期望 {base} / 得到 {p.full_name}")
    tot = hit + miss
    print(f"  命中 {hit:,} / 可解析 {tot:,} = {hit*100.0/tot:.2f}%")
    print(f"  父类无法静态还原（泛型实例/数组）: {unres:,}   dump 中有但元数据无: {notfound}")
    for b in bad:
        print(f"    反例: {b}")

    # ---------------------------------------------------------- 4. 枚举识别
    print()
    print("=" * 74)
    print("4. 枚举识别全量比对")
    print("=" * 74)
    dump_enums = {n for n, b in base_of.items() if b == "System.Enum"}
    ok = sum(1 for n in dump_enums if (tt := m.type_by_name(n)) and m.is_enum(tt))
    print(f"  dump 判定枚举 {len(dump_enums):,} 个 → 元数据识别 {ok:,} 个 "
          f"= {ok*100.0/len(dump_enums):.2f}%")
    assert ok == len(dump_enums), "枚举识别有遗漏！"
    print("  ✅ 100% 识别")

    # ---------------------------------------------------------- 5. 方法归属
    print()
    print("=" * 74)
    print("5. 方法归属比对（多重集，归一化泛型名）")
    print("=" * 74)

    def norm(s: str) -> str:
        # 去掉泛型实参并压缩空白：dump 写 FromAsync<TArg1, TArg2>，
        # 归一化后应等于元数据里的 FromAsync
        return RE_GENERIC.sub("", s).replace(" ", "")

    # 同名类型合并（dump 按名字输出，元数据中 <>c 等有数百个同名定义）
    merged: dict[str, collections.Counter] = {}
    for t in m.types:
        if m.is_enum(t):
            continue
        c = merged.setdefault(t.full_name, collections.Counter())
        for x in m.methods_of(t):
            c[norm(x.name)] += 1

    exact = subset = extra = other = checked = 0
    extras = []
    for name, dnames in dump_methods.items():
        if not dnames:
            continue
        tt = m.type_by_name(name)
        if tt is None or m.is_enum(tt):
            continue
        checked += 1
        a = merged.get(name, collections.Counter())
        b = collections.Counter(norm(x) for x in dnames)
        if a == b:
            exact += 1
        elif not (b - a):
            subset += 1  # dump 省略了元数据中存在的某些方法
        elif not (a - b):
            extra += 1   # 元数据缺少 dump 中的方法 ← 这才说明解析有误
            if len(extras) < 5:
                extras.append(f"{name}: {list((b - a).elements())[:4]}")
        else:
            other += 1
            if len(extras) < 5:
                extras.append(f"{name}: 交错 元数据多{list((a-b).elements())[:3]} "
                              f"dump多{list((b-a).elements())[:3]}")

    print(f"  检查 {checked:,} 个类：")
    print(f"    完全一致                : {exact:,}  ({exact*100.0/checked:.2f}%)")
    print(f"    dump 省略部分方法        : {subset:,}  ({subset*100.0/checked:.2f}%)")
    print(f"    元数据缺少方法（异常）    : {extra:,}")
    print(f"    交错差异（异常）         : {other:,}")
    for e in extras:
        print(f"      {e}")

    print()
    if extra == 0 and other == 0:
        print("  ✅ 无任何「元数据缺少方法」的情况 → 方法归属解析正确。")
        print("     dump 侧的差异已定性为：① 泛型方法名带 <T> ② 同名类型被合并")
        print("     ③ 省略 .cctor 等编译器生成方法 ④ .ctor 被重排到类首。")
    else:
        print("  ⚠️ 存在异常，需进一步排查。")

    print()
    print("=" * 74)
    print(f"最终统计: {m}")
    print("=" * 74)
