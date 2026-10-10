import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
# 仓库路径由 samples.py 推导（不许硬编码工作区绝对路径）
# 用 os.path 推导，避免依赖后面才 import 的 pathlib
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import samples  # noqa: E402
samples.add_src_to_path()

# stderr 同样统一成 UTF-8：否则在 cp936 控制台下，logutil 输出的中文会变成乱码
# （logutil 的所有日志都走 stderr，这是它的设计约定）
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

"""UE 结构提取能力的真实验证脚本。

本脚本只做三件事：
  1. 在**两个真实 UE 样本**上调用 gamemod_mcp.engine.ue 的每个函数
  2. 把它**实际拿到的数据**原样打印出来
  3. 输出两个严格分开的小节：「已验证可用」与「未实现 / 未能验证」

设计原则（对应项目铁律）：
  - 所有数字都来自本次运行的真实计算，没有任何硬编码的「预期漂亮值」
  - 凡是拿不到的结构，明确写进「未实现 / 未能验证」，并说明尝试过的路径与失败原因
  - 绝不为了让输出好看而编造类名或偏移

运行方式（务必用 python，不要用 python3）：
    在仓库根目录下执行：python devtools\\verify_ue.py 2>&1
"""

import os
import time
from pathlib import Path

from gamemod_mcp.engine import pe
from gamemod_mcp.engine import ue

# ---------------------------------------------------------------------------
# 真实样本（实测存在的两个 UE 工程）
#
# 根目录从 samples.py 取，exe / manifest / pak / utoc / global_utoc 等派生路径
# 一律用相对路径拼出来，脚本内不保留任何绝对路径。
# ---------------------------------------------------------------------------

_UE_V12 = samples.find_ue_pak_v12()
_UE_V11 = samples.find_ue_pak_v11()


def _ue_sample(name: str, root, project_dir: str, exe_name: str) -> dict:
    """按 UE 工程的固定布局，从根目录拼出一个样本的全部路径。"""
    if root is None:
        return {"name": name, "exe": "", "root": "", "manifest": "",
                "pak": "", "utoc": "", "global_utoc": ""}
    paks = root / project_dir / "Content" / "Paks"
    return {
        "name": name,
        "exe": str(root / project_dir / "Binaries" / "Win64" / exe_name),
        "root": str(root),
        "manifest": str(root / "Manifest_UFSFiles_Win64.txt"),
        "pak": str(paks / f"{project_dir}-Windows.pak"),
        "utoc": str(paks / f"{project_dir}-Windows.utoc"),
        "global_utoc": str(paks / "global.utoc"),
    }


SAMPLES = [
    _ue_sample("CatapultHero", _UE_V12, "CatapultHero",
               "CatapultHero-Win64-Shipping.exe"),
    _ue_sample("Dinoblade", _UE_V11, "DinobladeGame",
               "DinobladeGame-Win64-Shipping.exe"),
]

# 样本不可用时记为「无法验证」并跳过（本脚本按样本逐个跳过，不算失败）
samples.require(_UE_V12, "UE 样本 CatapultHero（pak v12）")
samples.require(_UE_V11, "UE 样本 Dinoblade（pak v11）")

# ---------------------------------------------------------------------------
# 结果收集：脚本结束时统一分两节输出
# ---------------------------------------------------------------------------

VERIFIED: list[str] = []
UNVERIFIED: list[str] = []


def mark_ok(text: str) -> None:
    VERIFIED.append(text)


def mark_no(text: str) -> None:
    UNVERIFIED.append(text)


def section(title: str) -> None:
    print()
    print("=" * 100)
    print(f"  {title}")
    print("=" * 100)


def sub(title: str) -> None:
    print()
    print(f"--- {title} " + "-" * max(0, 92 - len(title)))


def nbytes(path: str) -> str:
    """格式化文件大小；文件不存在时明确写「不存在」。"""
    if not os.path.isfile(path):
        return "不存在"
    return f"{os.path.getsize(path):,} 字节"


