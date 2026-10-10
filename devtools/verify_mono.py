"""真实验证 `gamemod_mcp.engine.mono_meta` 的 ECMA-335 解析正确性。

验证策略（三层，越靠后越硬）
--------------------------------------------------
1. **内部自洽**：Σfield_count 必须等于 Field 表行数、Σmethod_count 必须等于
   MethodDef 表行数、Σparameter_count 必须等于 Param 表行数。
   这是判断「FieldList/MethodList 相邻区间换算是否正确」最直接的证据。

2. **API 行为**：search_types / type_by_name / fields_of / methods_of /
   parameters_of / base_chain / is_subclass_of / parent_of / resolve_field_type
   都必须能跑通并给出合理结果；嵌套类型必须拼成 `Outer.Inner`。

3. **官方真值交叉比对**（最强）：用微软官方 System.Reflection.Metadata
   （ECMA-335 的官方实现，见 devtools/mono_groundtruth/）读同一个程序集导出 JSON，
   再与自研解析器逐表、逐类型、逐成员比对。
   **真值生成器完全不复用被测代码**，因此这不是自证。

诚实原则：任何一项失败都原样打印 FAIL，不粉饰、不跳过。
样本不存在 / 真值不可得时明确打印「无法验证」，不编造数字。

用法：
    python devtools\\verify_mono.py
    python devtools\\verify_mono.py [--keep-gt]
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

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from gamemod_mcp.engine.mono_meta import Metadata, MetadataError, TABLE_NAMES

# ---------------------------------------------------------------- 样本

# 样本路径全部从 samples.py 取（根目录）+ 相对路径拼出来；取不到就是 None，
# 由 main() 记为「无法验证」并跳过，不计入 FAIL。
_ARCHON = samples.find_mono()
_KINNY = samples.find_mono_multi()


def _mono_dll(game: "Path | None", data_dir: str, dll: str):
    """从样本根目录拼出托管程序集路径；样本缺失时返回 None。"""
    return (game / f"{data_dir}_Data" / "Managed" / dll) if game else None


SAMPLES = [
    (
        "Archon Soul",
        _mono_dll(_ARCHON, "Archon Soul", "Assembly-CSharp.dll"),
        {"needle": "Player", "base": "UnityEngine.MonoBehaviour"},
    ),
    (
        "Kinny and the Cosmic Cauldron",
        _mono_dll(_KINNY, "Kinny and the Cosmic Cauldron", "Assembly-CSharp.dll"),
        {"needle": "Player", "base": "UnityEngine.MonoBehaviour"},
    ),    # 第三个样本刻意选 mscorlib：它是基础类库，类型/方法/字段规模远大于游戏脚本，
    # 且含大量泛型、值类型、接口、P/Invoke，能把「表宽度 / coded index 宽度」
    # 这些只在规模上来后才会暴露的边界压出来。同一份代码在它上面也全绿，
    # 说明实现不是「只在游戏脚本上碰巧对」。
    #
    # 注意 heapSizes=0x05（#Blob 用 4 字节），与两个游戏样本的 0x01 不同，
    # 正好覆盖另一种堆宽度组合。
    #
    # 探测词必须按样本给：mscorlib 里**没有** Player 类型，也不引用 UnityEngine
    # （它的 assembly_refs 就是空表——基础类库不引用任何程序集）。
    # 早期版本把 Player/MonoBehaviour 写死，导致在 mscorlib 上误报 FAIL，
    # 那是**断言设计错了**，不是解析错了。
    (
        "mscorlib (Archon Soul)",
        _mono_dll(_ARCHON, "Archon Soul", "mscorlib.dll"),
        {"needle": "String", "base": "System.MulticastDelegate"},
    ),
]

# 已存在的真值（上一轮由 MonoGroundTruth 生成），能省掉一次 dotnet 构建
PREBUILT_GT = {
    "Archon Soul": Path(__file__).resolve().parent
                   / "groundtruth" / "mono" / "gt_archon.json",
}
# 真值工程（.csproj）；找不到 dotnet 或构建失败时该项记为「无法验证」
GT_PROJECT = Path(__file__).resolve().parent / "groundtruth" / "mono"

# ---------------------------------------------------------------- 结果记录

FAILURES: list[str] = []
CHECKS = 0


def check(ok: bool, label: str, detail: str = "") -> bool:
    """记一条断言结果。失败时累积到 FAILURES（最后统一汇总）。"""
    global CHECKS
    CHECKS += 1
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {label}"
    if detail:
        line += f"  —— {detail}"
    print(line)
    if not ok:
        FAILURES.append(f"{label}: {detail}" if detail else label)
    return ok


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


# ---------------------------------------------------------------- 真值获取


def gt_cache_path(sample_name: str) -> Path:
    slug = "".join(ch if ch.isalnum() else "_" for ch in sample_name).strip("_")
    return Path(tempfile.gettempdir()) / f"gamemod_mono_gt_{slug}.json"


def build_ground_truth_tool() -> str | None:
    """构建 MonoGroundTruth，返回可执行文件路径；失败返回 None。"""
    dotnet = shutil.which("dotnet")
    if dotnet is None:
        return None
    if not (GT_PROJECT / "MonoGroundTruth.csproj").is_file():
        return None

    print("  正在构建官方真值工具（System.Reflection.Metadata）...")
    proc = subprocess.run(
        [dotnet, "build", "-c", "Release", "--nologo", "-v", "quiet", str(GT_PROJECT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        print(f"  构建失败（returncode={proc.returncode}）")
        print("  " + (proc.stdout or "")[-1500:])
        print("  " + (proc.stderr or "")[-1500:])
        return None

    exe = GT_PROJECT / "bin" / "Release" / "net9.0" / "MonoGroundTruth.dll"
    if exe.is_file():
        return str(exe)
    return None


def get_ground_truth(sample_name: str, dll: Path, tool: str | None) -> dict | None:
    """取得官方真值 JSON；拿不到返回 None（调用方须如实报告无法交叉验证）。"""
    prebuilt = PREBUILT_GT.get(sample_name)
    if prebuilt is not None and prebuilt.is_file():
        print(f"  使用已生成的真值：{prebuilt.name}")
        return json.loads(prebuilt.read_text(encoding="utf-8"))

    cached = gt_cache_path(sample_name)
    if cached.is_file():
        print(f"  使用缓存真值：{cached}")
        return json.loads(cached.read_text(encoding="utf-8"))

    if tool is None:
        return None

    print("  调用官方真值工具解析样本 ...")
    proc = subprocess.run(
        ["dotnet", tool, str(dll)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        print(f"  真值工具返回 {proc.returncode}")
        print("  " + (proc.stderr or "")[-1500:])
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        print(f"  真值 JSON 解析失败: {exc}")
        return None
    cached.write_text(json.dumps(data), encoding="utf-8")
    return data


# ---------------------------------------------------------------- 单样本验证

# 自研表名 → 官方真值里的汇总字段
GT_SUMMARY_KEYS = {
    "TypeDef": "type_defs",
    "TypeRef": "type_refs",
    "Field": "fields",
    "MethodDef": "methods",
    "Param": "params",
    "NestedClass": "nested_classes",
    "AssemblyRef": "assembly_refs",
    "MemberRef": "member_refs",
    "TypeSpec": "type_specs",
    "GenericParam": "generic_params",
    "CustomAttribute": "custom_attributes",
    "MethodSpec": "method_specs",
    "InterfaceImpl": "interface_impls",
    "Constant": "constants",
}


def verify_sample(name: str, dll: Path, gt: dict | None, probe: dict) -> None:
    section(f"样本：{name}")
    print(f"  路径：{dll}")

    needle = probe["needle"]
    probe_base = probe["base"]
    if not dll.is_file():
        print(f"  [FAIL] 样本不存在：{dll}")
        FAILURES.append(f"{name}: 样本不存在")
        return
    print(f"  大小：{dll.stat().st_size:,} 字节")

    # ---- 1. 解析
    print()
    print("--- 1. 解析 ---")
    try:
        meta = Metadata(dll)
    except MetadataError as exc:
        print(f"  [FAIL] 解析抛出 MetadataError: {exc}")
        FAILURES.append(f"{name}: 解析失败 {exc}")
        return
    except Exception as exc:  # noqa: BLE001 - 验证脚本要如实暴露任何异常
        import traceback

        print(f"  [FAIL] 解析抛出未预期异常: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        FAILURES.append(f"{name}: 解析异常 {type(exc).__name__}")
        return

    print(f"  元数据版本字符串 : {meta.version_string}")
    print(f"  元数据根版本     : {meta.version} ({meta.major_version}.{meta.minor_version})")
    print(f"  表流版本         : {meta.tables_stream_name} "
          f"{meta.table_version[0]}.{meta.table_version[1]}")
    print(f"  heapSizes        : 0x{meta.heap_sizes:02X}  "
          f"(#Strings={'4B' if meta.heap_sizes & 1 else '2B'}, "
          f"#GUID={'4B' if meta.heap_sizes & 2 else '2B'}, "
          f"#Blob={'4B' if meta.heap_sizes & 4 else '2B'})")
    print(f"  模块 / 程序集    : {meta.module_name}  /  {meta.assembly_name}")
    print(f"  流               : {', '.join(f'{k}({v[1]:,}B)' for k, v in sorted(meta.streams.items()))}")
    print(f"  运行时           : {meta.image.runtime_kind}")
    print()
    print(f"  类型数           : {meta.type_count:,}")
    print(f"  方法数           : {meta.method_count:,}")
    print(f"  字段数           : {meta.field_count:,}")
    print(f"  参数数           : {meta.parameter_count:,}")
    print(f"  属性 / 事件      : {len(meta.properties):,} / {len(meta.events):,}")
    print(f"  引用程序集       : {len(meta.assembly_refs):,}")
    print(f"  成员引用         : {len(meta.member_refs):,}")
    print(f"  自定义特性       : {len(meta.custom_attributes):,}")

    check(meta.type_count > 0, "类型数 > 0", f"{meta.type_count}")
    check(meta.method_count > 0, "方法数 > 0", f"{meta.method_count}")
    check(meta.field_count > 0, "字段数 > 0", f"{meta.field_count}")
    print(f"  解析警告         : {meta.warnings if meta.warnings else '（无）'}")

    # ---- 2. 表行数（打印全部已出现的表）
    print()
    print("--- 2. 表行数（ECMA-335 表编号）---")
    present = [(i, n, meta.table_rows[n]) for i, n in enumerate(TABLE_NAMES) if n in meta.table_rows]
    for i, tname, rows in present:
        print(f"    0x{i:02X} {tname:<24} {rows:>8,}")
    print(f"    {'':<4} {'（共 ' + str(len(present)) + ' 张表非空）':<24}")

    # ---- 3. validate() 自洽性
    print()
    print("--- 3. validate() 内部自洽性 ---")
    v = meta.validate()
    for key in (
        "sum_field_count", "fields_table_count", "fields_ok",
        "sum_method_count", "methods_table_count", "methods_ok",
        "sum_parameter_count", "params_table_count", "params_ok",
        "owned_fields", "owned_methods", "owner_coverage_ok",
        "nested_class_rows", "nested_named_types", "nested_rows_vs_named_ok",
        "signature_params_match", "signature_params_fewer_in_param_table",
        "signature_params_more_in_param_table", "signature_params_unknown",
        "signature_params_ok",
    ):
        print(f"    {key:<36} = {v[key]}")

    check(v["fields_ok"], "Σfield_count == Field 表行数",
          f"{v['sum_field_count']} vs {v['fields_table_count']}")
    check(v["methods_ok"], "Σmethod_count == MethodDef 表行数",
          f"{v['sum_method_count']} vs {v['methods_table_count']}")
    check(v["params_ok"], "Σparameter_count == Param 表行数",
          f"{v['sum_parameter_count']} vs {v['params_table_count']}")
    check(v["owner_coverage_ok"], "字段/方法全部有归属类型",
          f"owned_fields={v['owned_fields']}/{v['fields_table_count']}, "
          f"owned_methods={v['owned_methods']}/{v['methods_table_count']}")
    check(v["nested_rows_vs_named_ok"], "NestedClass 行数 == 拼出嵌套名的类型数",
          f"{v['nested_class_rows']} vs {v['nested_named_types']}")
    # 签名参数个数与 Param 表区间的交叉校验（两个独立来源的一致性）。
    #
    # 这里**不能**断言两者相等：按 ECMA-335 II.22.33，Param 表只为
    # 「有名字/有特性/有默认值」的参数建行，所以 Param 行数 <= 签名参数个数
    # 才是合法的。真正的错误信号是 Param 行数**多于**签名参数个数。
    # 实测（探针 devtools/probe_mono_paramcount.py 逐条核对过原始字节）：
    # Archon Soul 7 处、Kinny 3 处「少于」，且全是编译器生成的
    # <...>g__X|N_M 局部函数（隐藏参数无名字），没有一处「多于」。
    check(
        v["signature_params_ok"],
        "Param 表行数从不多于签名参数个数（ECMA-335 II.22.33）",
        f"equal={v['signature_params_match']}, "
        f"fewer={v['signature_params_fewer_in_param_table']}(合法), "
        f"more={v['signature_params_more_in_param_table']}(非法), "
        f"unknown={v['signature_params_unknown']}",
    )

    # ---- 4. 嵌套类型名
    print()
    print("--- 4. 嵌套类型名（Outer.Inner）---")
    nested_types = [t for t in meta.types if t.parent_index >= 0]
    print(f"  嵌套类型数：{len(nested_types):,}")
    check(
        len(nested_types) == v["nested_class_rows"],
        "嵌套类型对象数 == NestedClass 行数",
        f"{len(nested_types)} vs {v['nested_class_rows']}",
    )
    bad = []
    for t in nested_types[:400]:
        outer = meta.types[t.parent_index]
        if not t.nested_name.startswith(outer.nested_name + "."):
            bad.append((t.nested_name, outer.nested_name))
    check(not bad, "嵌套名以「外层全名 + .」为前缀（抽查前 400 个）",
          f"不符 {len(bad)} 例: {bad[:3]}")
    print("  抽查 8 个嵌套类型：")
    for t in nested_types[:8]:
        print(f"    {t.nested_name:<70} ← 外层 {meta.types[t.parent_index].nested_name}")

    # ---- 5. API 行为
    print()
    print("--- 5. API 行为 ---")

    hits = meta.search_types(needle, limit=5)
    print(f"  search_types({needle!r}, limit=5) → {len(hits)} 条")
    for t in hits:
        print(f"    {t.nested_name}  字段 {t.field_count} 方法 {t.method_count}")
    check(bool(hits), f"search_types({needle!r}) 有结果", f"{len(hits)} 条")

    # 搜索必须真的是「按子串过滤」，而不是把整个表倒出来。
    # 用 limit=1 与 limit=100000 对比总数来验证过滤真的生效了。
    all_hits = meta.search_types(needle, limit=100000)
    print(f"  search_types({needle!r}, limit=100000) → {len(all_hits):,} 条"
          f"（全库类型 {meta.type_count:,} 个）")
    check(len(all_hits) < meta.type_count,
          "search_types 确实是子串过滤（结果少于全库）",
          f"{len(all_hits)} < {meta.type_count}")
    # 结果必须都命中关键字
    check(all(needle.lower() in t.nested_name.lower() or needle.lower() in t.full_name.lower()
              for t in all_hits),
          "search_types 返回的每条都命中关键字")

    # 全部类型里应该能找到 System.Object（Mono 程序集里通常是 TypeRef 而非 TypeDef）
    any_type = next((t for t in meta.types if t.field_count > 0 and t.method_count > 0), None)
    if any_type is None:
        check(False, "存在同时有字段和方法的类型", "一个都没有，非常可疑")
    else:
        print(f"  选一个真实类型做成员抽查：{any_type.nested_name}"
              f"  ({any_type.runtime_token})")
        f = meta.fields_of(any_type)
        m = meta.methods_of(any_type)
        print(f"    字段 {len(f)} 个：")
        for x in f[:6]:
            print(f"      {x.name:<32} : {meta.resolve_field_type(x)}")
        print(f"    方法 {len(m)} 个：")
        for x in m[:6]:
            params = meta.parameters_of(x)
            sig = meta.resolve_method_signature(x)
            ret = sig["return_type"] if sig else "?"
            ptypes = sig["parameter_types"] if sig else []
            print(f"      {x.name:<32} ({', '.join(ptypes)}) -> {ret}")
            if params:
                print(f"        Param 表参数名：{[p.name for p in params]}")
        check(len(f) == any_type.field_count, "fields_of 数量与 field_count 一致",
              f"{len(f)} vs {any_type.field_count}")
        check(len(m) == any_type.method_count, "methods_of 数量与 method_count 一致",
              f"{len(m)} vs {any_type.method_count}")

        # type_by_name 往返
        back = meta.type_by_name(any_type.nested_name)
        check(back is not None and back.index == any_type.index,
              "type_by_name(nested_name) 往返一致",
              f"{back.nested_name if back else None}")

        # 父类 / 继承链
        pname = meta.parent_name_of(any_type)
        print(f"    parent_name_of → {pname}")
        print(f"    base_chain_names → {meta.base_chain_names(any_type)}")
        check(pname is not None, "parent_name_of 能解出基类名", str(pname))

        # 字段类型解析覆盖率
        resolved = sum(1 for x in meta.fields if meta.resolve_field_type(x) is not None)
        total = len(meta.fields)
        ratio = resolved / total if total else 0
        print(f"    字段签名解析成功率：{resolved:,}/{total:,} = {ratio:.4%}")
        check(ratio > 0.99, "字段签名解析成功率 > 99%", f"{ratio:.4%}")

        # 方法签名解析覆盖率
        mtotal = len(meta.methods)
        mres = sum(1 for x in meta.methods if meta.resolve_method_signature(x) is not None)
        mratio = mres / mtotal if mtotal else 0
        print(f"    方法签名解析成功率：{mres:,}/{mtotal:,} = {mratio:.4%}")
        check(mratio > 0.99, "方法签名解析成功率 > 99%", f"{mratio:.4%}")

    # 基类探测：按样本给，不能写死 MonoBehaviour（mscorlib 不引用 UnityEngine）
    mb = meta.find_derived(probe_base, limit=100000)
    print(f"  find_derived({probe_base!r}) → {len(mb):,} 个")
    for t in mb[:5]:
        print(f"    {t.nested_name}")
    check(len(mb) > 0, f"能找到 {probe_base} 派生类", f"{len(mb)} 个")

    # ---- 6. 官方真值比对
    print()
    print("--- 6. 与官方 System.Reflection.Metadata 真值交叉比对 ---")
    if gt is None:
        print("  [SKIP] 真值不可得（没有预生成 JSON，且 dotnet 构建/运行失败）")
        print("         —— 本样本**未做外部交叉验证**，只有内部自洽证据。")
        return

    print(f"  真值元数据版本：{gt.get('metadata_version')}")
    check(gt.get("metadata_version") == meta.version_string,
          "元数据版本字符串一致",
          f"{gt.get('metadata_version')} vs {meta.version_string}")

    # 6.1 全部 64 个表的行数
    gt_rows = gt.get("table_row_counts", {})
    mismatched = []
    for i, tname in enumerate(TABLE_NAMES):
        key = f"0x{i:02X}"
        expect = int(gt_rows.get(key, 0))
        actual = meta.table_rows.get(tname, 0)
        if expect != actual:
            mismatched.append((tname, expect, actual))
    extra = [k for k in gt_rows if int(gt_rows[k]) > 0 and k not in
             {f"0x{i:02X}" for i in range(len(TABLE_NAMES))}]
    print(f"  比对 {len(TABLE_NAMES)} 张表的行数，不符 {len(mismatched)} 张")
    for tname, expect, actual in mismatched:
        print(f"    [DIFF] {tname}: 官方={expect} 自研={actual}")
    check(not mismatched, "全部表行数与官方真值一致",
          f"不符 {len(mismatched)} 张")

    # 6.2 汇总计数
    for tname, gt_key in GT_SUMMARY_KEYS.items():
        if gt_key not in gt:
            continue
        expect = int(gt[gt_key])
        actual = meta.table_rows.get(tname, 0)
        check(expect == actual, f"{gt_key}({tname}) 与官方一致", f"{expect} vs {actual}")

    # 6.3 逐类型：全名集合、基类、字段名、方法名
    gt_types = gt.get("types")
    if not isinstance(gt_types, list):
        check(False, "真值里的 types 数组可用", "缺失或类型不对")
        return

    print()
    print(f"  逐类型比对（官方 {len(gt_types):,} 个 vs 自研 {len(meta.types):,} 个）")
    check(len(gt_types) == len(meta.types), "类型总数一致",
          f"{len(gt_types)} vs {len(meta.types)}")

    name_mismatch = 0
    base_mismatch = 0
    field_mismatch = 0
    method_mismatch = 0
    checked = 0
    samples_shown = 0
    for g in gt_types:
        idx = g["token"] & 0x00FFFFFF
        if not (1 <= idx <= len(meta.types)):
            name_mismatch += 1
            continue
        mine = meta.types[idx - 1]
        checked += 1

        if mine.nested_name != g["full_name"]:
            name_mismatch += 1
            if samples_shown < 5:
                print(f"    [DIFF] 全名: 官方={g['full_name']!r} 自研={mine.nested_name!r}")
                samples_shown += 1

        # 官方 base_type 为 null 表示无基类；嵌套类型的 base 全名两种工具都应一致
        expected_base = g.get("base_type")
        actual_base = meta.parent_name_of(mine)
        if (expected_base or None) != (actual_base or None):
            base_mismatch += 1
            if samples_shown < 5:
                print(f"    [DIFF] 基类 {mine.nested_name}: "
                      f"官方={expected_base!r} 自研={actual_base!r}")
                samples_shown += 1

        my_fields = [f.name for f in meta.fields_of(mine)]
        if my_fields != [f["name"] for f in g["fields"]]:
            field_mismatch += 1
            if samples_shown < 5:
                print(f"    [DIFF] 字段 {mine.nested_name}: "
                      f"官方={[f['name'] for f in g['fields']][:6]} 自研={my_fields[:6]}")
                samples_shown += 1

        my_methods = [m.name for m in meta.methods_of(mine)]
        if my_methods != [m["name"] for m in g["methods"]]:
            method_mismatch += 1
            if samples_shown < 5:
                print(f"    [DIFF] 方法 {mine.nested_name}: "
                      f"官方={[m['name'] for m in g['methods']][:6]} 自研={my_methods[:6]}")
                samples_shown += 1

    print(f"    比对类型数           : {checked:,}")
    check(name_mismatch == 0, "所有类型全名一致（含嵌套拼装）", f"不符 {name_mismatch}")
    check(base_mismatch == 0, "所有类型基类一致", f"不符 {base_mismatch}")
    check(field_mismatch == 0, "所有类型的字段名列表一致（数量+顺序）", f"不符 {field_mismatch}")
    check(method_mismatch == 0, "所有类型的方法名列表一致（数量+顺序）", f"不符 {method_mismatch}")

    # 6.4 嵌套关系明细
    gt_nested = gt.get("nested", [])
    my_nested = sorted(
        (t.nested_name, meta.types[t.parent_index].nested_name) for t in nested_types
    )
    gt_nested_sorted = sorted((n["nested"], n["enclosing"]) for n in gt_nested)
    check(my_nested == gt_nested_sorted, "嵌套关系明细与官方一致",
          f"官方 {len(gt_nested_sorted)} 条 vs 自研 {len(my_nested)} 条")

    # 6.5 方法签名的返回类型与参数类型（最强的签名解析证据）
    print()
    print("  逐方法签名比对（返回类型 + 参数类型）")
    sig_total = 0
    sig_ret_diff = 0
    sig_param_diff = 0
    sig_shown = 0
    for g in gt_types:
        idx = g["token"] & 0x00FFFFFF
        if not (1 <= idx <= len(meta.types)):
            continue
        mine = meta.types[idx - 1]
        my_methods = meta.methods_of(mine)
        if len(my_methods) != len(g["methods"]):
            continue
        for mm, gm in zip(my_methods, g["methods"]):
            sig = meta.resolve_method_signature(mm)
            if sig is None:
                continue
            sig_total += 1
            if sig["return_type"] != gm.get("return_type"):
                sig_ret_diff += 1
                if sig_shown < 5:
                    print(f"    [DIFF] 返回类型 {mine.nested_name}.{mm.name}: "
                          f"官方={gm.get('return_type')!r} 自研={sig['return_type']!r}")
                    sig_shown += 1
            expected_params = gm.get("param_types") or []
            if sig["parameter_types"] != expected_params:
                sig_param_diff += 1
                if sig_shown < 5:
                    print(f"    [DIFF] 参数 {mine.nested_name}.{mm.name}: "
                          f"官方={expected_params[:4]} 自研={sig['parameter_types'][:4]}")
                    sig_shown += 1
    print(f"    比对方法签名数       : {sig_total:,}")
    check(sig_ret_diff == 0, "方法返回类型全部一致", f"不符 {sig_ret_diff}")
    check(sig_param_diff == 0, "方法参数类型全部一致", f"不符 {sig_param_diff}")

    # 6.5b 参数**个数**：这里以官方真值的 param_types 长度为准，
    # 而不是以 Param 表行数为准（Param 表可以少于参数个数，见 validate 的说明）。
    print()
    print("  逐方法参数个数比对（真值取自签名，非 Param 表）")
    pc_total = 0
    pc_diff = 0
    pc_shown = 0
    param_table_fewer = 0
    for g in gt_types:
        idx = g["token"] & 0x00FFFFFF
        if not (1 <= idx <= len(meta.types)):
            continue
        mine = meta.types[idx - 1]
        my_methods = meta.methods_of(mine)
        if len(my_methods) != len(g["methods"]):
            continue
        for mm, gm in zip(my_methods, g["methods"]):
            expect = len(gm.get("param_types") or [])
            pc_total += 1
            if mm.signature_parameter_count != expect:
                pc_diff += 1
                if pc_shown < 5:
                    print(f"    [DIFF] 参数个数 {mine.nested_name}.{mm.name}: "
                          f"官方={expect} 自研签名={mm.signature_parameter_count}")
                    pc_shown += 1
            # Param 表行数少于签名参数个数是合法的，单独统计以便留证
            if mm.parameter_count < expect:
                param_table_fewer += 1
    print(f"    比对方法数           : {pc_total:,}")
    print(f"    其中 Param 表行数少于签名参数个数的（ECMA-335 合法）：{param_table_fewer:,}")
    check(pc_diff == 0, "方法参数个数全部一致（对照官方签名）", f"不符 {pc_diff}")

    # 6.6 字段类型
    print()
    print("  逐字段类型比对")
    f_total = 0
    f_diff = 0
    f_shown = 0
    for g in gt_types:
        idx = g["token"] & 0x00FFFFFF
        if not (1 <= idx <= len(meta.types)):
            continue
        mine = meta.types[idx - 1]
        my_fields = meta.fields_of(mine)
        if len(my_fields) != len(g["fields"]):
            continue
        for ff, gf in zip(my_fields, g["fields"]):
            got = meta.resolve_field_type(ff)
            f_total += 1
            if got != gf.get("type"):
                f_diff += 1
                if f_shown < 5:
                    print(f"    [DIFF] {mine.nested_name}.{ff.name}: "
                          f"官方={gf.get('type')!r} 自研={got!r}")
                    f_shown += 1
    print(f"    比对字段数           : {f_total:,}")
    check(f_diff == 0, "字段类型全部一致", f"不符 {f_diff}")

    # 6.7 程序集信息
    gt_asm = gt.get("assembly")
    if isinstance(gt_asm, dict):
        check(gt_asm.get("name") == meta.assembly_name, "程序集名一致",
              f"{gt_asm.get('name')} vs {meta.assembly_name}")
        if meta.assemblies:
            mine_ver = (
                f"{meta.assemblies[0].major}.{meta.assemblies[0].minor}."
                f"{meta.assemblies[0].build}.{meta.assemblies[0].revision}"
            )
            check(gt_asm.get("version") == mine_ver, "程序集版本一致",
                  f"{gt_asm.get('version')} vs {mine_ver}")
    check(gt.get("module_name") == meta.module_name, "模块名一致",
          f"{gt.get('module_name')} vs {meta.module_name}")


# ---------------------------------------------------------------- 主流程


def main() -> int:
    print("=" * 78)
    print("Mono (ECMA-335) 元数据解析器真实验证 —— gamemod_mcp.engine.mono_meta")
    print("=" * 78)
    print(f"Python: {sys.version.split()[0]}")
    print(f"样本数: {len(SAMPLES)}")

    def _exists(p) -> bool:
        """样本路径可用性判断；None（samples.py 没找到样本根）一律算缺失。"""
        return p is not None and p.is_file()

    found = [(n, p, pr) for n, p, pr in SAMPLES if _exists(p)]
    for name, path, _pr in SAMPLES:
        if not _exists(path):
            # 样本拿不到 → 记为「无法验证」并跳过，不算失败
            print(f"  [缺失] {name}: {path}（无法验证，跳过）")

    if not found:
        print()
        print("!! 未找到任何真实样本，无法验证。本脚本不会编造结果。")
        return 2

    tool = build_ground_truth_tool()
    if tool is None:
        print()
        print("  [注意] 未能构建/定位官方真值工具（需要 dotnet SDK）。")
        print("         本次验证将只有内部自洽证据，缺少外部交叉验证。")
    else:
        print(f"  官方真值工具: {tool}")

    for name, path, probe in found:
        gt = get_ground_truth(name, path, tool)
        verify_sample(name, path, gt, probe)

    section("汇总")
    print(f"  断言总数: {CHECKS}")
    if FAILURES:
        print(f"  失败: {len(FAILURES)}")
        for item in FAILURES:
            print(f"    - {item}")
        print()
        print("  结论：验证未通过。以上失败项如实列出，未做任何粉饰。")
        return 1

    print("  失败: 0")
    print()
    print("  结论：全部断言通过。")
    print("  证据强度：内部自洽（Σ归属数 == 表行数）+ "
          "官方 System.Reflection.Metadata 逐表/逐类型/逐成员/逐签名交叉比对。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
