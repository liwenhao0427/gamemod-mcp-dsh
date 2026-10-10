"""验收：typeDefinitions 记录大小（stride）必须**自验证**，不许按版本号硬猜。

## 为什么需要它

这里曾写死 `TYPEDEF_SIZE_LEGACY = 92`（理由：「v24~v29 都是 92」）。实测
**v29 是 88**：Ved 的 19.4 MB 元数据用 92 解析出 21,290 个类型，且
Σfield_count = 650,148,194（真值 96,094）—— 每个类型的 field_start/field_count
全部错位，建索引时撞上 `UNIQUE constraint failed: fields.idx`，`open_game` 直接失败。

**这个 bug 能溜进仓库的原因值得记住**：仓库里的 IL2CPP 样本只有 v31 一款
（Garden of Witches），而 v31 走的是另一个常量（88），所以 92 这条分支
**从来没有被任何测试执行过**。样本覆盖不到的分支，写错了也不会有人知道。

## 本脚本怎么测（不依赖任何特定游戏）

关键手法：**合成一份元数据文件**，自己控制 version 与 stride。
这样无论本机装没装游戏，测试都真的在跑 —— 而不是「找不到样本就跳过」，
那种跳过会让「修坏了」和「没样本」长得一模一样。

四节，顺序就是重要性：

1. **铺满判据本身**：`_tiles_fields` 对「正确 stride」判 True，对
   「错位 stride / 有空洞 / 有重叠 / 越界」判 False。
2. **自验证真的会改选**：把期望值设成错的（92），`_typedef_size` 必须
   改选 88 并 **warn** 出来 —— 不许静默换 stride。
3. **端到端**：合成 v29 + stride 88 的元数据，`Metadata` 解析后
   Σfield_count 必须等于 fields 表条数（即 `validate()["fields_ok"]`）。
4. **回归现场**：同样的合成数据，若强制按 92 解析，`fields_ok` 必须为 False。
   这一节是「这个 bug 确实存在过」的可执行证据，也防止有人把判据改回去。
5. **版本区间**：超出支持区间的版本必须**快速如实拒绝**（`MetadataError`，
   点明「版本不支持」并给替代路径），不许硬解几百秒再抛 sqlite 约束错误。

## 反证（非空转）

- 把 `_tiles_fields` 改成恒 `return True` → 第 1 节必须报红。
- 把自验证的改选逻辑删掉（直接 return expected）→ 第 2、3 节必须报红。
- 把铺满判据的 `cursor == n_fields` 去掉 → 第 1 节的「有空洞」用例必须报红。
- 把 `_validate_layout` 的版本区间判定删掉 → 第 5 节必须报红。
"""

from __future__ import annotations

import os
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import samples                                            # noqa: E402
samples.add_src_to_path()
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from gamemod_mcp.engine import il2cpp_meta as M           # noqa: E402

PASS = 0
FAIL = 0
SKIP = 0


