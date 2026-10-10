"""覆盖率自检：确认 45 张表全部有列定义、13 个 coded index 全部实现。

这个检查存在的意义：防止「漏写某张表的列定义」而在运行时才炸
（_column_plan 对未定义的表会抛 MetadataError，但那张表可能正好
 在测试样本里不存在，于是漏写就悄悄逃过验证）。
"""

import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
# 仓库路径由 samples.py 推导（不许硬编码工作区绝对路径）
# 用 os.path 推导，避免依赖后面才 import 的 pathlib
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import samples  # noqa: E402
samples.add_src_to_path()

import inspect

from gamemod_mcp.engine.mono_meta import CODED_INDEX_TABLES, TABLE_NAMES, Metadata

ok = True

print("=" * 70)
print("1. 表列定义覆盖率（ECMA-335 II.22 共 45 张表）")
print("=" * 70)
src = inspect.getsource(Metadata._column_plan)
defined = [n for n in TABLE_NAMES if f'"{n}":' in src]
missing = [n for n in TABLE_NAMES if n not in defined]
print(f"  TABLE_NAMES 总数        : {len(TABLE_NAMES)}")
print(f"  _column_plan 已定义列数 : {len(defined)}")
print(f"  缺列定义的表            : {missing if missing else '（无）'}")
ok &= len(defined) == len(TABLE_NAMES) and not missing

print()
print("=" * 70)
print("2. coded index 覆盖（ECMA-335 II.24.2.6 共 13 个）")
print("=" * 70)
print(f"  实现个数: {len(CODED_INDEX_TABLES)}")
for name in sorted(CODED_INDEX_TABLES):
    tables = CODED_INDEX_TABLES[name]
    tag_bits = (len(tables) - 1).bit_length() if len(tables) > 1 else 1
    pretty = ", ".join(t or "—" for t in tables)
    print(f"    {name:<26} {tag_bits} 位 tag  [{pretty}]")
ok &= len(CODED_INDEX_TABLES) == 13

print()
print("=" * 70)
print("3. 列定义引用的表名 / coded 名是否都真实存在")
print("=" * 70)
bad_refs = []
for tname in TABLE_NAMES:
    for col, spec in Metadata._column_plan(tname):
        if isinstance(spec, tuple):
            kind, arg = spec
            if kind == "idx" and arg not in TABLE_NAMES:
                bad_refs.append(f"{tname}.{col} -> idx({arg}) 表不存在")
            if kind == "cod" and arg not in CODED_INDEX_TABLES:
                bad_refs.append(f"{tname}.{col} -> cod({arg}) 未定义")
for b in bad_refs:
    print(f"  [BAD] {b}")
if not bad_refs:
    print("  [OK]  所有 idx/cod 引用的名字都存在")
ok &= not bad_refs

print()
print("=" * 70)
print("4. coded index 表集合里的表名是否都真实存在")
print("=" * 70)
bad_tables = []
for coded, tables in CODED_INDEX_TABLES.items():
    for t in tables:
        if t is not None and t not in TABLE_NAMES:
            bad_tables.append(f"{coded} -> {t}")
for b in bad_tables:
    print(f"  [BAD] {b}")
if not bad_tables:
    print("  [OK]  所有 coded index 引用的表名都存在")
ok &= not bad_tables

print()
print("=" * 70)
print(f"结论: {'全部通过' if ok else '存在问题'}")
print("=" * 70)
sys.exit(0 if ok else 1)
