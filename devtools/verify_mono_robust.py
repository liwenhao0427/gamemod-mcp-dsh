"""边界与失败路径验证：确保 mono_meta 在非法/非托管输入上**清晰地失败**，
而不是返回假的解析结果，也不抛未预期的异常类型。

同时校验三个源文件均为 UTF-8 无 BOM。
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

import tempfile
from pathlib import Path

from gamemod_mcp.engine.mono_meta import Metadata, MetadataError

ROOT = samples.ROOT

results = []


def expect_error(label, fn):
    try:
        fn()
    except MetadataError as exc:
        print(f"  [OK]   {label}\n         -> MetadataError: {exc}")
        results.append((label, True))
    except Exception as exc:  # noqa: BLE001
        print(f"  [BAD]  {label}\n         -> 未预期异常 {type(exc).__name__}: {exc}")
        results.append((label, False))
    else:
        print(f"  [BAD]  {label}\n         -> 竟然成功了（应失败）")
        results.append((label, False))


print("=" * 78)
print("1. 编码检查（必须 UTF-8 无 BOM）")
print("=" * 78)
# 注：此处原先还检查 devtools\probe_mono_paramcount.py，
# 但该文件在 git 历史里从未存在过（死引用会导致整个脚本 FileNotFoundError 崩掉），
# 已移除。
for rel in (
    r"src\gamemod_mcp\engine\mono_meta.py",
    r"devtools\verify_mono.py",
):
    p = ROOT / rel
    raw = p.read_bytes()
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        raw.decode("utf-8")
        decodable = True
    except UnicodeDecodeError as exc:
        decodable = False
        print(f"    解码错误: {exc}")
    ok = decodable and not has_bom
    print(f"  [{'OK' if ok else 'BAD'}]  {rel}  {len(raw):,} 字节  "
          f"BOM={has_bom}  UTF-8可解码={decodable}")
    results.append((f"编码 {rel}", ok))

print()
print("=" * 78)
print("2. 失败路径（不得返回假数据，不得抛非 MetadataError 异常）")
print("=" * 78)

# 2.1 文件不存在
expect_error(
    "不存在的路径",
    lambda: Metadata(r"C:\definitely\not\here\nope.dll"),
)

# 2.2 完全不是 PE
tmp = Path(tempfile.gettempdir()) / "gamemod_not_pe.bin"
tmp.write_bytes(b"this is definitely not a PE file" * 100)
expect_error("非 PE 文件", lambda: Metadata(tmp))

# 2.3 PE 但不是托管程序集（截断的 MZ 头）
tmp2 = Path(tempfile.gettempdir()) / "gamemod_bad_pe.bin"
tmp2.write_bytes(b"MZ" + b"\x00" * 2 + b"garbage" * 50)
expect_error("损坏的 PE（MZ 头后无有效 PE 签名）", lambda: Metadata(tmp2))

# 2.4 真实的原生 DLL（IL2CPP 的 GameAssembly.dll）—— 非托管，应清晰拒绝
# 样本位置由 samples.py 提供：优先用 IL2CPP 样本自带的 GameAssembly.dll，
# 拿不到再退回在样本根目录下递归找任意一个。
native = None
_il2cpp = samples.find_il2cpp()
if _il2cpp is not None and (_il2cpp / "GameAssembly.dll").is_file():
    native = _il2cpp / "GameAssembly.dll"
else:
    for _root in samples.SAMPLE_ROOTS:
        hits = sorted(_root.rglob("GameAssembly.dll"))[:1]
        if hits:
            native = hits[0]
            break
if native is None:
    samples.require(None, "任意一个 IL2CPP 的 GameAssembly.dll（原生非托管样本）")
    print("  [SKIP] 未找到 GameAssembly.dll，跳过「原生 DLL 应被拒绝」这一项")
else:
    print(f"  （原生非托管样本：{native}，{native.stat().st_size:,} 字节）")
    expect_error(f"原生非托管 DLL {native.name}", lambda c=native: Metadata(c))

print()
print("=" * 78)
print("3. 截断真实程序集（文件被切断时应清晰失败）")
print("=" * 78)
real = None
_mono = samples.find_mono()
if _mono is not None:
    real = _mono / "Archon Soul_Data" / "Managed" / "Assembly-CSharp.dll"
if real is None or not real.is_file():
    samples.require(real, "Mono 程序集 Archon Soul（截断样本的真值来源）")
    print("  [SKIP] 真实样本不存在")
else:
    raw = real.read_bytes()

    # 先正常解析一次，**实测**元数据在文件里的真实结束位置，
    # 再据此构造「切进元数据内部」的截断样本。
    # 不能想当然地按百分比切：98% 处其实已经在元数据之后了，
    # 那种情况下解析成功是**正确**行为（元数据完整无损）。
    good = Metadata(real)
    md_end = good.metadata_offset + good.metadata_size
    print(f"  元数据区间: 0x{good.metadata_offset:X} .. 0x{md_end:X} "
          f"（文件共 {len(raw):,} 字节）")
    print(f"  注意：元数据结束于文件的 {md_end / len(raw):.2%} 处，")
    print("        所以「截断到 98%」时元数据仍然完整——那种情况解析成功是对的。")

    # 3.1 明确切进元数据内部 → 必须失败
    md_len = md_end - good.metadata_offset
    for delta, label in ((1000, "元数据末尾前 1000 字节"),
                         (md_len // 2, "元数据中点")):
        cut = Path(tempfile.gettempdir()) / f"gamemod_cut_md_{delta}.dll"
        cut.write_bytes(raw[: md_end - delta])
        expect_error(f"截断在{label}（0x{md_end - delta:X}）", lambda c=cut: Metadata(c))

    # 3.2 元数据之后截断 → 元数据完整，解析成功是正确的
    cut_ok = Path(tempfile.gettempdir()) / "gamemod_cut_after_md.dll"
    cut_ok.write_bytes(raw[: len(raw) - 1000])
    try:
        Metadata(cut_ok)
        print(f"  [OK]   元数据之后截断（元数据完整）-> 解析成功，符合预期")
        results.append(("元数据之后截断仍可解析", True))
    except Exception as exc:  # noqa: BLE001
        print(f"  [BAD]  元数据之后截断 -> 不该失败却失败: {type(exc).__name__}: {exc}")
        results.append(("元数据之后截断仍可解析", False))

print()
print("=" * 78)
print("4. 自造的最小合法程序集能正常解析（防止「只在样本上碰巧对」）")
print("=" * 78)
print("  [SKIP] 构造最小合法程序集需要 IL 汇编器（非标准库能力），")
print("         本项不在脚本内伪造——已由两个独立的真实样本覆盖。")

print()
print("=" * 78)
print("汇总")
print("=" * 78)
bad = [label for label, ok in results if not ok]
print(f"  检查项: {len(results)}   失败: {len(bad)}")
for label in bad:
    print(f"    - {label}")
sys.exit(1 if bad else 0)