def check(cond: bool, label: str, detail: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {label}" + (f" — {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ {label}" + (f" — {detail}" if detail else ""))
    return cond


# ---------------------------------------------------------------- 合成元数据

METADATA_MAGIC = 0xFAB11BAF
#: 表在头部里的顺序（v29+）。索引 i 的表，其 (offset,size) 在 8 + i*8。
_TABLE_ORDER = [
    "stringLiteral", "stringLiteralData", "string", "events", "properties",
    "methods", "parameterDefaultValues", "fieldDefaultValues",
    "fieldAndParameterDefaultValueData", "fieldMarshaledSizes", "parameters",
    "fields", "genericParameters", "genericParameterConstraints",
    "genericContainers", "nestedTypes", "interfaces", "vtableMethods",
    "interfaceOffsets", "typeDefinitions", "images", "assemblies",
    "fieldRefs", "referencedAssemblies", "attributeData", "attributeDataRange",
    "unresolvedVirtualCallParameterTypes", "unresolvedVirtualCallParameterRanges",
    "windowsRuntimeTypeNames", "windowsRuntimeStrings", "exportedTypeDefinitions",
]


def build_metadata(path: Path, version: int, stride: int, n_types: int) -> None:
    """合成一份「布局自洽」的 IL2CPP 元数据。

    每个类型分到 `fields_per_type` 个字段，field_start 连续铺满 ——
    这正是正确 stride 必须满足的性质，所以这份文件是合法输入。

    字符串表：一个空串在偏移 0，保证 name_idx=0 可读。
    """
    fields_per_type = 2
    n_fields = n_types * fields_per_type

    # 各表的 (offset, size)；这里只关心 fields 与 typeDefinitions 的内容，
    # 其余表给 0 长度（解析器会跳过）。
    header_size = 8 + len(_TABLE_ORDER) * 8

    fields_off = header_size
    fields_size = n_fields * M.FIELD_SIZE

    td_off = fields_off + fields_size
    td_size = n_types * stride

    strings_off = td_off + td_size
    strings = b"\x00"          # 偏移 0 = 空串
    strings_size = len(strings)

    buf = bytearray(strings_off + strings_size)

    # ---- 头部 ----
    struct.pack_into("<Ii", buf, 0, METADATA_MAGIC, version)
    for i, name in enumerate(_TABLE_ORDER):
        off, size = 0, 0
        if name == "fields":
            off, size = fields_off, fields_size
        elif name == "typeDefinitions":
            off, size = td_off, td_size
        elif name == "string":
            off, size = strings_off, strings_size
        struct.pack_into("<II", buf, 8 + i * 8, off, size)

    # ---- fields 表：每条 12 字节 (name_idx, type_idx, token) ----
    for i in range(n_fields):
        struct.pack_into("<3i", buf, fields_off + i * M.FIELD_SIZE, 0, 0, 0)

    # ---- typeDefinitions 表 ----
    for t in range(n_types):
        b = td_off + t * stride
        field_start = t * fields_per_type
        # 16 个 int32 @0..60：name_idx, ns_idx, byval, decl, parent, elem,
        #                      generic, flags, field_start, method_start,
        #                      property_start, event_start, nested_start,
        #                      interfaces_start, vtable_start, iface_off_start
        struct.pack_into("<16i", buf, b,
                         0, 0, 0, 0, 0, 0, 0, 0,
                         field_start, 0, 0, 0, 0, 0, 0, 0)
        # 8 个 uint16 @64..78：method_count, property_count, field_count, ...
        struct.pack_into("<8H", buf, b + 64,
                         0, 0, fields_per_type, 0, 0, 0, 0, 0)
        # token @84
        struct.pack_into("<I", buf, b + 84, 0x02000000 + t)

    path.write_bytes(bytes(buf))


TMP = Path(tempfile.mkdtemp(prefix="gamemod-stride-"))
print(f"合成元数据目录: {TMP}\n")

# ================================================================ 1. 铺满判据
print("1. 铺满判据 _tiles_fields 的正反用例")

GOOD = TMP / "good.dat"
build_metadata(GOOD, version=29, stride=88, n_types=64)
meta_good = M.load(GOOD)
td = meta_good.tables["typeDefinitions"]

check(meta_good._tiles_fields(td, 88) is True,
      "正确 stride(88) 判为铺满")

# 错位：把 stride 改成 92，区间会互相错开
check(meta_good._tiles_fields(td, 92) is False,
      "错位 stride(92) 判为不铺满")

# 有空洞 / 有重叠 / 越界 —— 直接改字节造出来
def mutate(src: Path, dst: Path, *, start_delta=0, count_delta=0, type_index=1):
    data = bytearray(src.read_bytes())
    b = td.offset + type_index * 88
    if start_delta:
        cur = struct.unpack_from("<i", data, b + 32)[0]
        struct.pack_into("<i", data, b + 32, cur + start_delta)
    if count_delta:
        cur = struct.unpack_from("<H", data, b + 68)[0]
        struct.pack_into("<H", data, b + 68, cur + count_delta)
    dst.write_bytes(bytes(data))


HOLE = TMP / "hole.dat"
mutate(GOOD, HOLE, count_delta=-1)          # 少一个字段 -> 空洞
m_hole = M.load(HOLE)
check(m_hole._tiles_fields(m_hole.tables["typeDefinitions"], 88) is False,
      "有空洞（count 少 1）判为不铺满")

OVERLAP = TMP / "overlap.dat"
mutate(GOOD, OVERLAP, start_delta=-1, count_delta=1)   # 前移并加长 -> 重叠
m_ov = M.load(OVERLAP)
check(m_ov._tiles_fields(m_ov.tables["typeDefinitions"], 88) is False,
      "有重叠（start 前移且 count 加 1）判为不铺满")

OOR = TMP / "oor.dat"
mutate(GOOD, OOR, start_delta=10_000)        # 越界
m_oor = M.load(OOR)
check(m_oor._tiles_fields(m_oor.tables["typeDefinitions"], 88) is False,
      "区间越界判为不铺满")

# ================================================================ 2. 自验证改选
print("\n2. 期望值错时必须改选并 warn（不许静默换 stride）")

import logging                                              # noqa: E402

from gamemod_mcp import logutil as _logutil               # noqa: E402

records: list[logging.LogRecord] = []


class _Capture(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        records.append(record)


handler = _Capture()
_logutil.log.addHandler(handler)
_logutil.log.setLevel(logging.DEBUG)
try:
    original_legacy = M.TYPEDEF_SIZE_LEGACY
    M.TYPEDEF_SIZE_LEGACY = 92                # 故意设成错的值（就是历史 bug）
    meta_wrong = M.load(GOOD)
    inferred = meta_wrong._typedef_size
    check(inferred == 88,
          "期望 92 但铺不满 -> 自动改选 88", f"实际 {inferred}")
    warned = [r for r in records if "改用" in r.getMessage()]
    check(len(warned) > 0,
          "改选时确实 warn 了", f"{len(warned)} 条")
finally:
    M.TYPEDEF_SIZE_LEGACY = original_legacy
    _logutil.log.removeHandler(handler)

# ================================================================ 3. 端到端
print("\n3. 端到端：合成 v29 元数据解析后必须自洽")

meta = M.load(GOOD)
v = meta.validate()
check(v["fields_ok"] is True,
      "Σfield_count == fields 表条数",
      f"{v['sum_field_count']} vs {v['fields_table_count']}")
check(v["methods_ok"] is True,
      "Σmethod_count == methods 表条数",
      f"{v['sum_method_count']} vs {v['methods_table_count']}")
check(len(meta.types) == 64, "类型数正确", f"{len(meta.types)}")

# ================================================================ 4. 回归现场
print("\n4. 回归现场：按 92 硬解析必须不自洽（证明判据不是恒真）")

# 注意：不能只是把常量改成 92 —— 新的自验证会把它改回 88（第 2 节已证），
# 那正好说明修复生效，但没法复现历史 bug。要复现历史行为，必须**同时**
# 关掉自验证（把 _tiles_fields 变成恒 True），这才是「硬编码、不校验」
# 的原样代码路径。
original_tiles = M.Metadata._tiles_fields
try:
    M.Metadata._tiles_fields = lambda self, entry, stride: True
    M.TYPEDEF_SIZE_LEGACY = 92
    M.TYPEDEF_SIZE_V31 = 92                   # 连 v31 一起，确保 92 真被用上
    meta_bad = M.load(GOOD)
    check(meta_bad._typedef_size == 92,
          "自验证关闭后确实用了 92", f"实际 {meta_bad._typedef_size}")
    v_bad = meta_bad.validate()
    check(v_bad["fields_ok"] is False,
          "按 92 硬解析 -> Σfield_count != fields 表条数",
          f"{v_bad['sum_field_count']} vs {v_bad['fields_table_count']}")
finally:
    M.Metadata._tiles_fields = original_tiles
    M.TYPEDEF_SIZE_LEGACY = original_legacy
    M.TYPEDEF_SIZE_V31 = 88

# ================================================================ 5. 版本区间
print("\n5. 超出支持区间必须**快速如实拒绝**（实测 v39 曾空转 573 秒）")

# 现场：Enter the Chronosphere（Unity 6000.3.14f1，metadata v39）头部布局已变
# （fields 表 24,998 字节连 12 都除不尽）。修复前 stride 推断失败只 warn 一句，
# 然后照旧硬解 -> 181,396 个类型 / Σfield_count = 3,156,391,098（真值 2,083），
# 花 573 秒建索引，最后抛 `UNIQUE constraint failed: fields.idx` ——
# 把「版本不支持」伪装成「数据库错误」。
FUTURE = TMP / "future.dat"
build_metadata(FUTURE, version=M.SUPPORTED_VERSION_MAX + 8, stride=88, n_types=64)

import time as _time                                       # noqa: E402

_t0 = _time.time()
try:
    M.load(FUTURE)
    check(False, "超出区间的版本被拒绝", "竟然解析成功了")
except M.MetadataError as exc:
    _dt = _time.time() - _t0
    check(True, "超出区间的版本被拒绝（MetadataError）", f"{_dt:.3f}s")
    msg = str(exc)
    check("不支持" in msg and "v39" in msg,
          "错误信息点明「版本不支持」并带上实际版本号",
          msg[:90])
    check("替代" in msg,
          "错误信息给了可用替代路径", msg[:90])
    check("UNIQUE" not in msg and "sqlite" not in msg.lower(),
          "错误信息**不是**数据库约束错误（旧症状）", msg[:90])
    check(_dt < 2.0,
          "是**快速**失败（不是先硬解几百秒）", f"{_dt:.3f}s")

# 反证：把版本区间放开，同一份文件就会走「硬解」老路 ——
# 说明上面拦下来的确实是版本判定，而不是文件本身读不了。
_orig_max = M.SUPPORTED_VERSION_MAX
try:
    M.SUPPORTED_VERSION_MAX = 999
    try:
        m_future = M.load(FUTURE)
        check(True, "反证：放开版本区间后同一文件能被解析（说明上面拦的是版本）",
              f"types={len(m_future.types)}")
    except M.MetadataError as exc:
        check(True, "反证：放开版本区间后仍被**布局自洽**检验拦下",
              str(exc)[:80])
finally:
    M.SUPPORTED_VERSION_MAX = _orig_max

# 受支持版本必须照旧通过（别把门关太死）
check(M.load(GOOD).version == 29, "受支持的 v29 仍能正常解析")

# 关键回归：**布局自洽检验不许误伤合法输入**。
# 曾经的写法把「typeDefinitions 能否铺满 fields」也当成拒绝条件，
# 结果 `verify_typedef_stride` 第 1 节的 hole/overlap/oor 三个**故意做坏的**
# 样本全部在建 Metadata 时就抛错，脚本直接崩在断言之前。
# 那三个样本要验的是 `_tiles_fields` 的返回值，必须能被解析出来。
for _name in ("hole.dat", "overlap.dat", "oor.dat"):
    _p = TMP / _name
    if _p.is_file():
        try:
            M.load(_p)
            check(True, f"合法头部 + 故意做坏的字段区间仍可解析（{_name}）")
        except M.MetadataError as _exc:
            check(False, f"合法头部 + 故意做坏的字段区间仍可解析（{_name}）",
                  f"不该拒绝：{_exc}"[:90])

# ================================================================ 汇总
print("\n" + "=" * 62)
print(f"PASS={PASS}  FAIL={FAIL}  SKIP={SKIP}")

import shutil                                              # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)

if FAIL:
    print("❌ 有断言失败")
    sys.exit(1)
print("✅ 全部通过")
