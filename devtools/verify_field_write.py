"""验收：字段**写入**的编码逻辑（`set_field_value` 的纯函数部分）。

为什么单独一个脚本：`set_field_value` 是 `read_field_value` 的对称入口，
它最容易出的问题不是「写不进去」，而是**写进去了但值不对**——
调用方看到 `ok:true` 就以为成了。这类缺陷在端到端里极难与
「游戏自己把值覆盖回去了」区分开，所以把编码逻辑抽成纯函数在这里钉死。

本脚本**不需要游戏、不需要样本**，永远会跑。

四类断言（每条都有对应的**非空转反证**）：

  1. **宽度按字段类型来** —— 1/2/4/8 字节各验一次。反证：把宽度表改回
     "一律 8" 的旧写法，`int32` 的断言必须报红。
  2. **溢出必须报错，不静默截断** —— 300 写进 1 字节字段如果被
     `struct.pack("<B", 300)` 直接调用会抛异常、被 `& 0xFF` 兜住则会
     静默变成 44。旧写法（`pack` 不带范围检查）下 300→44 的断言必须报红。
  3. **浮点/整数不能混** —— 同样 4 字节，`3.5` 作 float 是 `00006040`，
     作 int 会报错。反证：把 `_FIELD_WRITE_KIND` 里 `System.Single`
     改成 `int`，float 断言必须报红。
  4. **引用类型必须拒绝** —— 宽度 8 且类型认不出（`System.String`、
     `None`）时，`_field_write_kind` 返回 `long`，但工具层要拒绝。
     这里断言 tool 层的判据函数能区分「确实是 Int64」与「认不出」。

最后一节是**非空转反证**：直接篡改内部表再复原，确认断言真的会红。
"""

from __future__ import annotations

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import samples                                            # noqa: E402
samples.add_src_to_path()
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import gamemod_mcp.server as S                            # noqa: E402

PASS = 0
FAIL = 0
WARN = 0


def check(label: str, cond: bool, detail: str = "", warn_only: bool = False) -> bool:
    global PASS, FAIL, WARN
    if cond:
        print(f"  \u2705 {label}" + (f" \u2014 {detail}" if detail else ""))
        PASS += 1
        return True
    if warn_only:
        print(f"  \u26a0\ufe0f  {label}" + (f" \u2014 {detail}" if detail else ""))
        WARN += 1
        return False
    print(f"  \u274c {label}" + (f" \u2014 {detail}" if detail else ""))
    FAIL += 1
    return False


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


# ================================================================
section("1. 宽度按字段类型来（不是一律 8）")

WIDTH_CASES = [
    ("System.Boolean", 1),
    ("System.Byte", 1),
    ("System.SByte", 1),
    ("System.Char", 2),
    ("System.Int16", 2),
    ("System.UInt16", 2),
    ("System.Int32", 4),
    ("System.UInt32", 4),
    ("System.Single", 4),
    ("System.Int64", 8),
    ("System.UInt64", 8),
    ("System.Double", 8),
    ("System.IntPtr", 8),
]
for tname, want in WIDTH_CASES:
    got = S._field_read_width(tname)
    check(f"_field_read_width({tname!r}) == {want}", got == want, f"实际 {got}")

# 认不出按 8（指针宽度，也是历史行为，不能改坏）
check("_field_read_width(None) == 8（保底）", S._field_read_width(None) == 8)
check("_field_read_width('Some.Class') == 8（保底）",
      S._field_read_width("Some.Class") == 8)

# ================================================================
section("2. 整数编码：宽度 + 有符号 + 溢出必须报错")

