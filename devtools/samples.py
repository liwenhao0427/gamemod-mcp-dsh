#!/usr/bin/env python3
"""测试样本与路径的**集中定义**。所有 verify_*.py 都必须从这里取。

为什么要集中
============================================================
历史上 11 个验证脚本各自硬编码了工作区绝对路径：

    sys.path.insert(0, r"C:\\Users\\Administrator\\Documents\\deepseek-harness\\默认工作区\\GameModMCP\\src")

工作区一挪，整套测试就**全部跑不起来**，而脚本不会报「路径错了」，
只会 ModuleNotFoundError 或更糟——静默用了错误的样本。
绝对路径是本项目最容易腐坏的东西，所以收敛到这一个文件。

约定
============================================================
- 仓库根 / src 路径：由本文件位置**推导**，永不硬编码。
- 样本游戏：只记录「可能的位置」，用 `find_*` 拿；取不到返回 None。
- 样本不存在时调用方必须记为「无法验证」并跳过，**不许假装通过**。
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

# 中文 Windows 的控制台默认 GBK，本模块直接运行时（`python devtools\samples.py`）
# 会打印 ✅ / ⚠️，不做这一步会抛 UnicodeEncodeError。
# 注意：本模块是被各 verify_*.py **import** 的，所以这里只影响它自己的
# 直接运行；被 import 时上层脚本已各自设好编码。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ---------------------------------------------------------------- 仓库自身路径

#: 仓库根（devtools/ 的上一级）
ROOT: Path = Path(__file__).resolve().parent.parent
#: 源码根，验证脚本用它设 sys.path
SRC: Path = ROOT / "src"
#: 测试缓存目录（已被 .gitignore 排除）
CACHE_TEST: Path = ROOT / ".cache-test"
#: 跨引擎测试缓存（已被 .gitignore 排除）
CACHE_ADAPT_TEST: Path = ROOT / ".cache-adapt-test"
#: 惰性句柄测试缓存（已被 .gitignore 排除）
CACHE_LAZY_TEST: Path = ROOT / ".cache-lazy-test"


def add_src_to_path() -> None:
    """把仓库的 src 加进 sys.path，供验证脚本 import gamemod_mcp。

    用本文件位置推导，因此换机器、换目录都不需要改。
    """
    import sys
    p = str(SRC)
    if p not in sys.path:
        sys.path.insert(0, p)


def ensure_cache_dirs() -> None:
    """确保测试缓存目录存在。"""
    CACHE_TEST.mkdir(parents=True, exist_ok=True)
    CACHE_ADAPT_TEST.mkdir(parents=True, exist_ok=True)
    CACHE_LAZY_TEST.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- 样本根目录

#: 样本游戏可能所在的根。用环境变量 GAMEMOD_SAMPLES 可整体重定向。
#:
#: 默认值全部**由位置推导**，不含任何盘符字面量：
#:   ① 仓库所在目录（即 `ROOT.parent`，样本与仓库并列存放）
#:   ② `~/Projects`
#: 取不到的根不会有任何副作用（`_find_dir` 只做 `root / 名字` 的存在性判断，
#: 不做递归扫描），所以多列几个候选是安全的。
SAMPLE_ROOTS: list[Path] = [
    Path(os.environ["GAMEMOD_SAMPLES"]) if os.environ.get("GAMEMOD_SAMPLES") else None,
    ROOT.parent,
    Path.home() / "Projects",
]
# 去重但保序：仓库父目录与 ~/Projects 可能恰好是同一个
SAMPLE_ROOTS = list(dict.fromkeys(p for p in SAMPLE_ROOTS if p is not None))


def _find_dir(names: list[str], marker: str = "") -> Path | None:
    """在样本根下找一个目录。

    Args:
        names:  候选目录名（按优先级）
        marker: 必须存在的相对路径，用于确认目录内容正确（不是同名的空壳）
    """
    for root in SAMPLE_ROOTS:
        for n in names:
            cand = root / n
            if not cand.is_dir():
                continue
            if marker and not (cand / marker).exists():
                continue
            return cand
    return None


# ---------------------------------------------------------------- 各引擎样本

def find_il2cpp() -> Path | None:
    """IL2CPP 样本：Garden of Witches（元数据实测约 17 MB）。"""
    return _find_dir(["Garden of Witches"],
                     marker=r"Garden of Witches_Data/il2cpp_data/Metadata/global-metadata.dat")


#: 解析器**声明支持**的 IL2CPP 元数据版本区间（见 `engine/il2cpp_meta.py` 的
#: 「支持版本：v24 ~ v31」）。这里**故意另写一份常量**，不去 import 被测代码：
#: 样本可用性判定必须独立于被测实现，否则解析器一改版本区间，
#: 「哪个样本能用」也跟着变，测试就变成空转了。
_IL2CPP_SUPPORTED_VERSIONS = (24, 31)


def _il2cpp_meta_version(meta: Path) -> int | None:
    """读元数据头里的版本号；魔术数不对或读不出来返回 None（不猜）。"""
    try:
        with open(meta, "rb") as fh:
            head = fh.read(8)
    except OSError:
        return None
    if len(head) < 8:
        return None
    sanity, version = struct.unpack("<Ii", head)
    return version if sanity == 0xFAB11BAF else None


def find_il2cpp_any() -> Path | None:
    """**任意一个**可用的 IL2CPP 样本（不限定具体游戏）。

    为什么需要它：有一类验收与**具体是哪个游戏无关** —— 它们验的是
    「切换游戏时会话身份不串」「目录布局判定 / 容器目录识别」，
    只要是个真的 IL2CPP 游戏就能跑（实测：把 `find_il2cpp()` 换成别的
    IL2CPP 游戏，`verify_session_switch` 14 项、`verify_mcp_discovery`
    49 项全绿）。

    而此前它们硬依赖 Garden of Witches：样本不在本机时整条门禁直接
    `return 1`，于是**长期没有运行**。`AGENTS.md` §5.3 要求「缺样本记为
    无法验证并跳过」，但**长期跳过等于没有保护** —— 这两个门禁一个管
    状态污染、一个管容器目录误判，都是曾经真出过缺陷的地方。

    选取规则：先用规范样本 `find_il2cpp()`；没有再退而求其次，
    在样本根下找**版本号在解析器支持区间内**的 IL2CPP 游戏。
    为什么要看版本号：本机存在 v39 的样本（Unity 6.3），解析器不支持，
    拿它当样本只会把「样本不支持」误报成「代码坏了」。

    取不到返回 `None` —— 调用方仍须按「无法验证」处理，不许假装通过。
    """
    exact = find_il2cpp()
    if exact is not None:
        return exact
    lo, hi = _IL2CPP_SUPPORTED_VERSIONS
    for root in SAMPLE_ROOTS:
        if not root.is_dir():
            continue
        for meta in sorted(root.glob(
                "*/*_Data/il2cpp_data/Metadata/global-metadata.dat")):
            version = _il2cpp_meta_version(meta)
            if version is not None and lo <= version <= hi:
                return meta.parents[3]      # <游戏目录>
    return None


def find_mono() -> Path | None:
    """Mono 样本：Archon Soul（单程序集）。"""
    return _find_dir(["Archon Soul"],
                     marker=r"Archon Soul_Data/Managed/Assembly-CSharp.dll")


def find_mono_multi() -> Path | None:
    """Mono 多程序集样本：Kinny and the Cosmic Cauldron。"""
    return _find_dir(["Kinny and the Cosmic Cauldron"],
                     marker=r"Kinny and the Cosmic Cauldron_Data/Managed/Assembly-CSharp.dll")


#: 全部 Mono 样本：(目录名候选, 相对 marker)。
#:
#: 为什么集中列在这里：`agent.js` 的 Mono 后端里有多处「四个样本一致核实」的断言
#: （哪些导出有、哪些没有），这些断言必须能被一条命令复验 —— 只测一个样本
#: 支撑不起「四个样本一致」这句话。
#:
#: 后两个样本嵌在子目录里（`GambonanzaModDev/Gambonanza`、
#: `Sephiria.v0.12.0/Sephiria.v0.12.0`），所以候选名里带分隔符 ——
#: `_find_dir` 只做 `root / 名字` 的存在性判断，带分隔符是合法的。
MONO_SAMPLE_NAMES: list[tuple[list[str], str]] = [
    (["Archon Soul"], r"Archon Soul_Data/Managed/Assembly-CSharp.dll"),
    (["Kinny and the Cosmic Cauldron"],
     r"Kinny and the Cosmic Cauldron_Data/Managed/Assembly-CSharp.dll"),
    (["GambonanzaModDev/Gambonanza", "Gambonanza"],
     r"Gambonanza_Data/Managed/Assembly-CSharp.dll"),
    (["Sephiria.v0.12.0/Sephiria.v0.12.0", "Sephiria.v0.12.0", "Sephiria"],
     r"Sephiria_Data/Managed/Assembly-CSharp.dll"),
]


def mono_games() -> list[Path]:
    """全部可用的 Mono 样本目录（取不到的自动跳过，不做递归扫描）。"""
    out: list[Path] = []
    for names, marker in MONO_SAMPLE_NAMES:
        d = _find_dir(names, marker=marker)
        if d is not None:
            out.append(d)
    return out


def find_ue_pak_v12() -> Path | None:
    """UE 样本：CatapultHero（pak v12 / utoc v8）。"""
    return _find_dir(["CatapultHero"], marker="Manifest_UFSFiles_Win64.txt")


def find_ue_pak_v11() -> Path | None:
    """UE 样本：Dinoblade（pak v11 / utoc v8）。"""
    return _find_dir(["Dinoblade"], marker="Manifest_UFSFiles_Win64.txt")


def find_il2cpp_dump() -> Path | None:
    """IL2CPP 字段类型真值：游戏官方 dump（含 /*0x20*/ 偏移注释）。

    这是 IL2CPP 字段类型的**最高可信度真值**——它是游戏/dumper 自己生成的，
    不是我们自己写的第二个解析器。
    """
    game = find_il2cpp()
    if game is None:
        return None
    # 常见位置：aiMod/dump/<exe 名>/il2cpp_dump.cs
    for root in SAMPLE_ROOTS:
        cand = root / "aiMod" / "dump" / f"{game.name}.exe" / "il2cpp_dump.cs"
        if cand.is_file():
            return cand
    # 退化：在 aiMod/dump 下找任意 il2cpp_dump.cs
    for root in SAMPLE_ROOTS:
        base = root / "aiMod" / "dump"
        if base.is_dir():
            hits = list(base.rglob("il2cpp_dump.cs"))
            if hits:
                return hits[0]
    return None


def find_mono_groundtruth() -> Path | None:
    """Mono 真值生成器（微软官方 System.Reflection.Metadata）。"""
    d = ROOT / "devtools" / "groundtruth" / "mono"
    return d if d.is_dir() else None


def find_param_dump() -> tuple[Path, Path] | None:
    """带**参数名**的 IL2CPP dump 真值：`<游戏>/Decompiled/dump.cs`。

    为什么不复用 `find_il2cpp_dump()`：那一份（aiMod 的 `il2cpp_dump.cs`）
    只写参数**类型**，参数名位置是空的 ——

        /*0x1beef40*/ System.Void .ctor(System.Byte );

    它核不了「参数名解析对不对」。这一份是 Il2CppDumper 的完整输出：

        public static float Abs(float value) { }

    带参数名，可以用来核对 `parameters_of()` 取到的名字。

    Returns:
        (dump 路径, 它对应的游戏安装目录)；找不到返回 None。
        返回游戏目录是因为真值必须**同源**：拿 A 游戏的 dump 去核 B 游戏的
        元数据毫无意义。
    """
    for root in SAMPLE_ROOTS:
        if not root.is_dir():
            continue
        for cand in sorted(root.glob("*/Decompiled/dump.cs")):
            game = cand.parent.parent
            if list(game.glob("*_Data/il2cpp_data/Metadata/global-metadata.dat")):
                return cand, game
    return None


def metadata_of(game: Path) -> Path | None:
    """从游戏安装目录找出 `global-metadata.dat`（IL2CPP）。"""
    hits = sorted(game.glob("*_Data/il2cpp_data/Metadata/global-metadata.dat"))
    return hits[0] if hits else None


def _game_root_count(root: Path, max_depth: int = 3) -> int:
    """**独立**数一数：这个目录下有多少个「像游戏根」的子目录。

    像游戏根 = 该子目录里有 `<X>_Data/il2cpp_data/Metadata/global-metadata.dat`
    或 `<X>_Data/Managed`。

    **故意不复用 `discovery` 的实现**：样本的可用性判定必须独立于被测代码，
    否则被测代码一回归，样本查找也一起失效，测试就变成空转了
    （「找不到样本」会被当成「无法验证」而不是「修坏了」）。
    """
    roots: set[Path] = set()
    for depth in range(1, max_depth + 1):
        pattern = "/".join(["*"] * (depth - 1) + ["*_Data"])
        for data_dir in root.glob(pattern):
            if not data_dir.is_dir():
                continue
            # Windows 上 glob 大小写不敏感，`*_Data` 会匹配到 `<X>_Data/il2cpp_data`
            if data_dir.name.lower() in ("il2cpp_data", "mono_data"):
                continue
            if (data_dir / "il2cpp_data" / "Metadata"
                    / "global-metadata.dat").is_file() \
                    or (data_dir / "Managed").is_dir():
                roots.add(data_dir.parent)
    return len(roots)


def find_container() -> Path | None:
    """容器目录样本：**装着 ≥2 款游戏**的父目录（不是某一款游戏的目录）。

    这是扫描报告 §3.2 那个缺陷的现场：把这种父目录当游戏传进去，
    修复前会「挑中其中一个游戏」并返回 `ok: true`。
    """
    for root in SAMPLE_ROOTS:
        if root.is_dir() and _game_root_count(root) >= 2:
            return root
    return None


#: 本 MCP 不支持、但有明确标记文件的引擎家族（与报告里的判定规则一致）
UNSUPPORTED_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("godot", ("*.pck", "data_*_windows_*")),
    ("gamemaker", ("data.win",)),
    ("electron", ("chrome_100_percent.pak", "resources/app.asar")),
)


def find_unsupported(max_depth: int = 2) -> tuple[Path, str] | None:
    """不支持引擎的样本：返回 (目录, 家族)，取不到返回 None。

    家族靠**标记文件**判定（Godot 的 .pck、GameMaker 的 data.win、
    Electron 的 chrome_*.pak / app.asar），不靠目录名 —— 目录名会骗人。
    """
    for root in SAMPLE_ROOTS:
        if not root.is_dir():
            continue
        candidates: list[Path] = [root]
        for depth in range(1, max_depth + 1):
            pattern = "/".join(["*"] * depth)
            candidates.extend(p for p in root.glob(pattern) if p.is_dir())
        for game in candidates:
            for family, patterns in UNSUPPORTED_MARKERS:
                for pattern in patterns:
                    try:
                        if next(iter(game.glob(pattern)), None) is not None:
                            return game, family
                    except OSError:
                        continue
    return None


# ---------------------------------------------------------------- 便捷访问器

def game_exe(game_dir: Path) -> Path | None:
    """在一个游戏目录里找出主程序 exe。"""
    if game_dir is None:
        return None
    exes = sorted(game_dir.glob("*.exe"))
    return exes[0] if exes else None


def require(path: Path | None, what: str) -> bool:
    """报告一个样本是否可用，不可用时打印「无法验证」而不是失败。"""
    if path is None:
        print(f"  ⚠️  无法验证：样本「{what}」不在本机")
        print(f"      可用环境变量 GAMEMOD_SAMPLES 指定样本根目录")
        print(f"      当前查找位置：{[str(p) for p in SAMPLE_ROOTS]}")
        return False
    return True


if __name__ == "__main__":
    # 直接运行本文件 = 打印本机样本可用性，方便排查
    print("测试样本可用性")
    print("=" * 66)
    print(f"仓库根      : {ROOT}")
    print(f"源码根      : {SRC}  （存在={SRC.is_dir()}）")
    print(f"测试缓存    : {CACHE_TEST}")
    print(f"样本根      : {[str(p) for p in SAMPLE_ROOTS]}")
    print("-" * 66)
    for label, fn in [
        ("IL2CPP (Garden of Witches)", find_il2cpp),
        ("IL2CPP（任意受支持的）", find_il2cpp_any),
        ("Mono (Archon Soul)", find_mono),
        ("Mono 多程序集 (Kinny)", find_mono_multi),
        ("UE pak v12 (CatapultHero)", find_ue_pak_v12),
        ("UE pak v11 (Dinoblade)", find_ue_pak_v11),
        ("IL2CPP 字段真值 (il2cpp_dump.cs)", find_il2cpp_dump),
        ("Mono 真值生成器", find_mono_groundtruth),
        ("容器目录（装着多款游戏）", find_container),
        ("不支持引擎的样本", lambda: (find_unsupported() or (None, ""))[0]),
    ]:
        p = fn()
        mark = "✅" if p is not None else "⚠️ "
        size = ""
        if p is not None and p.is_file():
            size = f"  ({p.stat().st_size / 1024 / 1024:.1f} MB)"
        print(f"  {mark} {label:<36} {p if p else '不可用'}{size}")
