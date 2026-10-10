# 定性分析 121 个不一致的类：是偏移问题，还是 dump 侧遗漏？
#
# 判定方法：
#   - 若 dump 的列表是元数据列表的**连续子序列**，说明只是边界/起点差异（偏移问题）
#   - 若是**非连续子集**（中间缺方法），说明 dump 本身遗漏了方法（dump 侧问题）
#   - 另外统计重名类型，排除 type_by_name 取错重名项的干扰
import sys, io, re, collections
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\Administrator\Documents\deepseek-harness\默认工作区\GameModMCP\src")

from gamemod_mcp.engine.il2cpp_meta import Metadata

META = r"D:\Projects\Garden of Witches\Garden of Witches_Data\il2cpp_data\Metadata\global-metadata.dat"
DUMP = r"D:\Projects\aiMod\dump\Garden of Witches.exe\il2cpp_dump.cs"

RE_TOP = re.compile(r"^(?:class|enum)\s+(\S+?)\s*(?::.*)?$")
RE_MEMBER = re.compile(r"^\s*/\*0x[0-9a-f]+\*/\s+.+?\s+(\S+)\(")


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


def is_contiguous_subseq(small, big):
    """small 是否为 big 的连续子序列（考虑重复元素的朴素查找）。"""
    n, k = len(big), len(small)
    if k == 0:
        return True
    if k > n:
        return False
    for i in range(n - k + 1):
        if big[i:i + k] == small:
            return True
    return False


dump_map = dump_classes(DUMP)

with Metadata(META) as m:
    # ---------- 重名类型统计
    names = collections.Counter(t.full_name for t in m.types)
    dupes = {n: c for n, c in names.items() if c > 1}
    print(f"元数据中重名类型: {len(dupes):,} 个名字有多个定义")
    if dupes:
        top = sorted(dupes.items(), key=lambda kv: -kv[1])[:5]
        print(f"  最多的几个: {top}")

    # ---------- 分类统计
    cat = collections.Counter()
    mismatches = []

    checked = 0
    for name, dump_names in dump_map.items():
        if not dump_names:
            continue
        t = m.type_by_name(name)
        if t is None or m.is_enum(t):
            continue
        meta_names = [x.name for x in m.methods_of(t)]
        checked += 1

        a, b = collections.Counter(meta_names), collections.Counter(dump_names)
        if a == b or (not (b - a) and (a - b) == collections.Counter({".cctor": 1})):
            cat["一致"] += 1
            continue

        if is_contiguous_subseq(dump_names, meta_names):
            cat["dump是元数据的连续子序列(偏移)"] += 1
        elif is_contiguous_subseq(meta_names, dump_names):
            cat["元数据是dump的连续子序列"] += 1
        elif not (a - b):
            cat["元数据⊆dump(纯遗漏)"] += 1
        elif not (b - a):
            cat["dump⊆元数据(纯遗漏)"] += 1
        else:
            cat["双方各有多余(交错差异)"] += 1
            mismatches.append((name, meta_names, dump_names))

        if checked >= 4000:
            break

    print()
    print("=" * 74)
    print(f"检查 {checked:,} 个类，分类结果：")
    print("=" * 74)
    for k, v in cat.most_common():
        print(f"  {k:<34} : {v:,}")

    print()
    print("=== 交错差异样本（前 5 个，看是否元数据多出编译器生成名）===")
    for name, mn, dn in mismatches[:5]:
        a, b = collections.Counter(mn), collections.Counter(dn)
        print(f"\n  {name}")
        print(f"    元数据({len(mn)}) 多出: {list((a-b).elements())[:6]}")
        print(f"    dump({len(dn)})  多出: {list((b-a).elements())[:6]}")

    # ---------- 元数据中 method_count=0 的类型有多少
    zero_m = sum(1 for t in m.types if t.method_count == 0)
    neg = sum(1 for t in m.types if t.method_start < 0)
    print()
    print(f"元数据中 method_count=0 的类型: {zero_m:,}")
    print(f"元数据中 method_start<0 的类型: {neg:,}")

    # EmbeddedAttribute 的两个定义
    print()
    print("=== Microsoft.CodeAnalysis.EmbeddedAttribute 在元数据中的所有定义 ===")
    for t in m.types:
        if t.name == "EmbeddedAttribute":
            print(f"  idx={t.index} start={t.method_start} count={t.method_count} "
                  f"镜像={m.image_of_type(t)} 方法={[x.name for x in m.methods_of(t)]}")