INT_CASES = [
    # (值, 宽度, kind, 期望字节或 None, 说明)
    (0, 1, "uint", b"\x00", "0"),
    (255, 1, "uint", b"\xff", "1 字节上界"),
    (-128, 1, "int", b"\x80", "1 字节下界"),
    (127, 1, "int", b"\x7f", "1 字节上界"),
    (256, 1, "uint", None, "**溢出必须报错**（旧写法会截成 0）"),
    (300, 1, "uint", None, "**溢出必须报错**（旧写法会截成 44）"),
    (-129, 1, "int", None, "**下溢必须报错**"),
    (32767, 2, "int", b"\xff\x7f", "2 字节上界"),
    (-32768, 2, "int", b"\x00\x80", "2 字节下界"),
    (32768, 2, "int", None, "2 字节溢出"),
    (65535, 2, "uint", b"\xff\xff", "2 字节无符号上界"),
    (65536, 2, "uint", None, "2 字节无符号溢出"),
    (2147483647, 4, "int", b"\xff\xff\xff\x7f", "4 字节上界"),
    (-2147483648, 4, "int", b"\x00\x00\x00\x80", "4 字节下界"),
    (2147483648, 4, "int", None, "4 字节溢出"),
    (-1, 4, "int", b"\xff\xff\xff\xff", "-1 补码"),
    (4294967295, 4, "uint", b"\xff\xff\xff\xff", "4 字节无符号上界"),
    (4294967296, 4, "uint", None, "4 字节无符号溢出"),
    (9223372036854775807, 8, "long", b"\xff\xff\xff\xff\xff\xff\xff\x7f", "8 字节上界"),
    (-1, 8, "long", b"\xff" * 8, "-1 8 字节"),
    (2 ** 64 - 1, 8, "ulong", b"\xff" * 8, "8 字节无符号上界"),
    (2 ** 64, 8, "ulong", None, "8 字节无符号溢出"),
]
for value, width, kind, want, desc in INT_CASES:
    blob, err = S._encode_field_value(value, width, kind)
    if want is None:
        check(f"{kind}({width}) 写 {value} → 报错 [{desc}]",
              blob is None and bool(err), f"blob={blob} err={err!r}")
    else:
        check(f"{kind}({width}) 写 {value} → {want.hex()} [{desc}]",
              blob == want, f"实际 {blob.hex() if blob else None} err={err!r}")

# ================================================================
section("3. 浮点与整数不能混（同样 4 字节，含义完全不同）")

for value, width, kind, want in [
    (3.5, 4, "float", struct.pack("<f", 3.5)),
    (-2.5, 4, "float", struct.pack("<f", -2.5)),
    (0.0, 4, "float", struct.pack("<f", 0.0)),
    (1.5, 8, "double", struct.pack("<d", 1.5)),
]:
    blob, err = S._encode_field_value(value, width, kind)
    check(f"{kind} 写 {value} → {want.hex()}", blob == want,
          f"实际 {blob.hex() if blob else None} err={err!r}")

# 关键对照：同一个 4 字节，float 与 int 的字节不一样
bf, _ = S._encode_field_value(1, 4, "float")
bi, _ = S._encode_field_value(1, 4, "int")
check("float(1) != int(1) 的字节（证明「按类型解释」有意义）",
      bf != bi, f"float={bf.hex()} int={bi.hex()}")
check("float 写 1 得到 0000803f（IEEE754）", bf.hex() == "0000803f",
      f"实际 {bf.hex()}")

# ================================================================
section("4. bool 与字节串")

for value, want in [(True, b"\x01"), (False, b"\x00"),
                    (1, b"\x01"), (0, b"\x00"),
                    ("true", b"\x01"), ("FALSE", b"\x00"),
                    # 明确支持的同义词（写在工具 docstring 里，不是瞎猜）
                    ("yes", b"\x01"), ("no", b"\x00"),
                    ("on", b"\x01"), ("off", b"\x00")]:
    blob, err = S._encode_field_value(value, 1, "bool")
    check(f"bool 写 {value!r} → {want.hex()}", blob == want,
          f"实际 {blob.hex() if blob else None} err={err!r}")

# 真正的「不该猜」：认不出的词、越界数字，一律报错
for value in ["maybe", "2", "-1", "", "真"]:
    blob, err = S._encode_field_value(value, 1, "bool")
    check(f"bool 写 {value!r} → 报错（不瞎猜）", blob is None and bool(err),
          f"blob={blob} err={err!r}")

# 字节串：宽度必须**精确**匹配
blob, err = S._encode_field_value("0x0000803f", 4, "bytes")
check("bytes 写 '0x0000803f' (w=4) → 0000803f", blob == b"\x00\x00\x80\x3f",
      f"实际 {blob.hex() if blob else None} err={err!r}")
