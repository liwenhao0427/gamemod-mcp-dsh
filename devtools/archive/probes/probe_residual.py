# 残余 3% 不一致的归因验证
#
# 已发现的三类 dump 侧变换（不是元数据的问题）：
#   1. 泛型方法名带参数：元数据 SwapTo  →  dump SwapTo<T>
#   2. 同名类型被 dump 合并：<>c 在元数据里有 456 个定义，dump 按名字只留一个
#   3. dump 省略编译器生成的方法
#
# 若按这三条规则归一化后一致率显著上升，则确认 method_start 无偏移。
import sys, io, re, collections
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\Administrator\Documents\deepseek-harness\默认工作区\GameModMCP\src")

from gamemod_mcp.engine.il2cpp_meta import Metadata

META = r"D:\Projects\Garden of Witches\Garden of Witches_Data\il2cpp_data\Metadata\global-metadata.dat"
DUMP = r"D:\Projects\aiMod\dump\Garden of Witches.exe\il2cpp_dump.cs"

RE_TOP = re.compile(r"^(?:class|enum)\s+(\S+?)\s*(?::.*)?$")
RE_MEMBER = re.compile(r"^\s*/\*0x[0-9a-f]+\*/\s+.+?\s+(\S+)\(")
RE_GENERIC = re.compile(r"<[^<>]*>")


def dump_classes(path):
    result = {}
    cur = None
    depth = 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            s = line.rstrip("\r\n")
            t = s.strip()
            if depth == 0:
                m2 = RE_TOP.match(s)
                if m2:
                    cur = m2.group(1)
                    result.setdefault(cur, [])
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
                m3 = RE_MEMBER.match(s)
                if m3:
                    result[cur].append(m3.group(1))
    return result


def norm(n: str) -> str:
    """归一化方法名：去掉泛型参数。"""
    return RE_GENERIC.sub("", n)


dump_map = dump_classes(DUMP)

with Metadata(META) as m:
    # 同名类型只统计一次（合并所有同名定义的方法），模拟 dump 的行为
    merged: dict[str, collections.Counter] = {}
    for t in m.types:
        if m.is_enum(t):
            continue
        c = merged.setdefault(t.full_name, collections.Counter())
        for x in m.methods_of(t):
            c[norm(x.name)] += 1

    cat = collections.Counter()
    checked = 0
    residual = []

    for name, dump_names in dump_map.items():
        if not dump_names:
            continue
        t = m.type_by_name(name)
        if t is None or m.is_enum(t):
            continue
        checked += 1

        b = collections.Counter(norm(x) for x in dump_names)
        a = merged.get(name, collections.Counter())

        if a == b:
            cat["归一化后完全一致"] += 1
        elif not (b - a):
            # dump 是元数据的子集 → dump 省略了方法
            cat["dump⊆元数据(dump省略)"] += 1
            if len(residual) < 8:
                residual.append(("dump省略", name, list((a - b).elements())[:4]))
        elif not (a - b):
            cat["元数据⊆dump(元数据遗漏)"] += 1
            if len(residual) < 12:
                residual.append(("元数据遗漏", name, list((b - a).elements())[:4]))
        else:
            cat["仍不一致"] += 1
            if len(residual) < 16:
                residual.append(("交错", name,
                                 f"元数据多{list((a-b).elements())[:3]} dump多{list((b-a).elements())[:3]}"))

        if checked >= 4000:
            break

    print("=" * 74)
    print(f"按「泛型名归一化 + 同名类型合并」后，检查 {checked:,} 个类：")
    print("=" * 74)
    for k, v in cat.most_common():
        print(f"  {k:<26} : {v:>6,}  ({v*100.0/checked:.2f}%)")

    print()
    print("=== 残余样本 ===")
    for kind, name, detail in residual:
        print(f"  [{kind}] {name}")
        print(f"        {detail}")
