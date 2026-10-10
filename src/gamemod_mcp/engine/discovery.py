"""游戏发现：进程枚举与引擎自动识别。

不依赖 psutil —— 用 ctypes 直接调 Win32 API，保证零第三方依赖。

注：原先还有一套「扫 Steam 库列出本机装了哪些游戏」的逻辑，
随 detect_games 工具一并移除了 —— 本 MCP 每次运行都限定在
一个游戏项目目录内，全机扫描属于噪声能力。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import enum
import os
import struct
from dataclasses import dataclass
from pathlib import Path

from ..logutil import warn
from . import pe

# ---------------------------------------------------------------- Mono 程序集

# 游戏自己的代码几乎总在这几个程序集里；按优先级排（越靠前越可能是游戏逻辑）
MONO_PREFERRED_ASSEMBLIES = (
    "Assembly-CSharp.dll",
    "Assembly-CSharp-firstpass.dll",
    "Assembly-CSharp-Editor.dll",
)

# Unity / .NET 自带的框架程序集前缀，不是游戏逻辑
MONO_FRAMEWORK_PREFIXES = (
    "UnityEngine", "Unity.", "System", "mscorlib", "netstandard",
    "Mono.", "Microsoft.", "Newtonsoft", "nunit", "ExCSS", "I18N",
)

# ---------------------------------------------------------------- Win32 常量

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010
MAX_PATH = 260
TH32CS_SNAPPROCESS = 0x00000002
LIST_MODULES_ALL = 0x03

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

# 不属于游戏、但常驻的系统/工具进程（用于过滤噪音）。
# P3 复核时删掉了两项：`"aimodv1"`（上一个项目的工具名，本仓库不该有）与
# 重复出现的 `"conhost"`。名字比对走 `name.lower()`，所以条目一律小写。
NOISE_NAMES = {
    "svchost", "conhost", "idle", "system", "runtimebroker", "dwm", "csrss",
    "winlogon", "searchhost", "textinputhost", "shellexperiencehost",
    "startmenuexperiencehost", "webview2", "msedge", "chrome", "chromium",
    "explorer", "tasklist", "python", "python3", "pythonw", "node", "pwsh",
    "powershell", "cmd", "crashpad_handler", "steam", "steamwebhelper",
    "devenv", "code", "gameoverlayui", "audiodg", "spoolsv",
    "searchindexer", "securityhealthservice", "widgets", "sihost", "ctfmon",
    "taskhostw", "dllhost", "smartscreen", "applicationframehost",
    "backgroundtaskhost", "lockapp", "fontdrvhost", "lsass", "services",
    "wininit", "wmiprvse", "nvcontainer", "nvidia web helper", "igfxem",
    "razer synapse", "armourycrate", "msiafterburner", "rtss",
}

# 明显是游戏运行时的模块名
RUNTIME_MODULES = {
    "gameassembly.dll": "il2cpp",
    "mono-2.0-bdwgc.dll": "mono",
    "mono-2.0-sgen.dll": "mono",
    "mono.dll": "mono",
}


# ---------------------------------------------------------------- 数据结构


@dataclass
class ProcessInfo:
    """一个候选游戏进程。"""

    pid: int
    name: str
    exe_path: str = ""
    window_title: str = ""
    runtime: str = "unknown"
    is_64: bool | None = None
    # 这里曾有一个 `architectures: list[str]` 字段，**永远是空列表**：
    # process_arch 的第二个返回值历来都写死 `[]`，却被 find_game_processes
    # 原样输出成 `"architectures": []`。一个恒为空、又长得像「没检测到任何
    # 架构标记」的字段，比没有这个字段更容易误导 —— 已删除。
    # 位数看 `is_64`（Wow64 判定；32 位系统上会把 32 位进程报成 True，
    # 这是已知局限，本机与样本都是 64 位 Windows）。

    @property
    def image_name(self) -> str:
        return Path(self.exe_path).name if self.exe_path else self.name


# ---------------------------------------------------------------- 进程枚举


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", ctypes.c_wchar * MAX_PATH),
    ]


def _kernel32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def process_exe_path(pid: int) -> str:
    """取进程完整映像路径（需要 PROCESS_QUERY_LIMITED_INFORMATION）。"""
    k32 = _kernel32()
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wt.DWORD(len(buf))
        if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        k32.CloseHandle(handle)


def process_identity(pid: int) -> dict:
    """取进程身份三元组：pid + path + 创建时间。

    用于检测「进程已重启但 PID 复用」的情况 —— AiMod 也用了同样的思路。
    """
    k32 = _kernel32()
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return {"pid": pid, "path": "", "created_at": 0, "valid": False}

    try:
        path = ""
        buf = ctypes.create_unicode_buffer(32768)
        size = wt.DWORD(len(buf))
        if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            path = buf.value

        created = 0
        creation, exit_code, kernel, user = (wt.FILETIME() for _ in range(4))
        if k32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_code),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            created = (creation.dwHighDateTime << 32) | creation.dwLowDateTime

        return {"pid": pid, "path": path, "created_at": created, "valid": bool(path)}
    finally:
        k32.CloseHandle(handle)


def enumerate_processes() -> list[tuple[int, str]]:
    """枚举所有进程，返回 (pid, 映像名) 列表。"""
    k32 = _kernel32()
    snapshot = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return []

    out: list[tuple[int, str]] = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if not k32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return []
        while True:
            out.append((entry.th32ProcessID, entry.szExeFile))
            if not k32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        k32.CloseHandle(snapshot)

    return out


def process_arch(pid: int) -> bool | None:
    """判定进程是不是 64 位。拿不到就返回 None（不知道）。

    依据是 Wow64 判定：`IsWow64Process` 为真说明这是跑在 64 位系统上的
    32 位进程，取反即位数。**已知局限**：32 位 Windows 上 32 位进程会被
    报成 True（那时 IsWow64Process 返回假）。本项目样本与本机都是 64 位
    Windows，真要严格判定应改用 `IsWow64Process2` 比对 nativeMachine。

    这里**不再**返回第二个「架构标记模块」值 —— 它历来都写死 `[]`，
    白白让调用方以为做过检测（见 ProcessInfo 上的说明）。
    """
    k32 = _kernel32()
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    is_64: bool | None = None
    if handle:
        try:
            wow64 = wt.BOOL()
            if k32.IsWow64Process(handle, ctypes.byref(wow64)):
                is_64 = not bool(wow64.value)
        finally:
            k32.CloseHandle(handle)

    return is_64


def list_modules(pid: int, limit: int = 0) -> list[str]:
    """列出进程已加载的模块名（小写）。"""
    k32 = _kernel32()
    # 用 TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32 需要目标架构匹配；
    # 这里用 EnumProcessModules 更稳。psapi 是系统 DLL，始终可用。
    PROCESS_VM_READ_ = 0x0010
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ_, False, pid)
    if not handle:
        return []

    try:
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        needed = wt.DWORD(0)
        if not psapi.EnumProcessModulesEx(
            handle, None, 0, ctypes.byref(needed), LIST_MODULES_ALL
        ):
            return []

        count = needed.value // ctypes.sizeof(ctypes.c_void_p)
        if count <= 0 or count > 4096:
            return []

        arr = (ctypes.c_void_p * count)()
        if not psapi.EnumProcessModulesEx(
            handle, arr, needed.value, ctypes.byref(needed), LIST_MODULES_ALL
        ):
            return []

        out: list[str] = []
        buf = ctypes.create_unicode_buffer(MAX_PATH)
        for h in arr:
            if not h:
                continue
            if psapi.GetModuleBaseNameW(handle, ctypes.c_void_p(h), buf, MAX_PATH):
                out.append(buf.value.lower())
            if limit and len(out) >= limit:
                break
        return out
    finally:
        k32.CloseHandle(handle)


def detect_runtime(pid: int) -> str:
    """检测目标进程使用的脚本运行时。"""
    mods = set(list_modules(pid))
    if not mods:
        return "unknown"

    for mod, rt in RUNTIME_MODULES.items():
        if mod in mods:
            return rt

    # UE 的特征模块
    for m in mods:
        if m.startswith("ue4") or m.startswith("ue5") or m in (
            "unrealengine.dll", "ue4game.exe", "ue4-editor.exe",
        ):
            return "ue"

    return "unknown"


def is_game_process(pid: int) -> bool:
    """启发式判断某进程是否像游戏。

    判据（满足任一即可）：
    - 加载了 il2cpp / mono / UE 运行时
    - 有可见窗口且不在噪音名单里
    """
    if detect_runtime(pid) != "unknown":
        return True

    exe = process_exe_path(pid)
    if not exe:
        return False

    stem = Path(exe).stem.lower()
    if stem in NOISE_NAMES:
        return False

    title = window_title_for_pid(pid)
    return bool(title)


def window_title_for_pid(pid: int) -> str:
    """取某 PID 的主窗口标题（用于辅助识别游戏）。"""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    found: list[str] = []

    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def callback(hwnd, _lparam):
        wnd_pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wnd_pid))
        if wnd_pid.value == pid and user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if buf.value:
                    found.append(buf.value)
                    return False  # 找到一个就够了
        return True

    try:
        user32.EnumWindows(WNDENUMPROC(callback), 0)
    except Exception:  # noqa: BLE001 - 窗口枚举失败不影响主流程
        return ""
    return found[0] if found else ""


def find_processes(name_filter: str = "", only_games: bool = True) -> list[ProcessInfo]:
    """查找进程。

    name_filter: 子串匹配（大小写不敏感）；空表示全部。
    only_games : 是否只保留像游戏的进程。
    """
    needle = name_filter.lower().strip()
    out: list[ProcessInfo] = []

    for pid, name in enumerate_processes():
        if pid <= 4:
            continue
        if needle and needle not in name.lower():
            continue

        stem = Path(name).stem.lower()
        if only_games and stem in NOISE_NAMES:
            continue

        exe = process_exe_path(pid)
        if only_games and not exe:
            continue

        runtime = detect_runtime(pid)
        if only_games and runtime == "unknown":
            # 没有已知运行时，必须有可见窗口才认为是游戏
            title = window_title_for_pid(pid)
            if not title:
                continue
        else:
            title = window_title_for_pid(pid)

        is_64 = process_arch(pid)
        out.append(
            ProcessInfo(
                pid=pid,
                name=name,
                exe_path=exe,
                window_title=title,
                runtime=runtime,
                is_64=is_64,
            )
        )

    # 有运行时的排前面，其次按名字
    out.sort(key=lambda p: (p.runtime == "unknown", p.name.lower()))
    return out


# ---------------------------------------------------------------- 引擎探测


def pick_main_exe(game_dir: str | Path) -> str:
    """在游戏目录里挑出主程序 exe。

    优先级：与目录同名的 exe > 最大的非辅助 exe
    """
    game_dir = Path(game_dir)
    if not game_dir.is_dir():
        return ""

    skip = {
        "crashpad_handler", "unitycrashhandler64", "unitycrashhandler32",
        "unins000", "vcredist_x64", "vcredist_x86", "dxsetup", "setup",
        "launcher_installer", "python", "node",
    }

    candidates: list[tuple[int, int, str]] = []
    for exe in list(game_dir.glob("*.exe")) + list(game_dir.glob("*/*.exe")):
        stem = exe.stem.lower()
        if stem in skip or stem.endswith("crashhandler") or stem.endswith("crashhandler64"):
            continue
        try:
            size = exe.stat().st_size
        except OSError:
            continue
        # 与目录同名者优先（0），其次按大小降序
        same_name = 0 if stem == game_dir.name.lower() else 1
        candidates.append((same_name, -size, str(exe)))

    if not candidates:
        return ""
    candidates.sort()
    return candidates[0][2]


# ---------------------------------------------------------------- 目录布局判定
#
# 这里的三个常量的来历见 `docs/多游戏兼容性扫描报告.md` §3.2：
# 原先 `il2cpp_paths` / `mono_paths` 都有一条 `game_dir.glob("*/*_Data")` 兜底，
# 用来支持「游戏外面还套一层目录」的布局。但那个兜底**无法区分**
#     <dir>/<游戏>/<游戏>_Data      ← 外面套了一层，要支持
#     <dir>/<游戏A>/<A>_Data + <游戏B>/<B>_Data + ...  ← 父目录，必须拒绝
# 实测把 `D:\Projects`（装着十几款游戏）传进来时，它挑中了 Echoes 的元数据、
# 返回 `ok: true`，于是 Agent 以为在分析 `D:\Projects`，实际全程在读另一个游戏。
# 判据只能是「候选游戏根有几个」，所以下面的函数会把候选根一并返回。

#: 主程序体积超过这个值、又和所在目录不同名时，多半是安装包而不是游戏主程序。
#: 实测 `D:\Projects` 里挑出来的是 3.3 GB 的自解压安装包 `Pass The Fear(5).exe`。
SUSPICIOUS_MAIN_EXE_BYTES = 1024 ** 3

#: 找 `*_Data` / `global-metadata.dat` 时的最大深度（相对 game_dir）。
#: 原来最后那段是无深度限制的 `rglob("global-metadata.dat")`；而 `*_Data`
#: 原先只看到第 2 层。取 3 是「比原来略宽、但仍然有界」。
MARKER_SEARCH_MAX_DEPTH = 3

#: 候选清单最多回传多少个（容器目录可能有几十个游戏，回传全部没有意义）
MAX_CANDIDATES = 20


def page_candidates(roots: list[str]) -> tuple[list[str], bool]:
    """把候选清单截到 `MAX_CANDIDATES`，并**如实报告是否被截断**。

    为什么要单独一个函数：候选清单有三处出口（`container_candidates` 的
    `candidates`、`il2cpp_paths` / `mono_paths` 的 `ambiguous_roots`），
    原先三处都写 `roots[:MAX_CANDIDATES]` 而**没有任何截断标志** ——
    实测 `E:\\project` 有 25 个游戏，只回了 20 个，调用方看不出少了 5 个。

    AGENTS.md 契约第 5 条要求「被 limit 截断时在 `has_more` 里如实标注」。
    截断本身是合理的（几十个候选回传没有意义），**不标注**才是缺陷：
    调用方会以为候选就这么多。

    Args:
        roots: 完整候选清单。

    Returns:
        (本页候选, 是否被截断)。字段名沿用 `cap.py` 的约定
        （`{列表名}_has_more` / `{列表名}_returned`）。
    """
    return roots[:MAX_CANDIDATES], len(roots) > MAX_CANDIDATES

#: Mono 程序集目录名（`<游戏>_Data/Managed`）
_MONO_MANAGED = "Managed"

#: IL2CPP 元数据的相对路径（`<游戏>_Data/il2cpp_data/Metadata/...`）
_IL2CPP_META_REL = Path("il2cpp_data") / "Metadata" / "global-metadata.dat"


def _depth_patterns(name: str, max_depth: int) -> list[str]:
    """生成 depth 1..max_depth 的「按名字找」glob 模式。

    形如 `["*_Data", "*/*_Data", "*/*/*_Data"]`：
    depth 1 表示**就在 game_dir 里**，所以通配层数是 `depth - 1`。
    """
    return ["/".join(["*"] * (depth - 1) + [name]) for depth in range(1, max_depth + 1)]


def list_data_dirs(game_dir: Path) -> list[Path]:
    """列出 game_dir 之下（深度 ≤ `MARKER_SEARCH_MAX_DEPTH`）所有 `*_Data` 目录。

    **用按深度展开的 glob，而不是递归遍历**：递归遍历会走进游戏的每一层
    子目录，实测在 Godot 游戏（`Sovereign Tower`）上要 8.3 s，
    而 glob 只按模式逐层列目录，同样的目录 20 ms 内返回。
    命中 `*_Data` 之后也不往下走（那是游戏数据，翻它没有意义）。

    `il2cpp_data` / `mono_data` 要排除：Windows 上 glob 大小写不敏感，
    `*_Data` 会把 `<X>_Data/il2cpp_data` 这个**子目录**也匹配进来
    （名字以 `_data` 结尾），它显然不是一个游戏的 Data 目录。
    """
    skip = {"il2cpp_data", "mono_data"}
    out: list[Path] = []
    for pattern in _depth_patterns("*_Data", MARKER_SEARCH_MAX_DEPTH):
        try:
            out.extend(p for p in game_dir.glob(pattern)
                       if p.is_dir() and p.name.lower() not in skip)
        except OSError:  # 权限不足/路径过长：跳过这一层，不要中断整次探测
            continue
    return sorted(set(out))


def _rel_depth(root: Path, target: Path) -> int:
    """target 在 root 之下第几层（root 自己算 0）；算不出来返回 -1。"""
    try:
        return len(target.relative_to(root).parts) - 1
    except ValueError:
        return -1


def _find_bounded(root: Path, filename: str, max_depth: int) -> Path | None:
    """按文件名做**有深度上限**的搜索，找到即返回。

    替代原来的无界 `rglob`：同一次探测里既要兼容「元数据埋得比较深」的
    非标准布局，又不能让一次 `inspect_game` 把整棵树递归翻一遍。
    """
    for pattern in _depth_patterns(filename, max_depth):
        try:
            for hit in root.glob(pattern):
                if hit.is_file():
                    return hit
        except OSError:
            continue
    return None


def _same_dir(a: str | Path, b: str | Path) -> bool:
    """两个路径是否指向同一个目录（容错大小写与相对路径）。"""
    try:
        return os.path.normcase(os.path.abspath(str(a))) == \
               os.path.normcase(os.path.abspath(str(b)))
    except Exception:  # noqa: BLE001 - 路径非法时按「不同」处理
        return False


def _marked_roots(game_dir: Path, marker: str,
                  data_dirs: list[Path] | None = None) -> tuple[list[tuple[Path, Path]], list[str]]:
    """找出「带着某个标记的游戏根」。

    marker 取 `"il2cpp"`（看 `<X>_Data/il2cpp_data/Metadata/global-metadata.dat`）
    或 `"mono"`（看 `<X>_Data/Managed`）。

    返回 (命中列表, 候选根清单)：
      · 命中列表元素是 (游戏根, 标记路径)，**按深度再按路径**排序 ——
        离 game_dir 越近的越优先（游戏自己那一层的 `X_Data` 必须先于
        子目录里另一个游戏的 `Y_Data`，否则「外面套了一层」的目录会挑错）；
      · 候选根清单是**去重后**的游戏根。

    `data_dirs` 可以传外部已经列好的 `*_Data` 列表 —— 一次探测里要问好几遍
    「这个目录像不像某个引擎的游戏」，每次都重新列一遍目录没有必要。
    """
    hits: list[tuple[Path, Path]] = []
    for data_dir in (list_data_dirs(game_dir) if data_dirs is None else data_dirs):
        marker_path = (data_dir / _IL2CPP_META_REL if marker == "il2cpp"
                       else data_dir / _MONO_MANAGED)
        try:
            ok = marker_path.is_file() if marker == "il2cpp" else marker_path.is_dir()
        except OSError:
            continue
        if ok:
            hits.append((data_dir.parent, marker_path))
    hits.sort(key=lambda item: (_rel_depth(game_dir, item[1]), str(item[1])))
    roots = sorted({str(root) for root, _ in hits})
    return hits, roots


def nested_game_roots(game_dir: str | Path,
                      data_dirs: list[Path] | None = None) -> list[str]:
    """列出 game_dir **自己之外**、看起来是游戏根的候选目录（按路径排序）。

    判据只有一条：**子目录里带着 `*_Data`**（Unity 的布局常量）。
    不额外要求「后端标记存在」—— 一个被裁剪过的、或后端信息缺失的构造
    也是「一个游戏」，把它排除掉就会退回「从一堆游戏里挑一个」的老问题。

    这是「容器目录」与「外面套了一层目录」的**唯一判据**：
      · 1 个候选 → 合法的套层布局，按它来定位元数据；
      · ≥2 个候选 → 装着很多游戏的容器目录，**一律拒绝**（不挑、不猜）。

    两种布局在文件系统上长得一模一样，只能靠候选个数区分 ——
    原先 `glob("*/*_Data")` 兜底挑第一个，才会把 `D:\\Projects` 当成
    Echoes 这个游戏（扫描报告 §3.2）。

    （UE 游戏没有 `*_Data`，不参与计数；多 UE 游戏的父目录当前判定为
    unknown 而不是被误当成某个 UE 游戏，见扫描报告 §5 的已知边界。）
    """
    gd = Path(game_dir)
    if not gd.is_dir():
        return []
    if data_dirs is None:
        data_dirs = list_data_dirs(gd)
    roots: set[str] = set()
    for data_dir in data_dirs:
        if not _same_dir(data_dir.parent, gd):
            roots.add(str(data_dir.parent))
    return sorted(roots)


def container_candidates(game_dir: str | Path,
                         data_dirs: list[Path] | None = None) -> dict:
    """这个目录是不是「装着很多游戏的容器目录」？

    返回 {} 表示不像容器；否则给出候选清单与建议。
    """
    gd = Path(game_dir)
    if not gd.is_dir():
        return {}
    roots = nested_game_roots(gd, data_dirs)
    if len(roots) < 2:
        return {}
    page, truncated = page_candidates(roots)
    out = {
        "container": True,
        "count": len(roots),
        "candidates": page,
        "candidates_returned": len(page),
        "candidates_has_more": truncated,
        "note": (f"这个目录下有 {len(roots)} 个可识别的游戏，它本身不是某一个游戏的"
                 "安装目录。请把 game_dir 指向其中某一个（见 candidates）。"),
    }
    if truncated:
        # 不许只说「有 25 个」却只给 20 个 —— 那 5 个调用方看不见。
        out["note"] += (f"（candidates 只列了前 {len(page)} 个，"
                        f"还有 {len(roots) - len(page)} 个未列出；"
                        f"总数见 count）")
    return out


#: 不支持的引擎的特征文件 → 家族名。判定只依赖「某个文件/目录存在」，
#: 成本极低、误报率低（扫描里 4 款不支持的**真实游戏**全部命中）。
#: 注意这是**给人看的提示**，不是声明的支持能力：引擎仍然是 `unknown`。
_UNSUPPORTED_MARKERS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("godot", ("*.pck", "data_*_windows_*"), "Godot"),
    ("gamemaker", ("data.win",), "GameMaker Studio"),
    ("electron", ("chrome_100_percent.pak", "resources/app.asar"), "Electron / Chromium"),
)


def engine_family(game_dir: str | Path) -> dict:
    """在**不支持**的引擎上给出家族猜测（有标记文件为证）。

    产品只声明支持 IL2CPP / Mono / UE，所以引擎字段照旧是 `unknown`；
    但对使用者来说「Godot」与「完全没认出来」是两件事 —— 前者能立刻
    下结论「不归它管」，后者只会让人反复重试。
    """
    gd = Path(game_dir)
    if not gd.is_dir():
        return {}
    evidence: list[str] = []
    for family, patterns, label in _UNSUPPORTED_MARKERS:
        for pattern in patterns:
            try:
                hit = next(iter(gd.glob(pattern)), None)
            except OSError:
                hit = None
            if hit is not None:
                evidence.append(Path(hit).name)
                return {
                    "family": family,
                    "label": label,
                    "evidence": sorted(set(evidence)),
                    "note": (f"看标记像是 {label}，本 MCP 不支持该引擎"
                             "（只支持 IL2CPP / Mono / UE）。"),
                }
    return {}


def il2cpp_paths(game_dir: str | Path, data_dirs: list[Path] | None = None) -> dict:
    """定位 IL2CPP 的关键文件。

    返回 {"game_assembly":..., "metadata":..., "data_dir":...,
          "ambiguous_roots":[...], "metadata_depth": int}
    缺失项为 ""（`metadata_depth` 为 -1）。

    `ambiguous_roots` **非空**表示：这个目录下有多个各带 IL2CPP 元数据的游戏，
    即它是个容器目录而不是某个游戏。此时 `metadata` 一律留空 ——
    宁可让上层报错并给出候选，也不能挑一个来分析（见文件顶部注释）。
    """
    game_dir = Path(game_dir)
    result = {"game_assembly": "", "metadata": "", "data_dir": "",
              "ambiguous_roots": [], "metadata_depth": -1}
    if not game_dir.is_dir():
        return result

    # GameAssembly.dll 通常在游戏根目录（与 exe 同级）
    for candidate in (
        game_dir / "GameAssembly.dll",
        game_dir / "GameAssembly.dll".lower(),
    ):
        if candidate.is_file():
            result["game_assembly"] = str(candidate)
            break

    # 先判「是不是容器」：≥2 个候选游戏根时一律不挑（见 nested_game_roots）
    nested = nested_game_roots(game_dir, data_dirs)
    if len(nested) > 1:
        page, truncated = page_candidates(nested)
        result["ambiguous_roots"] = page
        result["ambiguous_roots_returned"] = len(page)
        result["ambiguous_roots_has_more"] = truncated
        return result

    hits, _roots = _marked_roots(game_dir, "il2cpp", data_dirs)
    if hits:
        _root, meta = hits[0]
        result["metadata"] = str(meta)
        result["data_dir"] = str(meta.parent.parent.parent)
        result["metadata_depth"] = _rel_depth(game_dir, meta)
        return result

    # 兜底：按文件名有界搜索（原名是 rglob，没有深度上限）
    meta = _find_bounded(game_dir, "global-metadata.dat", MARKER_SEARCH_MAX_DEPTH)
    if meta is not None:
        result["metadata"] = str(meta)
        result["data_dir"] = str(meta.parent.parent.parent)
        result["metadata_depth"] = _rel_depth(game_dir, meta)

    return result


def mono_paths(game_dir: str | Path, data_dirs: list[Path] | None = None) -> dict:
    """定位 Mono 的程序集目录与程序集列表。

    与 `il2cpp_paths` 同一套容器判据：多个候选根时 `managed_dir` 留空，
    只回传 `ambiguous_roots`。
    """
    game_dir = Path(game_dir)
    result = {"managed_dir": "", "assemblies": [], "ambiguous_roots": []}
    if not game_dir.is_dir():
        return result

    # 与 il2cpp_paths 同一套容器判据
    nested = nested_game_roots(game_dir, data_dirs)
    if len(nested) > 1:
        page, truncated = page_candidates(nested)
        result["ambiguous_roots"] = page
        result["ambiguous_roots_returned"] = len(page)
        result["ambiguous_roots_has_more"] = truncated
        return result

    hits, _roots = _marked_roots(game_dir, "mono", data_dirs)
    if not hits:
        return result

    data_dir, managed = hits[0]
    result["managed_dir"] = str(managed)
    # 不截断：glob 顺序不确定，截断可能正好丢掉 Assembly-CSharp.dll。
    # 目录里通常也就一两百个 dll，全收下没有代价。
    result["assemblies"] = sorted(str(p) for p in managed.glob("*.dll"))
    # 顺便找一下 Mono 运行时本体（有它才说明是 Mono 后端）
    for rt in ("mono-2.0-bdwgc.dll", "mono-2.0-sgen.dll", "mono.dll"):
        if (data_dir.parent / rt).is_file() or (game_dir / rt).is_file():
            result["runtime"] = rt
            break

    return result


def pick_mono_assembly(assemblies: list[str]) -> str:
    """从托管程序集列表里挑出「游戏自己的代码」那个 dll。

    为什么要专门挑：一个 Unity Mono 游戏的 Managed 目录里通常有
    100~200 个 dll，其中绝大多数是 UnityEngine/System 等框架程序集。
    **游戏逻辑几乎总在 Assembly-CSharp.dll 里**，直接指出它，
    Agent 就不用在一个目录里猜哪个才是目标。

    挑选优先级：
        1. 精确命中 Assembly-CSharp.dll 之类的首选名
        2. 不在框架程序集前缀里的、体积最大的那个
    挑不出来返回空串（由调用方报错），不要瞎猜一个框架 dll 当游戏代码。
    """
    if not assemblies:
        return ""
    by_name = {os.path.basename(p).lower(): p for p in assemblies}
    for want in MONO_PREFERRED_ASSEMBLIES:
        hit = by_name.get(want.lower())
        if hit:
            return hit

    candidates = []
    for p in assemblies:
        base = os.path.basename(p)
        if base.startswith(MONO_FRAMEWORK_PREFIXES):
            continue
        try:
            candidates.append((os.path.getsize(p), p))
        except OSError:
            continue
    if not candidates:
        return ""
    # 体积最大的通常就是游戏主逻辑（Assembly-CSharp 一般几百 KB ~ 几 MB）
    return max(candidates)[1]


def ue_paths(game_dir: str | Path) -> dict:
    """定位 Unreal Engine 的关键目录。"""
    game_dir = Path(game_dir)
    result = {"paks": "", "exe": "", "engine_dir": ""}

    # ProjectName/Binaries/Win64/ProjectName-Win64-Shipping.exe
    for binaries in game_dir.glob("*/Binaries/Win64"):
        for exe in binaries.glob("*-Win64-Shipping.exe"):
            result["exe"] = str(exe)
            break
        if result["exe"]:
            break

    for content in game_dir.glob("*/Content/Paks"):
        if content.is_dir():
            result["paks"] = str(content)
            break

    for engine in game_dir.glob("*/Binaries/Win64/Engine"):
        result["engine_dir"] = str(engine)
        break

    return result


def detect_engine(game_dir: str | Path, data_dirs: list[Path] | None = None) -> str:
    """自动识别游戏引擎。返回 il2cpp / mono / ue / unknown。

    容器目录（`<dir>/<游戏A>/<A>_Data` + `<dir>/<游戏B>/<B>_Data` + ...）
    一律返回 `unknown` —— 见 `il2cpp_paths` 上方的注释。
    """
    game_dir = Path(game_dir)
    if not game_dir.is_dir():
        return "unknown"

    if data_dirs is None:
        data_dirs = list_data_dirs(game_dir)

    if il2cpp_paths(game_dir, data_dirs)["metadata"]:
        return "il2cpp"

    if mono_paths(game_dir, data_dirs)["managed_dir"]:
        return "mono"

    # UE 特征：*.pak 与 shipped exe
    if list(game_dir.glob("*/Content/Paks/*.pak")) or list(
        game_dir.glob("*/Binaries/Win64/*-Win64-Shipping.exe")
    ):
        return "ue"

    if (game_dir / "Engine").is_dir():
        return "ue"

    # 纯 Unity（无脚本后端信息）：**同级**就有 *_Data 才算
    # （`_rel_depth(...) == 0` 表示那个 `X_Data` 直接躺在 game_dir 里）。
    # 这里不再看子目录：子目录里一堆 `<游戏>_Data` 说明它是容器目录，
    # 报成 unity 会让调用方以为「这目录是个 Unity 游戏，只是缺元数据」。
    if any(_rel_depth(game_dir, d) == 0 for d in data_dirs):
        return "unity"

    return "unknown"


#: `game_root_for_exe` 向上回溯的最大层数。
#:
#: UE 的 Shipping 程序在 `<根>/<项目>/Binaries/Win64/` 下 —— 从它所在目录
#: 回到游戏根需要 4 层；Unity 的主程序通常就躺在游戏根那一层。再往上没有
#: 意义：越往上越可能撞到一个**装着多款游戏**的父目录。
GAME_ROOT_MAX_UP = 4


def _direct_data_dirs(game_dir: Path) -> list[Path]:
    """只列**直接**躺在 `game_dir` 下的 `*_Data`（绝不下钻）。

    与 `list_data_dirs` 的区别是这里**不搜子目录** —— 判「这个目录本身
    是不是游戏根」时不能下钻，理由见 `is_game_root`。
    """
    skip = {"il2cpp_data", "mono_data"}
    try:
        return [p for p in game_dir.glob("*_Data")
                if p.is_dir() and p.name.lower() not in skip]
    except OSError:
        return []


def is_game_root(game_dir: str | Path) -> bool:
    """这个目录**本身**是不是一个受支持游戏的安装根？（严格判据）

    为什么不能用 `detect_engine` 代替：它按设计会**下钻到 3 层**去找标记
    （兼容「元数据埋得比较深」的非标准布局），于是「装着游戏的父目录」
    也会被认成游戏。实测 `detect_engine("C:\\\\")` 返回 `mono` ——
    因为 `C:\\Hearthstone\\Hearthstone_Data\\Managed` 在第 3 层被搜到了。

    这个差别在本函数的使用场景里是致命的：`game_root_for_exe` 要**逐层向上**
    找第一个像游戏根的目录，若用 `detect_engine`，从 `notepad.exe` 往上会
    一路匹配到 `C:\\` 并把会话上下文切到盘符根 —— 实测就是这么错的。

    所以这里的判据是「标记必须**直接**位于本目录之下」（`*_Data` 深度 0，
    UE 的 `Engine` / `*/Content/Paks` 同样只认直接子级）。
    """
    gd = Path(game_dir)
    if not gd.is_dir():
        return False

    # Unity：本目录下直接有 <X>_Data，且带 IL2CPP 元数据或 Managed 程序集
    for data_dir in _direct_data_dirs(gd):
        try:
            if (data_dir / _IL2CPP_META_REL).is_file():
                return True
            if (data_dir / _MONO_MANAGED).is_dir():
                return True
        except OSError:
            continue

    # UE：本目录下直接有 Engine/，或有 <项目>/Content/Paks/*.pak /
    #     <项目>/Binaries/Win64/*-Win64-Shipping.exe
    try:
        if (gd / "Engine").is_dir():
            return True
        for pattern in ("*/Content/Paks/*.pak",
                        "*/Binaries/Win64/*-Win64-Shipping.exe"):
            if next(iter(gd.glob(pattern)), None) is not None:
                return True
    except OSError:
        return False
    return False


def game_root_for_exe(exe: str | Path) -> str:
    """从一个可执行文件**反推**它所属的游戏安装目录；推不出来返回空串。

    为什么需要它：`launch_game(exe=...)` 允许指定任意 exe，而会话里还带着
    「当前游戏是谁」这份身份（`game_dir` / `engine` / `metadata_path`）以及
    由它建立的索引。两者若指向**不同游戏**，索引与运行时就会错配 ——
    实测表现是 `runtime_info` 的 `image_count` 与 `static_image_count`
    对不上（143 vs 140），而工具本身当时不会报任何错。

    判据：从 exe 所在目录逐层向上，取**第一个** `is_game_root` 为真的祖先目录。
    取「第一个」而不是「最外层」—— 越靠近 exe 的边界越具体，越往上越可能
    撞上装着多款游戏的父目录。

    推不出来时返回空串，**不猜**：调用方据此如实说明，而不是硬指一个目录。
    """
    p = Path(exe)
    if not p.is_file():
        return ""
    cur = p.parent
    for _ in range(GAME_ROOT_MAX_UP):
        try:
            if is_game_root(cur):
                return str(cur)
        except OSError:
            return ""
        parent = cur.parent
        # 到盘符根 / 文件系统根就停：再往上没有「游戏目录」这回事了
        if parent == cur:
            break
        cur = parent
    return ""


def describe_game(game_dir: str | Path) -> dict:
    """给出游戏目录的完整探测报告。"""
    game_dir = Path(game_dir)
    # 列一次 `*_Data` 就够：下面每个判断都复用它（容器目录上这能省好几遍目录遍历）
    data_dirs = list_data_dirs(game_dir) if game_dir.is_dir() else []
    engine = detect_engine(game_dir, data_dirs)
    info: dict = {
        "install_dir": str(game_dir),
        "exists": game_dir.is_dir(),
        "engine": engine,
        "main_exe": pick_main_exe(game_dir) if game_dir.is_dir() else "",
    }

    if engine == "il2cpp":
        info["il2cpp"] = {
            k: v for k, v in il2cpp_paths(game_dir, data_dirs).items() if k != "data_dir"
        }
    elif engine == "mono":
        mono = mono_paths(game_dir, data_dirs)
        picked = pick_mono_assembly(mono["assemblies"])
        info["mono"] = {
            "managed_dir": mono["managed_dir"],
            "assembly_count": len(mono["assemblies"]),
            # 指出「哪一个 dll 是游戏自己的代码」——这是 open_game 会索引的那个，
            # Agent 拿到它就能直接读/反编译，不用在 160 个 dll 里猜。
            "game_assembly": picked,
            "game_assembly_name": Path(picked).name if picked else "",
        }
    elif engine == "ue":
        info["ue"] = ue_paths(game_dir)
    else:
        # 认不出来时要说清「为什么」：是容器目录、是套了一层的目录，
        # 还是引擎不在支持范围。只说 `unknown` 会让 Agent 反复重试
        # （见扫描报告 §3.4）。
        nested = nested_game_roots(game_dir, data_dirs)
        if len(nested) > 1:
            container = container_candidates(game_dir, data_dirs)
            info["container"] = container
            info["hint"] = container.get("note", "")
        elif len(nested) == 1:
            # 外面套了一层：目录本身不是游戏，但下面恰好有一个。
            # 这不是错误，只要把 game_dir 指到它就行。
            info["hint"] = (f"这个目录本身不是游戏，它下面的 "
                            f"{Path(nested[0]).name} 是；请把 game_dir 指向它。")
            info["game_root"] = nested[0]
        else:
            family = engine_family(game_dir)
            if family:
                info["engine_family"] = family
                info["hint"] = family["note"]

    # 顺带报一下主程序的运行时
    exe = info.get("main_exe")
    if exe and Path(exe).is_file():
        try:
            pinfo = pe.summarize(exe)
            info["main_exe_runtime"] = pinfo["runtime"]
            info["main_exe_is_64"] = pinfo["is_64"]
        except Exception as exc:  # noqa: BLE001 - 探测失败不影响整体报告
            warn(f"解析主程序失败: {exc}")
        # 安装包常常是这个目录里最大的 exe（实测 3.3 GB 的自解压包），
        # 把它当「游戏主程序」报出去，会让 Agent 拿一个跑不起来的 exe 去启动。
        try:
            size = Path(exe).stat().st_size
        except OSError:
            size = 0
        if size >= SUSPICIOUS_MAIN_EXE_BYTES and \
                Path(exe).stem.lower() != game_dir.name.lower():
            info["main_exe_hint"] = (
                f"这个 exe 有 {size / 1024 ** 3:.1f} GB、且与目录不同名，"
                "很可能是安装包而不是游戏主程序；启动游戏请自带 exe 参数。"
            )

    return info
