# 跨游戏验证：字段 typeIndex 的平移量是否与 Unity/IL2CPP 版本相关
#
# 发现在 Garden of Witches (v31) 上：field.typeIndex - 1 ∈ byval 键集合（精度 100%）
# 但 method.returnType ∈ byval 键集合 恰好 delta=0
# 若各游戏的最优 delta 不同，则说明这是版本相关的约定，索引层应**自动探测**该平移
import sys, io, os, struct, glob, collections
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\Administrator\Documents\deepseek-harness\默认工作区\GameModMCP\src")

from gamemod_mcp.engine.il2cpp_meta import Metadata

ROOTS = [
    r"D:\Projects",
]

found = []
for root in ROOTS:
    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath.count(os.sep) - root.count(os.sep) > 4:
            dirnames[:] = []
            continue
        if "global-metadata.dat" in filenames:
            found.append(os.path.join(dirpath, "global-metadata.dat"))

print(f"发现 {len(found)} 个 global-metadata.dat")
print()

summary = []
for path in found:
    try:
        with Metadata(path) as m:
            ver = m.version
            byval_set = set(t.byval_type for t in m.types)
            e = m.tables["fields"]
            n = e.size // 12
            fvals = [struct.unpack_from("<3i", m._mm, e.offset + i * 12)[1] for i in range(n)]

            me = m.tables["methods"]
            mn = me.size // 36
            mvals = [struct.unpack_from("<6i", m._mm, me.offset + i * 36)[2] for i in range(mn)]

            frow = []
            for d in range(-2, 3):
                hit = sum(1 for v in fvals if (v + d) in byval_set)
                frow.append(hit * 100.0 / n if n else 0)
            mrow = []
            for d in range(-2, 3):
                hit = sum(1 for v in mvals if (v + d) in byval_set)
                mrow.append(hit * 100.0 / mn if mn else 0)

            game = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(path))))
            summary.append((game, ver, n, mn, frow, mrow, path))
    except Exception as exc:
        print(f"!! {path}: {type(exc).__name__}: {exc}")

print("=" * 110)
print("版本  游戏                                字段数    方法数   字段最佳delta  方法最佳delta")
print("=" * 110)
for game, ver, n, mn, frow, mrow, path in summary:
    bf = max(range(-2, 3), key=lambda d: frow[d + 2])
    bm = max(range(-2, 3), key=lambda d: mrow[d + 2])
    print(f"v{ver:<4} {game[:32]:<34} {n:>8,} {mn:>8,}   "
          f"{bf:+d} ({frow[bf+2]:5.2f}%)     {bm:+d} ({mrow[bm+2]:5.2f}%)")

print()
print("=" * 110)
print("明细（delta 依次为 -2..+2 的命中率）")
print("=" * 110)
for game, ver, n, mn, frow, mrow, path in summary:
    print(f"{game[:34]:<36} v{ver}")
    print(f"   字段: " + "  ".join(f"{d:+d}={r:5.2f}%" for d, r in zip(range(-2, 3), frow)))
    print(f"   方法: " + "  ".join(f"{d:+d}={r:5.2f}%" for d, r in zip(range(-2, 3), mrow)))
    print(f"   {path}")