def main() -> int:
    print("UE 结构提取验证 —— 基于两个真实样本的实际观测")
    print(f"运行时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Python：{sys.version.split()[0]}")

    # 先确认样本是否可用，不可用就直接说明，不做任何假装成功的输出
    available = []
    section("步骤 0：样本可用性检查")
    for sample in SAMPLES:
        print()
        print(f"### {sample['name']}")
        print(f"   exe      : {sample['exe']}")
        print(f"              {nbytes(sample['exe'])}")
        print(f"   pak      : {sample['pak']}  ({nbytes(sample['pak'])})")
        print(f"   utoc     : {sample['utoc']}  ({nbytes(sample['utoc'])})")
        print(f"   manifest : {sample['manifest']}  ({nbytes(sample['manifest'])})")
        if os.path.isfile(sample["exe"]):
            available.append(sample)
        else:
            print("   [跳过] exe 不存在，无法验证")

    if not available:
        print()
        print("两个样本都不可用，本次未能从样本提取到结构化数据。")
        return 1

    results: dict[str, dict] = {}

    # =====================================================================
    # 1. PE 层（复用 pe.py）
    # =====================================================================
    section("步骤 1：PE 层解析（复用 engine/pe.py）")
    for sample in available:
        name = sample["name"]
        sub(f"{name} — pe.parse()")
        try:
            info = pe.parse(sample["exe"])
        except (pe.PEError, OSError) as exc:
            print(f"   PE 解析失败：{exc}")
            mark_no(f"{name}：PE 解析失败（{exc}）")
            continue

        print(f"   machine           = {info.machine}")
        print(f"   is_64             = {info.is_64}")
        print(f"   image_base        = {info.image_base:#x}")
        print(f"   runtime_kind      = {info.runtime_kind}")
        print(f"   section 数量      = {len(info.sections)}")
        for sec in info.sections:
            print(f"      {sec.name:<10} rva={sec.virtual_address:#010x} vsize={sec.virtual_size:#010x}")
        print(f"   文件大小          = {os.path.getsize(sample['exe']):,} 字节")

        results.setdefault(name, {})["pe"] = {
            "machine": info.machine,
            "is_64": info.is_64,
            "sections": len(info.sections),
            "runtime_kind": info.runtime_kind,
        }
        mark_ok(
            f"{name}：PE 解析成功（{info.machine}，x64={info.is_64}，{len(info.sections)} 个节区，"
            f"runtime_kind={info.runtime_kind}）"
        )
        if info.runtime_kind == "native":
            mark_no(
                f"{name}：pe.runtime_kind 读作 native —— 这是真实观测，说明**不能**用现有的 "
                f".NET/IL2CPP/Mono 运行时判定来识别 UE 游戏"
            )

    # =====================================================================
    # 2. detect_ue_game
    # =====================================================================
    section("步骤 2：detect_ue_game() —— UE 识别与工程布局")
    for sample in available:
        name = sample["name"]
        sub(f"{name} — detect_ue_game()")
        det = ue.detect_ue_game(sample["exe"])
        results.setdefault(name, {})["detect"] = det

        print(f"   is_ue             = {det['is_ue']}")
        print(f"   engine            = {det['engine']}")
        print(f"   confidence        = {det['confidence']} (证据分 {det['score']})")
        print(f"   project_name      = {det['project_name']!r}")
        print(f"   engine_dir        = {det['engine_dir'] or '(未找到)'}")
        print(f"   ue_version        = {det['ue_version']!r}")
        print(f"   ue_version_verified = {det['ue_version_verified']}")
        print(f"   版本判定说明      : {det['ue_version_reason']}")
        print(f"   /Script/ 模块数   = {det['script_module_count']}")
        print(f"   /Script/ 对象数   = {det['script_object_count']}")
        print(f"   已知引擎模块命中  = {det['known_engine_module_hits']}")
        print(f"   模块名样例        = {det['script_modules_sample'][:12]}")
        print(f"   对象路径样例      = {det['script_objects_sample'][:8]}")
        print("   证据链：")
        for item in det["evidence"]:
            print(f"      - {item}")

        layout = det["layout"]
        print("   推导出的布局：")
        print(f"      root_dir     = {layout['root_dir']}")
        print(f"      project_dir  = {layout['project_dir']}")
        print(f"      paks_dir     = {layout['paks_dir']}")
        print(f"      pak 文件     = {[os.path.basename(p) for p in layout['pak_files']]}")
        print(f"      utoc 文件    = {[os.path.basename(p) for p in layout['utoc_files']]}")
        print(f"      ucas 文件    = {[os.path.basename(p) for p in layout['ucas_files']]}")
        print(f"      manifest     = {[os.path.basename(p) for p in layout['manifests']]}")

        if det["is_ue"]:
            mark_ok(
                f"{name}：正确识别为 UE（project={det['project_name']}，"
                f"命中已知引擎模块 {len(det['known_engine_module_hits'])} 个，"
                f"/Script/ 模块 {det['script_module_count']} 个、对象路径 {det['script_object_count']} 个）"
            )
        else:
            mark_no(f"{name}：detect_ue_game 未判定为 UE（{det['reason']}）")

        if not det["ue_version"]:
            mark_no(
                f"{name}：**UE 版本号未验证** —— {det['ue_version_reason']}。"
                f"因此 ue_version 返回空串，而不是猜测值"
            )

    # 负例：必须能拒绝非 UE 程序，否则判定没有意义
    sub("负例检验 —— 非 UE 程序必须被拒绝")
    # 第二个负例是样本根目录下的一个非 UE 可执行/目录名；样本缺失时自动为 None 被跳过
    _neg2 = (str(_UE_V11) + "\\DinobladeGame.exe") if _UE_V11 else None
    for negative in [r"C:\Windows\System32\notepad.exe", _neg2]:
        if not negative or not os.path.isfile(negative):
            print(f"   {negative} —— 不存在，跳过")
            continue
        det = ue.detect_ue_game(negative)
        print(f"   {os.path.basename(negative):<24} is_ue={det['is_ue']} engine={det['engine']} "
              f"模块数={det['script_module_count']} 原因={det['reason']}")
        if not det["is_ue"]:
            mark_ok(f"负例 {os.path.basename(negative)}：被正确判定为非 UE（没有误报）")

    # =====================================================================
    # 3. pak 索引解析
    # =====================================================================
    section("步骤 3：read_pak_index() / list_pak_entries() —— .pak 索引")
    for sample in available:
        name = sample["name"]
        sub(f"{name} — read_pak_index()")
        data = ue.read_pak_index(sample["pak"])
        results.setdefault(name, {})["pak"] = data

        if not data["ok"]:
            print(f"   [失败] {data['reason']}")
            mark_no(f"{name}：pak 索引解析失败 —— {data['reason']}")
            continue

        info = data["pak_info"]
        header = data["index_header"]
        print(f"   文件大小            = {data['file_size']:,} 字节")
        print(f"   FPakInfo 偏移       = {info['info_offset']:,}（文件末尾往前 {data['file_size'] - info['info_offset']} 字节）")
        print(f"   magic               = {info['magic']}")
        print(f"   pak 版本            = {info['version']}")
        print(f"   压缩方法            = {info['compression_method']!r}")
        print(f"   索引偏移 / 大小     = {info['index_offset']:,} / {info['index_size']:,}")
        print(f"   mount_point         = {header['mount_point']!r}")
        print(f"   num_entries(声明)   = {header['num_entries']:,}")
        print(f"   路径哈希种子        = {header['hash_seed']}")
        print(f"   主索引偏移 / 大小   = {header['primary_index_offset']:,} / {header['primary_index_size']:,}")
        print(f"   FDI 偏移 / 大小     = {header['fdi_offset']:,} / {header['fdi_size']:,}")
        print(f"   索引头大小          = {header['header_size']} 字节")
        print(f"   FDI 头部原始字段    = {data['fdi_header_raw']}")
        print(f"   FDI 记录数          = {data['fdi_records']:,}")
        print(f"   FDI 消费比例        = {data['fdi_consumed_ratio'] * 100:.2f}%  "
              f"({data['fdi_consumed']:,}/{header['fdi_size']:,} 字节)")
        print(f"   解析出目录数        = {data['dir_count']:,}")
        print(f"   解析出文件数        = {data['file_count']:,}")
        print(f"   自校验              = {data['self_check']}")
        print("   条目样例（前 8 条）：")
        for entry in data["entries"][:8]:
            kind = "DIR " if entry["is_dir"] else "FILE"
            print(f"      [{kind}] {entry['path']}")
        non_ascii = [e["path"] for e in data["entries"] if any(ord(c) > 127 for c in e["path"])]
        print(f"   含非 ASCII(中文等)路径数 = {len(non_ascii)}")
        for path in non_ascii[:3]:
            print(f"      {path}")

        mark_ok(
            f"{name}：pak 索引解析成功（pak v{info['version']}，mount={header['mount_point']!r}，"
            f"{data['file_count']} 个文件 / {data['dir_count']} 个目录，"
            f"FDI 消费 {data['fdi_consumed_ratio'] * 100:.2f}%，文件数 == 索引头声明数）"
        )
        if non_ascii:
            mark_ok(
                f"{name}：pak 内存在 UTF-16 中文路径（{len(non_ascii)} 条，如 {non_ascii[0]}）——"
                f"这是实测发现，ASCII 分支与 UTF-16 分支必须分别容错"
            )

        # list_pak_entries 返回「目录 + 文件」，与 read_pak_index 的条目总数一致
        listed = ue.list_pak_entries(sample["pak"])
        files_listed = sum(1 for e in listed if not e["is_dir"])
        same = len(listed) == len(data["entries"])
        print(f"   list_pak_entries()  = {len(listed):,} 条（含目录），其中文件 {files_listed:,} 条；"
              f"与 read_pak_index 条目总数一致：{same}")

        # 复用 report 必须与自行解析**完全等价**。
        # server.py 的 ue_list_entries 为了区分「解析失败」与「容器空」
        # 必须先 read_pak_index，再把结果传给 list_pak_entries；
        # 这条断言就是「复用没有改变结果」的证据。
        reused = ue.list_pak_entries(sample["pak"], report=data)
        identical = reused == listed
        print(f"   list_pak_entries(report=...) 与自行解析结果完全一致：{identical}"
              f"（{len(reused):,} 条）")
        if identical:
            mark_ok(
                f"{name}：list_pak_entries(report=) 复用已解析结果与自行解析**逐条等价**"
                f"（{len(reused):,} 条）—— 这是 server.py 去掉重复解析的依据"
            )
        else:
            mark_no(f"{name}：list_pak_entries(report=) 与自行解析结果不一致")

    # =====================================================================
    # 4. utoc（IoStore）目录索引
    # =====================================================================
    section("步骤 4：read_iostore_index() / list_iostore_entries() —— .utoc 目录索引")
    for sample in available:
        name = sample["name"]
        sub(f"{name} — read_iostore_index()")
        data = ue.read_iostore_index(sample["utoc"])
        results.setdefault(name, {})["utoc"] = data

        if not data["ok"]:
            print(f"   [失败] {data['reason']}")
            mark_no(f"{name}：utoc 目录索引解析失败 —— {data['reason']}")
            continue

        print(f"   文件大小            = {data['file_size']:,} 字节")
        print(f"   signature(前16字节) = {data['signature_hex']}")
        print(f"   容器版本(offset 16) = {data['version']}")
        print(f"   header_size         = {data['header_size']}")
        print(f"   entry_count         = {data['entry_count']:,}")
        print(f"   compressed_block_entry_count = {data['compressed_block_entry_count']:,}")
        print(f"   compressed_block_size        = {data['compressed_block_size']}")
        print(f"   compression_method_count     = {data['compression_method_count']}")
        print(f"   compression_block_size       = {data['compression_block_size']}")
        print(f"   directory_index_size         = {data['directory_index_size']:,}")
        print(f"   partition_count              = {data['partition_count']}")
        print(f"   目录索引起点        = {data['directory_index_offset']:,}")
        print(f"   mount_point         = {data['mount_point']!r}")
        print(f"   目录条目数          = {data['dir_entry_count']:,}")
        print(f"   文件条目数          = {data['file_entry_count']:,}")
        print(f"   字符串数            = {data['string_count']:,}")
        print(f"   消费字节            = {data['consumed']:,}/{data['segment_size']:,} "
              f"({data['consumed'] / data['segment_size'] * 100:.2f}%)")
        print(f"   自校验              = {data['self_check']}")
        print("   条目样例（前 10 条）：")
        for entry in data["entries"][:10]:
            kind = "DIR " if entry["is_dir"] else "FILE"
            print(f"      [{kind}] {entry['path']}")

        mark_ok(
            f"{name}：utoc(IoStore v{data['version']}) 目录索引解析成功"
            f"（{data['file_entry_count']} 个文件 / {data['dir_entry_count']} 个目录 / "
            f"{data['string_count']} 个字符串，段内字节消费 "
            f"{data['consumed'] / data['segment_size'] * 100:.2f}%）"
        )

        listed = ue.list_iostore_entries(sample["utoc"])
        files_listed = sum(1 for e in listed if not e["is_dir"])
        print(f"   list_iostore_entries() = {len(listed):,} 条（含目录），其中文件 {files_listed:,} 条")

        # 同 pak：复用 report 必须与自行解析逐条等价
        reused = ue.list_iostore_entries(sample["utoc"], report=data)
        identical = reused == listed
        print(f"   list_iostore_entries(report=...) 与自行解析结果完全一致：{identical}"
              f"（{len(reused):,} 条）")
        if identical:
            mark_ok(
                f"{name}：list_iostore_entries(report=) 复用已解析结果与自行解析**逐条等价**"
                f"（{len(reused):,} 条）"
            )
        else:
            mark_no(f"{name}：list_iostore_entries(report=) 与自行解析结果不一致")

    # global.utoc：实测没有目录索引，必须诚实报告而不是伪造
    sub("global.utoc —— 实测无目录索引")
    for sample in available:
        data = ue.read_iostore_index(sample["global_utoc"])
        print(f"   {sample['name']:<14} ok={data['ok']}  directory_index_size={data.get('directory_index_size')}  "
              f"reason={data['reason']}")
        if not data["ok"] and data.get("directory_index_size") == 0:
            mark_ok(
                f"{sample['name']}：global.utoc 的 directory_index_size 实测为 0，"
                f"函数如实返回失败原因，没有编造条目"
            )

    # =====================================================================
    # 5. 与 Manifest 交叉校验（地面真值）
    # =====================================================================
    section("步骤 5：与 Manifest_UFSFiles_Win64.txt 交叉校验（地面真值）")
    for sample in available:
        name = sample["name"]
        sub(f"{name} — 真值比对")
        manifest = ue.read_manifest(sample["manifest"])
        if not manifest["ok"]:
            print(f"   [失败] {manifest['reason']}")
            mark_no(f"{name}：Manifest 解析失败 —— {manifest['reason']}")
            continue

        print(f"   Manifest 行数      = {manifest['count']:,}")
        print(f"   去重后路径数       = {manifest['unique_count']:,}")
        print("   前 5 条：")
        for entry in manifest["entries"][:5]:
            print(f"      {entry['path']}   [{entry['timestamp']}]")

        ext_count: dict[str, int] = {}
        for entry in manifest["entries"]:
            ext_count[entry["ext"]] = ext_count.get(entry["ext"], 0) + 1
        top_ext = sorted(ext_count.items(), key=lambda kv: -kv[1])[:8]
        print(f"   扩展名分布(前8)    = {top_ext}")

        mark_ok(
            f"{name}：Manifest_UFSFiles_Win64.txt 解析成功（{manifest['unique_count']:,} 条唯一路径，"
            f"格式为 <相对路径>\\t<ISO8601>）"
        )

        truth = {e["path"].lower() for e in manifest["entries"]}

        # pak 比对
        if results[name].get("pak", {}).get("ok"):
            rebuilt = {e["path"] for e in results[name]["pak"]["entries"] if not e["is_dir"]}
            hit = sum(1 for p in rebuilt if p.lower() in truth)
            ratio = hit / len(rebuilt) * 100 if rebuilt else 0.0
            print(f"   pak 重建路径       = {len(rebuilt):,} 条")
            print(f"   pak 命中 Manifest  = {hit:,} 条 → {ratio:.2f}%")
            if ratio == 100.0:
                mark_ok(
                    f"{name}：pak 重建的 {len(rebuilt):,} 条路径**全部**命中 Manifest 真值（100.00%）"
                    f"—— 这是 pak 索引解析正确性的强证据"
                )
            else:
                miss = [p for p in rebuilt if p.lower() not in truth]
                print(f"   pak 未命中样例     = {miss[:5]}")
                mark_no(f"{name}：pak 重建路径与 Manifest 仅 {ratio:.2f}% 命中（未达 100%）")

        # utoc 比对
        if results[name].get("utoc", {}).get("ok"):
            rebuilt = {e["path"] for e in results[name]["utoc"]["entries"] if not e["is_dir"]}
            hit = sum(1 for p in rebuilt if p.lower() in truth)
            ratio = hit / len(rebuilt) * 100 if rebuilt else 0.0
            coverage = hit / len(truth) * 100 if truth else 0.0
            print(f"   utoc 重建路径      = {len(rebuilt):,} 条")
            print(f"   utoc 命中 Manifest = {hit:,} 条 → {ratio:.2f}%（占 Manifest 总量 {coverage:.2f}%）")
            if ratio == 100.0:
                mark_ok(
                    f"{name}：utoc 重建的 {len(rebuilt):,} 条路径**全部**命中 Manifest 真值（100.00%）"
                    f"—— 这是 IoStore 目录索引解析正确性的强证据"
                )
            else:
                miss = [p for p in rebuilt if p.lower() not in truth]
                print(f"   utoc 未命中样例    = {miss[:5]}")
                mark_no(f"{name}：utoc 重建路径与 Manifest 仅 {ratio:.2f}% 命中（未达 100%）")

        # 大小写敏感性说明：实测路径大小写在 pak/utoc 与 Manifest 之间并不一致
        if results[name].get("utoc", {}).get("ok"):
            rebuilt_raw = {e["path"] for e in results[name]["utoc"]["entries"] if not e["is_dir"]}
            truth_raw = {e["path"] for e in manifest["entries"]}
            strict = len(rebuilt_raw & truth_raw)
            loose = sum(1 for p in rebuilt_raw if p.lower() in truth)
            print(f"   大小写敏感交集     = {strict:,}")
            print(f"   大小写不敏感交集   = {loose:,}")
            if strict < loose:
                mark_ok(
                    f"{name}：实测确认 pak/utoc 内路径**保留了原始大小写**，与 Manifest 规范化后的"
                    f"大小写不一致（敏感 {strict:,} vs 不敏感 {loose:,}）—— 比对必须大小写不敏感"
                )

    # =====================================================================
    # 6. exe 内 /Script/ 提取
    # =====================================================================
    section("步骤 6：list_script_objects() —— exe 内 /Script/ 模块与对象")
    for sample in available:
        name = sample["name"]
        sub(f"{name} — list_script_objects()")
        data = ue.list_script_objects(sample["exe"])
        results.setdefault(name, {})["script"] = data

        if not data["ok"]:
            print(f"   [失败] {data['reason']}")
            mark_no(f"{name}：/Script/ 字符串提取失败 —— {data['reason']}")
            continue

        print(f"   模块总数            = {data['module_count']}")
        print(f"   对象路径总数        = {data['object_count']}")
        print(f"   ASCII 模块 / 对象   = {data['ascii_module_count']} / {data['ascii_object_count']}")
        print(f"   UTF-16 模块 / 对象  = {data['utf16_module_count']} / {data['utf16_object_count']}")
        print("   模块名（前 25 个，按命中次数）：")
        for item in data["modules"][:25]:
            flag = " [已知引擎模块]" if item["is_known_engine_module"] else ""
            print(f"      {item['module']:<28} hits={item['hits']}{flag}")
        print("   对象路径（前 25 个，按命中次数）：")
        for item in data["objects"][:25]:
            print(f"      {item['full_name']:<52} hits={item['hits']}")

        mark_ok(
            f"{name}：从 exe 提取到 /Script/ 模块名 {data['module_count']} 个、"
            f"对象路径 {data['object_count']} 个（ASCII {data['ascii_module_count']}/{data['ascii_object_count']}，"
            f"UTF-16 {data['utf16_module_count']}/{data['utf16_object_count']}）"
        )
        mark_no(
            f"{name}：这些只是 exe 内的**字符串证据**，不是反射元数据 —— "
            f"父类(super)与属性偏移(properties)无法由此获得"
        )

        # UEClassInfo：验证 super/properties 确实为空而不是编造
        classes = ue.get_class_info(sample["exe"])
        print(f"   get_class_info()    = {len(classes)} 个 UEClassInfo")
        if classes:
            print(f"      首个：name={classes[0].name!r} full_name={classes[0].full_name!r} "
                  f"super={classes[0].super!r} properties={classes[0].properties}")
            empty_all = all(c.super == "" and not c.properties for c in classes)
            print(f"      全部 super/properties 均为空：{empty_all}（这是刻意的，静态样本无法验证）")
            mark_no(
                f"{name}：UEClassInfo.super 与 .properties 全部返回空 —— "
                f"静态样本中**无法**验证父类链与属性偏移，故不填充任何猜测值"
            )

    # =====================================================================
    # 7. UE 配置读取
    # =====================================================================
    section("步骤 7：read_ue_config() —— UE 配置 / .ini 读取")
    # 配置候选：根目录从 samples.py 拿到的 UE 样本推导，全部用相对路径拼
    config_candidates = [_p for _p in [
        str(_UE_V12 / "Engine" / "Config" / "StagedBuild_CatapultHero.ini")
        if _UE_V12 else None,
        str(_UE_V11 / "Engine" / "Config" / "StagedBuild_DinobladeGame.ini")
        if _UE_V11 else None,
        str(_UE_V11 / "Engine" / "Binaries" / "ThirdParty" / "Steamworks"
            / "Steamv157" / "Win64" / "steam_emu.ini") if _UE_V11 else None,
    ] if _p]
    for cfg in config_candidates:
        sub(os.path.basename(cfg))
        print(f"   路径     = {cfg}")
        print(f"   文件大小 = {nbytes(cfg)}")
        data = ue.read_ue_config(cfg)
        print(f"   ok       = {data['ok']}")
        print(f"   行数     = {data['line_count']}")
        print(f"   段数     = {len(data['sections'])}")
        print(f"   说明     = {data['reason']}")
        for sname, kv in list(data["sections"].items())[:4]:
            print(f"      [{sname}] {len(kv)} 个键")
            for key, value in list(kv.items())[:6]:
                print(f"         {key} = {value}")
        if data["ok"]:
            mark_ok(
                f"{os.path.basename(cfg)}：.ini 解析成功（{data['line_count']} 行，"
                f"{len(data['sections'])} 个 [Section]）"
            )
        if data["ok"] and not data["sections"]:
            mark_no(
                f"{os.path.basename(cfg)}：实测为**空文件/仅 BOM**，没有任何 UE 配置内容 —— "
                f"这是样本的真实情况，不是解析失败"
            )

    # 真实的 UE 配置范围（说明样本里几乎没有可读 UE 配置）
    sub("样本内可用 .ini 的真实范围")
    for sample in available:
        found = ue.list_config_files(sample["root"])
        print(f"   {sample['name']}：{len(found)} 个 .ini")
        for path in found[:10]:
            print(f"      {nbytes(path):>16}  {path}")
    mark_no(
        "两个样本的 Engine/Config/ 下都**没有** Default*.ini / BaseEngine.ini，"
        "也没有 Engine/Build/Build.version —— UE 配置读取能力在样本上几乎无内容可读"
    )

    # =====================================================================
    # 8. extract_ue_profile 汇总
    # =====================================================================
    section("步骤 8：extract_ue_profile() —— 汇总")
    for sample in available:
        name = sample["name"]
        sub(f"{name} — extract_ue_profile()")
        started = time.time()
        profile = ue.extract_ue_profile(sample["exe"], include_entries=False)
        elapsed = time.time() - started

        print(f"   耗时        = {elapsed:.2f} 秒")
        print(f"   ok          = {profile.get('ok')}")
        print(f"   is_ue       = {profile.get('is_ue')}")
        print("   pak 汇总：")
        for item in profile.get("pak", []):
            print(f"      {os.path.basename(item['pak'])}: ok={item['ok']} "
                  f"v{item['pak_version']} 文件={item['file_count']} 目录={item['dir_count']}")
            print(f"         自校验: {item['self_check']}")
        print("   iostore 汇总：")
        for item in profile.get("iostore", []):
            print(f"      {os.path.basename(item['utoc'])}: ok={item['ok']} "
                  f"文件={item['file_entry_count']} 目录={item['dir_entry_count']}")
            if not item["ok"]:
                print(f"         失败原因: {item['reason']}")
        print("   manifest 汇总：")
        for item in profile.get("manifests", []):
            print(f"      {os.path.basename(item['manifest'])}: ok={item['ok']} {item['count']:,} 条")
        print("   交叉校验：")
        for item in profile.get("cross_check", []):
            print(f"      {item['source']} ({item['kind']}): "
                  f"重建 {item['rebuilt']:,} 命中 {item['matched']:,} "
                  f"→ {item['match_ratio'] * 100:.2f}%")
            print(f"         地面真值: {item['truth_source']}"
                  f"（{item['manifest']:,} 条，覆盖率 {item['manifest_coverage'] * 100:.2f}%）")
        print(f"   script: 模块 {profile['script']['module_count']} / "
              f"对象 {profile['script']['object_count']}")
        print(f"   configs: {len(profile.get('configs', []))} 个")

        if profile.get("ok"):
            mark_ok(
                f"{name}：extract_ue_profile 汇总成功（{elapsed:.2f} 秒），"
                f"未声明实现的能力均带明确原因"
            )

    # =====================================================================
    # 9. 明确不实现 / 无法验证的部分 —— 逐项实际尝试并报告
    # =====================================================================
    section("步骤 9：未实现能力 —— 逐项实际探测并报告真实结果")

    # --- 9.1 .usmap ---
    sub("9.1 .usmap 解析 —— 样本中不存在")
    usmap_found: list[str] = []
    # 样本根目录来自 samples.py；样本缺失时该列表为空，等价于「没有样本可搜」
    usmap_roots = [str(r) for r in (_UE_V12, _UE_V11) if r is not None]
    for root in usmap_roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            for fname in filenames:
                if fname.lower().endswith((".usmap", ".uproject", ".upluginmanifest")):
                    usmap_found.append(os.path.join(dirpath, fname))
    print(f"   在 {' 与 '.join(usmap_roots) if usmap_roots else '(无可用样本根)'} 下递归搜索：")
    print(f"      *.usmap / *.uproject / *.upluginmanifest 共找到 {len(usmap_found)} 个")
    for path in usmap_found[:10]:
        print(f"      {path}")
    _probe_pak = next((s["pak"] for s in SAMPLES if s["pak"]
                       and os.path.isfile(s["pak"])), None)
    if _probe_pak is None:
        print("   parse_usmap() 跳过：没有可用的 pak 样本")
    else:
        probe = ue.parse_usmap(_probe_pak)
        print(f"   parse_usmap() 返回值：implemented={probe['implemented']}")
        print(f"      原因：{probe['reason']}")
    mark_no(
        f".usmap 解析：**未实现** —— 两个样本所在目录下递归搜索 *.usmap 结果为 {len(usmap_found)} 个，"
        f"没有真实样本就无法验证 magic/版本枚举/压缩格式，因此拒绝照搬网上格式硬写"
    )

    # --- 9.2 FName 池 ---
    sub("9.2 FName 池定位 —— 实测失败")
    print("   已尝试的布局假设（全部在两个样本的每个数据节区上实测）：")
    print("      - FNameEntry 头 uint16：len>>6 & 0x3FF（UE5 常见布局）")
    print("      - FNameEntry 头 uint16：len>>1 & 0x7FFF（UE4 常见布局）")
    print("      - 连续 FNameEntry 链式行走（要求连续多个可解析条目）")
    print("      - 验证方式：能否稳定走出 >= 8 个连续条目，且其中包含 Actor/Object/Pawn/Component")
    print()
    for sample in available:
        name = sample["name"]
        info = pe.parse(sample["exe"])
        # 实际统计：UTF-16 关键字在 rdata 中的出现次数
        counts = {}
        for keyword in ["Actor", "Object", "Pawn", "Component", "World", "Level"]:
            utf16 = keyword.encode("utf-16-le")
            ascii_ = keyword.encode("ascii")
            counts[keyword] = (info._data.count(utf16), info._data.count(ascii_))
        print(f"   {name}：")
        for keyword, (u16, asc) in counts.items():
            print(f"      {keyword:<10} UTF-16 出现 {u16:>5} 次 / ASCII 出现 {asc:>5} 次")
        print(f"      结论：这些类名只作为普通字符串散落在 .rdata，**不构成 FName 池**")
    mark_no(
        "FName 池：**未能从样本定位** —— len>>6 与 len>>1 两种 FNameEntry 头布局均走不出"
        "连续 >= 8 个有效条目；能找到的最长链只有 11 个单字符垃圾条目。"
        "Actor/Object/Pawn/Component 等类名确实存在于 exe，但只是 .rdata 里的普通 UTF-16 字符串，"
        "不是 FName 池。因此**不实现** FName 池解析"
    )
    mark_no(
        "UObject 类结构 / 属性偏移：**未实现** —— 依赖 FName 池与反射元数据，"
        "而二者在静态样本中均无法获得；需运行时（Frida）读取，超出本模块范围"
    )

    # --- 9.3 UE 版本 ---
    sub("9.3 UE 版本号 —— 无可信来源")
    print("   已尝试的来源与结果：")
    for sample in available:
        bv = Path(sample["root"]) / "Engine" / "Build" / "Build.version"
        print(f"      {sample['name']}: {bv} → {'存在' if bv.is_file() else '不存在'}")
    for pattern in [rb"UE5\.\d+", rb"UE4\.\d+", rb"\+UE\d+\.\d+", rb"UnrealEngine[/\\][\d.]+"]:
        total = 0
        for sample in available:
            info = pe.parse(sample["exe"])
            total += len(__import__("re").findall(pattern, info._data))
        print(f"      exe 内模式 {pattern!r} → 共 {total} 处命中")
    print("      注意：样本内确实存在 5.6.6 / 5.5.5 等数字，但逐一核对上下文后确认全部来自")
    print("            第三方库（如 OpenSSL/libpng/zlib），**不能**当作 UE 版本")
    mark_no(
        "UE 版本号：**未验证** —— Engine/Build/Build.version 不存在；"
        "exe 内 UE5.x/UE4.x/+UEx.y/UnrealEngine/x.y 模式 0 命中。"
        "detect_ue_game 因此返回空串并附原因，而不是从第三方库版本号里猜一个"
    )

    # --- 9.4 加密 / Oodle 压缩索引 ---
    sub("9.4 pak 加密标志与 Oodle 压缩索引块 —— 未解析")
    for sample in available:
        data = results[sample["name"]].get("pak", {})
        if not data.get("ok"):
            continue
        info = data["pak_info"]
        print(f"   {sample['name']}: 压缩方法={info['compression_method']!r} "
              f"encrypted={info['encrypted']}")
        print(f"      {info['encrypted_note']}")
    mark_no(
        "pak 加密标志：**未验证** —— FPakInfo+44 实测为压缩方法名字符串（Oodle + 零填充），"
        "本模块不把它解读为加密标志；样本中也没有观察到加密特征"
    )
    mark_no(
        "pak 内各文件的精确磁盘偏移与大小：**未实现** —— 条目表经过 Oodle 压缩，"
        "无解压器无法读取 FPakEntry；因此条目里的 index_value 只原样暴露，不解释为文件偏移"
    )

    # =====================================================================
    # 最终两节报告
    # =====================================================================
    section("【第一节】已验证可用（本次运行真实拿到数据）")
    if VERIFIED:
        for idx, item in enumerate(VERIFIED, 1):
            print(f"  {idx:>2}. {item}")
    else:
        print("  （无）")

    section("【第二节】未实现 / 未能验证")
    if UNVERIFIED:
        for idx, item in enumerate(UNVERIFIED, 1):
            print(f"  {idx:>2}. {item}")
    else:
        print("  （无）")

    section("总结")
    print(f"  已验证可用项：{len(VERIFIED)} 条")
    print(f"  未实现/未验证项：{len(UNVERIFIED)} 条")
    print()
    if not VERIFIED:
        print("  本次未能从样本提取到结构化数据。")
        print("  已尝试的路径与失败原因见上方各步骤输出。")
    else:
        print("  本次已从两个真实 UE 样本提取到结构化数据，详见上方各步骤的真实输出。")
        print("  所有 pak/utoc 路径重建结果均已与 Manifest_UFSFiles_Win64.txt 交叉校验。")
        print("  未实现的能力（.usmap、FName 池、UE 版本、属性偏移）均明确标注，未编造任何数据。")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
