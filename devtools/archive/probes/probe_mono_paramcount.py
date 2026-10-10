"""临时探针：弄清「方法签名参数个数」与「Param 表区间行数」为何有 7/3 处不符。

不猜。把每一条不符的方法的原始事实全部打印出来：
    · 方法名 / token / 所属类型
    · 签名 blob 原始 hex、调用约定、ParamCount
    · Param 表区间 [start, end) 的每一行（Sequence / Flags / Name）
    · 该区间内 Sequence != 0 的行数

判定标准（ECMA-335 II.22.33）：
    Param 表**只**为「有名字 / 有特性 / 有默认值」的参数建行，
    没有这些信息的参数可以没有行。因此「签名参数个数 > Param 行数」
    在规范上是**合法**的，不算解析错误——本探针用来确认真实数据是否如此。
"""

import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\Administrator\Documents\deepseek-harness\默认工作区\GameModMCP\src")

from pathlib import Path

from gamemod_mcp.engine.mono_meta import Metadata

SAMPLES = [
    Path(r"D:\Projects\Archon Soul\Archon Soul_Data\Managed\ArchonSoul.dll"),
    Path(r"D:\Projects\Archon Soul\Archon Soul_Data\Managed\Assembly-CSharp.dll"),
    Path(r"D:\Projects\Kinny and the Cosmic Cauldron\Kinny and the Cosmic Cauldron_Data\Managed\Assembly-CSharp.dll"),
]

# 调用约定 bit（ECMA-335 II.23.2.3）
CC_NAMES = {
    0x00: "DEFAULT", 0x01: "C", 0x02: "STDCALL", 0x03: "THIS", 0x04: "FASTCALL",
    0x05: "VARARG", 0x10: "GENERIC", 0x20: "HASTHIS", 0x40: "EXPLICITTHIS",
}


def describe_cc(calling: int) -> str:
    bits = []
    if calling & 0x08:
        bits.append("PROPERTY")
    if calling & 0x10:
        bits.append("GENERIC")
    if calling & 0x20:
        bits.append("HASTHIS")
    if calling & 0x40:
        bits.append("EXPLICITTHIS")
    low = calling & 0x0F
    bits.append(f"low=0x{low:X}({CC_NAMES.get(low, '?')})")
    return "|".join(bits)


for path in SAMPLES:
    if not path.is_file():
        print(f"[缺失] {path}")
        continue
    print("=" * 78)
    print(f"{path}")
    meta = Metadata(path)

    total = 0
    mismatches = []
    for m in meta.methods:
        actual = sum(
            1 for p in meta.parameters[
                m.parameter_start : m.parameter_start + m.parameter_count
            ] if p.sequence != 0
        )
        sig_n = m.signature_parameter_count
        total += 1
        if sig_n >= 0 and actual != sig_n:
            mismatches.append((m, sig_n, actual))

    print(f"方法总数 {total:,}，签名与 Param 区间不符 {len(mismatches)} 处")
    for m, sig_n, actual in mismatches:
        owner = meta.owner_of_method(m.index)
        blob = meta.read_blob(m.signature) or b""
        print("-" * 74)
        print(f"  方法      : {owner.nested_name if owner else '?'}.{m.name}")
        print(f"  token     : {m.runtime_token}   rva=0x{m.rva:X}  flags=0x{m.flags:04X}")
        print(f"  签名 blob : {blob.hex(' ')}")
        print(f"  调用约定  : 0x{blob[0]:02X} -> {describe_cc(blob[0])}")
        print(f"  签名声明参数个数 = {sig_n}   Param 区间内非返回值行数 = {actual}")
        print(f"  Param 区间: [{m.parameter_start}, {m.parameter_start + m.parameter_count})")
        for p in meta.parameters[
            m.parameter_start : m.parameter_start + m.parameter_count
        ]:
            print(f"      idx={p.index:>5} seq={p.sequence:<3} flags=0x{p.flags:04X} name={p.name!r}")

    # 统计：不符的方法里调用约定是否含 VARARG
    vararg = sum(1 for m, _, _ in mismatches if (meta.read_blob(m.signature) or b"\0")[0] & 0x0F == 0x05)
    print()
    print(f"  其中调用约定为 VARARG 的：{vararg} / {len(mismatches)}")
    print(f"  其中 Param 区间行数少于签名参数："
          f"{sum(1 for _, s, a in mismatches if a < s)} / {len(mismatches)}")
    print(f"  其中 Param 区间行数多于签名参数："
          f"{sum(1 for _, s, a in mismatches if a > s)} / {len(mismatches)}")
    print()
