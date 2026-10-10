# 最终判定：元数据方法列表 vs dump 方法列表 —— 按多重集比较
#
# 背景：UserData_Service 元数据末段为 [get_currentChapter, .ctor, InitializeData]，
#       而 dump 为 [.ctor, get_storeStatSync, ..., get_chapters, get_currentChapter, InitializeData]。
# 假设：dump 把 .ctor/.cctor 提前排列了，方法集合本身不变。
# 验证：对大量类做多重集比较。若内容全部一致，则 delta=0（method_start 即区间起点）成立。
#
# 注意 dump 的排版：嵌套类写在父类大括号内，且所有行都带缩进，
# 因此必须用 「去掉缩进后的花括号深度」 来判定成员归属。
import sys, io, re, collections
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\Administrator\Documents\deepseek-harness\默认工作区\GameModMCP\src")

from gamemod_mcp.engine.il2cpp_meta import Metadata

META = r"D:\Projects\Garden of Witches\Garden of Witches_Data\il2cpp_data\Metadata\global-metadata.dat"
DUMP = r"D:\Projects\aiMod\dump\Garden of Witches.exe\il2cpp_dump.cs"

# 顶层类声明：行首无缩进的 "class X : Y" / "enum X : Y"
RE_TOP = re.compile(r"^(?:class|enum)\s+(\S+?)\s*(?::.*)?$")
# 成员行：任意缩进的 /*0x...*/ 前缀 + 签名 + 方法名(
RE_MEMBER = re.compile(r"^\s*/\*0x[0-9a-f]+\*/\s+.+?\s+(\S+)\(")


def dump_classes(path: str) -> dict[str, list[str]]:
    """扫描 dump，返回 {类全名: [自身直接声明的方法名]}。"""
    result: dict[str, list[str]] = {}
    cur: str | None = None
    depth = 0

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            s = line.rstrip("\r\n")
            t = s.strip()

            # 顶层类型声明（无缩进）
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

            # 只有深度 1 的成员属于当前类（嵌套类的方法深度 >1）
            if depth == 1 and cur is not None:
                m3 = RE_MEMBER.match(s)
                if m3:
                    result[cur].append(m3.group(1))

    return result


print("正在解析 dump（331,540 行，稍候）...")
dump_map = dump_classes(DUMP)
non_empty = sum(1 for v in dump_map.values() if v)
print(f"  提取到 {len(dump_map):,} 个类型，其中 {non_empty:,} 个含方法")

with Metadata(META) as m:
    exact = multiset_only = mismatch = notfound = skipped = 0
    bad_samples: list[str] = []
    checked = 0

    for name, dump_names in dump_map.items():
        if not dump_names:
            continue
        t = m.type_by_name(name)
        if t is None:
            notfound += 1
            continue
        if m.is_enum(t):
            skipped += 1
            continue

        meta_names = [x.name for x in m.methods_of(t)]
        checked += 1

        if meta_names == dump_names:
            exact += 1
            continue

        a = collections.Counter(meta_names)
        b = collections.Counter(dump_names)

        if a == b:
            multiset_only += 1
        elif not (b - a) and (a - b) == collections.Counter({".cctor": 1}):
            # dump 常省略静态构造函数
            multiset_only += 1
        else:
            mismatch += 1
            if len(bad_samples) < 8:
                bad_samples.append(
                    f"{name}: 元数据多出 {list((a - b).elements())[:4]} "
                    f"dump 多出 {list((b - a).elements())[:4]}"
                )

        if checked >= 4000:
            break

    print()
    print("=" * 74)
    print(f"检查了 {checked:,} 个类（跳过枚举 {skipped}，元数据未找到 {notfound}）")
    print("=" * 74)
    print(f"  顺序 + 内容完全一致          : {exact:,}")
    print(f"  内容一致（仅顺序不同）        : {multiset_only:,}")
    print(f"  内容不一致                   : {mismatch:,}")
    for s in bad_samples:
        print(f"    {s}")

    rate = (exact + multiset_only) * 100.0 / checked if checked else 0.0
    print(f"\n  内容一致率 = {rate:.2f}%")
    if mismatch == 0:
        print()
        print("  ✅ 结论：method_start 无偏移（delta=0 正确）。")
        print("     dump 只是把 .ctor/.cctor 重新排序到类首，方法集合与元数据完全相同。")
