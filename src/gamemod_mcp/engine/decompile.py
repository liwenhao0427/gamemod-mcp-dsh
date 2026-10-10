"""反编译：对 Mono/.NET 程序集做薄封装（外部工具 `ilspycmd`）。

为什么是薄封装，而不是自研
--------------------------------------------------
`AGENTS.md` §2.4 规定引擎解析器全部手写、只用标准库。反编译是**唯一例外**，
理由是它与解析器不同：元数据解析只有我们自己支持到 IL2CPP v31
（成熟开源方案都停在 v27 / Unity 2022.2），所以必须自研；
而「C# 反编译」是一个完全成熟、MIT 许可、本机已可用的能力，
自研一份只会更差。详见 `docs/开源方案复用评估.md` §2.1 / §3.1。

设计约定（来自上面的评估文档，必须遵守）
--------------------------------------------------
    · **不是运行期依赖**：`requirements.txt` 仍然只有 `mcp` + `frida`。
      工具缺失时由 `get_diagnostics` 报 `decompiler.available=False`，
      调用方返回 `ok: false` + 安装指引，**不抛异常**。
    · **输出体积设上限**：整程序集会出上万个文件（实测 1.1 MB 的 dll
      → 855 个文件 / 2.14 MB），因此整程序集模式必须落盘，
      只返回文件清单与总数，**不把源码塞进返回值**。
    · **只处理 Mono/.NET**：IL2CPP 的 `GameAssembly.dll` 没有托管元数据，
      反编译它需要先造 DummyDll（本项目暂不做），因此明确拒绝并说明原因。

实测到的三个坑（本模块据此设计，改动前请先复读）
--------------------------------------------------
1. **嵌套类型只能拿到「最外层」那一份产物**（实测 ilspycmd 9.0.0.7889）。
   `-t Outer+Inner` 与 `-t Outer.Inner` **都能成功解析**到真实存在的嵌套
   类型（退出码 0），但产物**始终是最外层类型那一份** —— 内层只是其中
   一段缩进的声明。不存在的内层名两种写法都 `exit 65`，会被拦下。
   所以这里的「假成功」不是「随便传什么都返回外层」，而是
   **「产物是外层，却被报成内层」**：调用方拿到
   `decompiled_type=Outer.Inner` 又没有提示，就会以为这是内层的独立源码。
   → 对策：`_list_types()` 取程序集的权威类型清单（`-l cisde`，实测与
     TypeDef 表行数完全一致），据此判定请求名到底是「命名空间限定的
     顶层类型」还是「嵌套类型」—— 这两种写法在字面上都是 `A.B`，
     只有清单能区分。是嵌套就**如实报最外层名**并写 `nested_note`。
     同时保留 `_declared_names()` 护栏（产物里必须真的声明了目标简单名）。
2. **索引里的 `nested_name` 是点号写法**（`Outer.Inner`），而 ilspycmd
   需要先找到 `Outer` 再在内层里看。直接把 `Outer.Inner` 丢给它
   会得到 `Could not find type definition`。
   → 对策：`_candidates()` 逐级向外层退让，且每级都过坑 1 的核对，
     因此「退让」不会造成错认。
3. **退出码语义不统一**：类型不存在、程序集不是托管程序集，
   都是 `exit=70`，只能靠 stderr 的异常类型区分。
   → 对策：`_classify_error()` 按异常名给出可操作的中文说明。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..logutil import log

# ---------------------------------------------------------------- 工具定位

# dotnet 全局工具的常规安装位置（`dotnet tool install --global ilspycmd`）
_DOTNET_TOOL_DIR = Path(os.path.expanduser("~")) / ".dotnet" / "tools"

# 单次调用的超时上限。实测：单类型 0.76~1.05 秒、整程序集 3.5~11.5 秒，
# 这里给足余量，避免大程序集被误判为超时。
_TYPE_TIMEOUT = 120
_ASSEMBLY_TIMEOUT = 600

# 内联返回源码的上限。超过就必须落盘 —— 见模块 docstring 的「输出体积设上限」。
MAX_INLINE_CHARS = 200_000

_INSTALL_HINT = (
    "未找到 ilspycmd。安装（需要 .NET SDK 6.0+）：\n"
    "    dotnet tool install --global ilspycmd\n"
    "安装后确认 `ilspycmd --version` 可用。它是可选的**反编译**依赖，"
    "不影响元数据索引与运行时 Hook（那两项是内置实现）。"
)

# 类型声明行：`public class Foo` / `internal sealed partial struct Bar` …
# 注意要允许前导空白（嵌套类型在输出里是缩进的）。
_DECL_RE = re.compile(
    r"^[ \t]*(?:\[[^\]]*\][ \t]*)*"
    r"(?:(?:public|internal|private|protected)[ \t]+)?"
    r"(?:(?:sealed|abstract|static|partial|readonly|ref|unsafe)[ \t]+)*"
    r"(?:class|struct|enum|interface|delegate)[ \t]+"
    r"([A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)


def _find_tool() -> str:
    """定位 ilspycmd 可执行文件，找不到返回空串。

    先查 PATH（`dotnet tool install --global` 之后通常已在 PATH 上），
    再查 dotnet 全局工具目录（PATH 没刷新的情况下仍能找到）。
    """
    for name in ("ilspycmd", "ilspycmd.exe"):
        hit = shutil.which(name)
        if hit:
            return hit
    for name in ("ilspycmd.exe", "ilspycmd"):
        cand = _DOTNET_TOOL_DIR / name
        if cand.is_file():
            return str(cand)
    return ""


def available() -> dict[str, Any]:
    """探测反编译能力是否可用（供 `get_diagnostics` 调用，不抛异常）。"""
    tool = _find_tool()
    if not tool:
        return {"available": False, "tool": "", "version": "", "hint": _INSTALL_HINT}
    return {"available": True, "tool": tool, "version": _version(tool), "hint": ""}


def _version(tool: str) -> str:
    """取版本号；取不到返回空串，不猜。"""
    try:
        proc = subprocess.run(
            [tool, "--version"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30,
        )
    except Exception:
        return ""
    text = (proc.stdout or "") + (proc.stderr or "")
    match = re.search(r"\d+\.\d+\.\d+(?:\.\d+)?", text)
    return match.group(0) if match else ""


# ---------------------------------------------------------------- 调用封装


def _run(args: list[str], timeout: int) -> dict[str, Any]:
    """执行一次 ilspycmd，返回 {ok, rc, stdout, stderr, error}。

    统一加 `--disable-updatecheck`：该工具默认会联网检查更新，
    在离线环境会拖慢甚至卡住调用。
    """
    tool = _find_tool()
    if not tool:
        return {"ok": False, "rc": -1, "stdout": "", "stderr": "",
                "error": _INSTALL_HINT}
    cmd = [tool, "--disable-updatecheck", *args]
    log.debug("执行反编译：%s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "rc": -1, "stdout": "", "stderr": "",
                "error": f"ilspycmd 执行超时（>{timeout}s）"}
    except Exception as exc:  # 二进制损坏、无执行权限等
        return {"ok": False, "rc": -1, "stdout": "", "stderr": "",
                "error": f"ilspycmd 执行失败：{exc}"}
    return {
        "ok": proc.returncode == 0,
        "rc": proc.returncode,
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
        "error": "" if proc.returncode == 0 else _classify_error(proc.stderr or ""),
    }


def _classify_error(stderr: str) -> str:
    """把 ilspycmd 的 .NET 异常翻译成可操作的中文说明。

    退出码只有 `70` 一种（见模块 docstring 坑 3），所以必须看异常类型。
    """
    text = stderr or ""
    if "MetadataFileNotSupportedException" in text or "managed metadata" in text:
        return (
            "这个文件不含托管元数据，无法反编译。若它是 IL2CPP 的 "
            "GameAssembly.dll，请改用元数据索引（open_game → search_classes）；"
            "IL2CPP 的 C# 反编译需要先造 DummyDll，本项目暂未实现。"
        )
    if "was not found in the module being decompiled" in text:
        return (
            "该类型存在，但不在这个程序集里（它由别的程序集定义）。"
            "请改用定义它的那个程序集作为 assembly 参数。"
        )
    if "Could not find type definition" in text:
        return "在目标程序集里找不到这个类型定义。"
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return f"ilspycmd 失败（退出码 70）：{first[:300]}" if first else "ilspycmd 失败。"


def _declared_names(source: str) -> set[str]:
    """从反编译产物里提取「被声明的类型简单名」。"""
    return set(_DECL_RE.findall(source))


def _candidates(type_name: str) -> list[str]:
    """生成候选类型名：从最具体逐级向外层退让。

    `+` 是 CLR 的嵌套写法，`.` 是索引 `nested_name` 的写法，两者都接受。
    例：`A.B.Outer.Inner` → `A.B.Outer.Inner`、`A.B.Outer`、`A.B`、`A`。
    每级都会过「声明核对」，所以退让不会把外层错认成内层（见 docstring 坑 1）。
    """
    normalized = (type_name or "").strip().replace("+", ".")
    parts = [p for p in normalized.split(".") if p]
    out: list[str] = []
    for i in range(len(parts), 0, -1):
        cand = ".".join(parts[:i])
        if cand not in out:
            out.append(cand)
    return out


def _simple_name(type_name: str) -> str:
    """取类型简单名（最后一段）。"""
    normalized = (type_name or "").strip().replace("+", ".")
    parts = [p for p in normalized.split(".") if p]
    return parts[-1] if parts else ""


# ---------------------------------------------------------------- 类型清单
#
# `-l` 支持的种类：class / interface / struct / delegate / enum。
# 紧凑写法 `cisde` 实测与 TypeDef 表行数**完全一致**（Archon Soul：1387 = 1387），
# 即「程序集里的全部类型定义」，嵌套类型也在内（用 `+` 表示）。
_LIST_KINDS = "cisde"
_LIST_KIND_NAMES = frozenset(
    {"Class", "Interface", "Struct", "Delegate", "Enum"})


def _list_types(assembly: str) -> set[str] | None:
    """取程序集里的全部类型全名（命名空间用 `.`、嵌套用 `+`）。

    为什么需要它：`BarrierSpellEffect.BarrierReference`（嵌套类型）与
    `FlyingWormConsole3.LiteNetLib.NetManager`（命名空间限定名）在字面上
    都是 `A.B`，**只有清单能区分** —— 而这两种情况 ilspycmd 的产出不一样：
    前者只给最外层类型的源码（见模块 docstring 坑 1）。

    取不到返回 `None`。调用方必须按「不知道」处理，不许猜。
    """
    result = _run(["-l", _LIST_KINDS, str(assembly)], _TYPE_TIMEOUT)
    if not result["ok"]:
        return None
    names: set[str] = set()
    for line in result["stdout"].splitlines():
        kind, _, name = line.strip().partition(" ")
        if kind in _LIST_KIND_NAMES and name.strip():
            names.add(name.strip())
    return names or None


def _nested_variants(name: str) -> list[str]:
    """把 `A.B.C` 展开成所有「点号=命名空间 / 加号=嵌套」的可能写法。

    清单里的写法是命名空间用 `.`、嵌套用 `+`（实测
    `BarrierSpellEffect+BarrierReference`、`AnimationSet+StateInfo+Event+EPriority`）；
    而调用方给的名字（索引里的 `nested_name`）一律用 `.`。
    所以要把「最后几段其实是嵌套」的写法都列出来再比对：

        A.B.C → ['A.B.C', 'A.B+C', 'A+B+C']

    顺序 = 嵌套层级由浅到深，即**优先按命名空间解释**（顶层类型更常见）。
    """
    parts = [p for p in (name or "").strip().replace("+", ".").split(".") if p]
    out: list[str] = []
    for i in range(len(parts), 0, -1):
        cand = ".".join(parts[:i])
        if i < len(parts):
            cand = cand + "+" + "+".join(parts[i:])
        if cand not in out:
            out.append(cand)
    return out


def _match_listing(listing: set[str], name: str) -> str:
    """在类型清单里定位 `name`，返回清单中的那个写法；找不到返回空串。"""
    for variant in _nested_variants(name):
        if variant in listing:
            return variant
    return ""


def _real_target(listing: set[str] | None, cand: str) -> tuple[str, bool]:
    """算出 ilspycmd 对 `cand` **实际会产出的类型名**。

    Returns:
        `(实际产出的类型全名, 请求的是否为嵌套类型)`。
        清单不可用（`None`）时退化为 `(cand, False)` —— 与修复前一致。
    """
    if not listing:
        return cand, False
    hit = _match_listing(listing, cand)
    if not hit:
        return cand, False
    if "+" in hit:
        # 嵌套类型：ilspycmd 只会输出最外层那一份（见模块 docstring 坑 1）
        return hit.split("+", 1)[0], True
    return hit, False


# ---------------------------------------------------------------- 对外能力


def decompile_type(assembly: str, type_name: str,
                   output_dir: str = "") -> dict[str, Any]:
    """反编译**单个类型**。

    Args:
        assembly:   目标程序集（.dll / .exe）路径。
        type_name:  类型名，支持 `Namespace.Type`；嵌套类型可传
                    `Outer.Inner` 或 `Outer+Inner`（会自动退让到外层，
                    因为 ilspycmd 无法单独定位嵌套类型 —— 见 docstring 坑 1）。
        output_dir: 给了就落盘到该目录，否则源码内联返回（受体积上限约束）。
    """
    if not _find_tool():
        return {"ok": False, "error": _INSTALL_HINT}
    asm = Path(assembly)
    if not asm.is_file():
        return {"ok": False, "error": f"程序集不存在：{assembly}"}
    if not type_name or not str(type_name).strip():
        return {"ok": False, "error": "必须提供 type_name（或改用 output_dir 反编译整个程序集）。"}

    wanted = _simple_name(type_name)
    tried: list[str] = []
    last_error = ""

    # 权威类型清单：用来区分「命名空间限定的顶层类型」与「嵌套类型」——
    # 两者字面上都是 `A.B`，而 ilspycmd 对后者的产物只有最外层那一份。
    # 取不到（None）时 `_real_target` 退化为修复前的行为，不会更糟。
    listing = _list_types(str(asm))

    for cand in _candidates(type_name):
        tried.append(cand)
        result = _run(["-t", cand, str(asm)], _TYPE_TIMEOUT)
        if not result["ok"]:
            last_error = result["error"]
            # 类型不在本程序集 / 程序集不受支持 —— 退让也没用，直接返回
            if "不含托管元数据" in last_error or "不在这个程序集里" in last_error:
                return {"ok": False, "error": last_error, "assembly": str(asm),
                        "type_name": str(type_name)}
            continue
        source = result["stdout"]
        declared = _declared_names(source)
        # 坑 1 的护栏：输出里必须真的声明了我们找的那个简单名
        if wanted and wanted not in declared:
            last_error = (
                f"ilspycmd 对 `{cand}` 返回成功，但产物里没有声明类型 `{wanted}`"
                "（疑似回退到了外层类型）。已拒绝这次结果，避免交出错误源码。"
            )
            continue

        # 产物**实际**是哪个类型：请求的是嵌套类型时，ilspycmd 只给最外层。
        actual, is_nested = _real_target(listing, cand)

        out: dict[str, Any] = {
            "ok": True,
            "assembly": str(asm),
            "requested": str(type_name),
            "decompiled_type": actual,
            "declared_types": sorted(declared),
            "line_count": len(source.splitlines()),
            "char_count": len(source),
            "truncated": False,
        }
        if is_nested or actual != str(type_name).strip().replace("+", "."):
            # 如实标注退让：`decompiled_type` 已经是**实际产出**的那个类型，
            # 这里说清楚「你请求的 X 为什么变成了 Y」。
            out["nested_note"] = (
                f"`{type_name}` 是嵌套类型，ilspycmd 无法单独定位它 —— "
                f"产物是它所在的最外层类型 `{actual}` 的完整源码，"
                f"`{wanted}` 的声明就在这份产物里（已核对）。"
                if is_nested else
                f"`{type_name}` 未命中，已退让到 `{actual}`；"
                f"`{wanted}` 的声明就在这份产物里（已核对）。"
            )
        if output_dir:
            target = Path(output_dir)
            target.mkdir(parents=True, exist_ok=True)
            dest = target / f"{actual}.decompiled.cs"
            dest.write_text(source, encoding="utf-8")
            out["output_dir"] = str(target)
            out["files"] = [str(dest)]
            out["file_count"] = 1
        elif len(source) <= MAX_INLINE_CHARS:
            out["source"] = source
        else:
            return {
                "ok": False,
                "error": (
                    f"单个类型产出 {len(source)} 字符，超过内联上限 "
                    f"{MAX_INLINE_CHARS}；请指定 output_dir 让它落盘。"
                ),
                "assembly": str(asm), "type_name": str(type_name),
            }
        return out

    return {
        "ok": False,
        "error": (
            f"在《{asm.name}》里找不到类型 `{type_name}`。"
            + (f"已尝试：{tried}。" if len(tried) > 1 else "")
            + (f"底层原因：{last_error}" if last_error else "")
        ),
        "assembly": str(asm),
        "type_name": str(type_name),
        "tried": tried,
    }


def decompile_assembly(assembly: str, output_dir: str,
                       split_files: bool = True) -> dict[str, Any]:
    """反编译**整个程序集**，落盘并返回文件清单统计。

    源码一律不内联（实测 1.1 MB 的 dll 出 2.14 MB 源码），只返回
    文件数、总体积与样例路径 —— 由 Agent 按需去读具体文件。

    Args:
        assembly:    目标程序集路径。
        output_dir:  输出目录（必填，不存在会自动创建）。
        split_files: True 用 `-p` 生成一个类型一个文件（实测 855 个文件 /
                     3.5 秒，便于按需读取）；False 生成单个大文件
                     （实测 2.10 MB / 11.5 秒，便于整体 grep）。
    """
    if not _find_tool():
        return {"ok": False, "error": _INSTALL_HINT}
    asm = Path(assembly)
    if not asm.is_file():
        return {"ok": False, "error": f"程序集不存在：{assembly}"}
    if not output_dir:
        return {"ok": False,
                "error": "反编译整个程序集必须指定 output_dir（产物可能上万文件）。"}

    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    args = ["-o", str(target)]
    if split_files:
        args.append("-p")
    args.append(str(asm))

    result = _run(args, _ASSEMBLY_TIMEOUT)
    if not result["ok"]:
        return {"ok": False, "error": result["error"], "assembly": str(asm),
                "output_dir": str(target)}

    files = sorted(target.rglob("*.cs"))
    total = sum(f.stat().st_size for f in files)
    return {
        "ok": True,
        "assembly": str(asm),
        "output_dir": str(target),
        "split_files": split_files,
        "file_count": len(files),
        "total_bytes": total,
        "total_mb": round(total / 1024 / 1024, 2),
        # 只给样例，完整清单让 Agent 自己去逛目录（避免返回值过大）
        "sample_files": [str(f) for f in files[:20]],
        "note": "源码已落盘，未内联返回；按需读取上面的文件。",
    }