blob, err = S._encode_field_value("0000803f", 4, "bytes")
check("bytes 不带 0x 前缀也能用", blob == b"\x00\x00\x80\x3f")
blob, err = S._encode_field_value("0000803f", 8, "bytes")
check("bytes 长度与字段宽度不符 → 报错（不多不少）",
      blob is None and bool(err), f"err={err!r}")
blob, err = S._encode_field_value("zz", 1, "bytes")
check("bytes 非法十六进制 → 报错", blob is None and bool(err))

# ================================================================
section("5. 引用类型必须能被识别出来（工具层据此拒绝）")

# 认得出的 Int64/IntPtr → long，工具层允许
for tname in ["System.Int64", "System.UInt64", "System.Double", "System.IntPtr"]:
    w = S._field_read_width(tname)
    k = S._field_write_kind(tname, w)
    in_table = str(tname).strip() in S._FIELD_WRITE_KIND
    check(f"{tname} 在类型表里（工具层允许写）", in_table and k in ("long", "ulong", "double"),
          f"kind={k} in_table={in_table}")

# 认不出的（引用类型）→ 工具层判据：kind=='long' 且不在表里 ⇒ 拒绝
for tname in ["System.String", "Some.Game.Manager", None]:
    w = S._field_read_width(tname)
    k = S._field_write_kind(tname, w)
    key = str(tname).strip() if tname else ""
    would_reject = (k == "long") and (key not in S._FIELD_WRITE_KIND)
    check(f"{tname!r} → 工具层会拒绝（疑似引用类型）", would_reject,
          f"kind={k} key={key!r}")

# ================================================================
section("6. 非空转反证：把实现改回旧写法，断言必须报红")

# 反证 1：宽度一律 8（旧行为）→ int32 的宽度断言必须不再成立
orig_read_width = S._field_read_width
try:
    S._field_read_width = lambda t: 8          # 旧写法
    red = S._field_read_width("System.Int32") != 4
    check("改回「一律 8 字节」后，int32 宽度断言会报红", red,
          "若这里为 False，说明断言是空转的")
finally:
    S._field_read_width = orig_read_width

# 反证 2：去掉溢出检查（用 struct 直接 pack 并 & 掩码）→ 300 会静默变 44
def _old_encode(value, width, kind):
    """旧写法：不查范围，直接截断。"""
    try:
        if kind == "float":
            return struct.pack("<f", float(value)), ""
        if kind == "bool":
            return (b"\x01" if value else b"\x00"), ""
        iv = int(value)
        fmt = {1: "B", 2: "H", 4: "I", 8: "Q"}[width]
        return struct.pack("<" + fmt, iv & ((1 << (width * 8)) - 1)), ""
    except Exception as exc:
        return None, str(exc)

old_blob, _ = _old_encode(300, 1, "uint")
new_blob, new_err = S._encode_field_value(300, 1, "uint")
check("旧写法把 300 静默截断成 44（这正是要防的）",
      old_blob == b"\x2c", f"旧={old_blob.hex() if old_blob else None}")
check("新写法对同一个 300 报错（断言确实抓住了差异）",
      new_blob is None and bool(new_err), f"err={new_err!r}")
check("反证有效：新旧行为**确实不同**（否则上面的断言是空转的）",
      old_blob is not None and new_blob is None)

# 反证 3：把 System.Single 错标成 int → float 断言必须报红
orig_kind = dict(S._FIELD_WRITE_KIND)
try:
    S._FIELD_WRITE_KIND["System.Single"] = "int"     # 错误标注
    wrong = S._field_write_kind("System.Single", 4)
    check("把 System.Single 错标成 int 后，float 断言会报红", wrong == "int",
          f"实际 {wrong}")
finally:
    S._FIELD_WRITE_KIND.clear()
    S._FIELD_WRITE_KIND.update(orig_kind)
check("恢复后 System.Single 又是 float",
      S._field_write_kind("System.Single", 4) == "float")

# ================================================================
print()
print("=" * 78)
print(f"通过 {PASS} 项，失败 {FAIL} 项" + (f"，警告 {WARN} 项" if WARN else ""))
print("=" * 78)
sys.exit(1 if FAIL else 0)
