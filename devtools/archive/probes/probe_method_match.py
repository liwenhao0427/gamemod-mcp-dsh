# 精确比对：元数据 methods_of() 与 dump 中该类的完整方法列表
# 关键：dump 里嵌套类也在父类大括号内，必须用花括号深度过滤，只取深度 1 的方法
import sys, io, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\Administrator\Documents\deepseek-harness\默认工作区\GameModMCP\src")

from gamemod_mcp.engine.il2cpp_meta import Metadata

META = r"D:\Projects\Garden of Witches\Garden of Witches_Data\il2cpp_data\Metadata\global-metadata.dat"
DUMP = r"D:\Projects\aiMod\dump\Garden of Witches.exe\il2cpp_dump.cs"


def dump_class_methods(path: str, class_decl: str) -> list[str]:
    """从 dump 中提取某个类自身（深度 1）的方法名列表。"""
    out: list[str] = []
    found = False
    depth = 0
    started = False

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not found:
                if line.startswith(class_decl):
                    found = True
                continue

            if not started:
                if line.startswith("{"):
                    started = True
                    depth = 1
                continue

            stripped = line.rstrip("\n")

            # 先处理闭括号
            if stripped.startswith("}"):
                depth -= 1
                if depth <= 0:
                    break
                continue

            if stripped.strip() == "" or stripped.strip() == "{":
                if stripped.strip() == "{":
                    depth += 1
                continue

            # 深度 1 才是该类自己的成员
            if depth == 1:
                mm2 = re.match(r"\s*/\*0x[0-9a-f]+\*/\s+.+?\s+(\S+)\(", stripped)
                if mm2:
                    out.append(mm2.group(1))

    return out


with Metadata(META) as m:
    for class_name in (
        "TeamTapas.ProjectR.UserDatas.Service.UserData_Service",
        "TeamTapas.ProjectR.Domain.CurrencyKey",
        "TeamTapas.ProjectR.CursorManager",
    ):
        t = m.type_by_name(class_name)
        if t is None:
            print(f"!! 元数据中未找到 {class_name}")
            continue

        meta_names = [x.name for x in m.methods_of(t)]

        # 枚举没有方法（只有 value__ 之类），跳过
        if m.is_enum(t):
            print(f"=== {class_name} (枚举) 字段={[f.name for f in m.fields_of(t)]}")
            print()
            continue

        decl = f"class {class_name} "
        dump_names = dump_class_methods(DUMP, decl)

        print("=" * 72)
        print(f"{class_name}")
        print("=" * 72)
        print(f"  元数据 method_start={t.method_start} count={t.method_count} → {len(meta_names)} 个")
        print(f"  dump                                              → {len(dump_names)} 个")

        if meta_names == dump_names:
            print("  ✅ 完全一致")
        elif meta_names == dump_names[1:]:
            print("  ⚠️ 元数据 = dump 去掉首个方法（dump 首项: "
                  f"{dump_names[0]}）")
        elif dump_names == meta_names[1:]:
            print(f"  ⚠️ dump = 元数据去掉首个方法（元数据首项: {meta_names[0]}）")
        else:
            print("  ❌ 不一致")
            print(f"     元数据前6: {meta_names[:6]}")
            print(f"     dump  前6: {dump_names[:6]}")
            print(f"     元数据后3: {meta_names[-3:]}")
            print(f"     dump  后3: {dump_names[-3:]}")
            # 找第一个分歧点
            for i in range(min(len(meta_names), len(dump_names))):
                if meta_names[i] != dump_names[i]:
                    print(f"     首个分歧 @ {i}: 元数据={meta_names[i]} / dump={dump_names[i]}")
                    break
        print()

    # ---- 用 dump 反查：哪个类型拥有 Finalize（delta=0 时 139500 号）
    print("=" * 72)
    print("最后若干方法的归属（delta=0）")
    print("=" * 72)
    for k in range(len(m.methods) - 5, len(m.methods)):
        owner = None
        for t in m.types:
            if t.method_start <= k < t.method_start + t.method_count:
                owner = t.full_name
                break
        print(f"  [{k}] {m.methods[k].name:<22} 归属={owner}")
