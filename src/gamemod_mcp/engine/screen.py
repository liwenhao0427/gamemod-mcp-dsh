"""屏幕捕获与 OCR —— 给「最后一公里」验收用的**弱视觉**能力。

为什么只做「弱视觉」
============================================================
调研了 7 个游戏自动化 Bot 项目（MAA / MaaFramework / BetterGI /
March7thAssistant / ALAS / ok-script / MaaMCP）后的结论，也是本模块的
设计前提：

  **验收该靠引擎自己的信号，不该靠像素。**

所以本模块**不参与判据**，它只提供两件事：
  1. **OCR 文本**——用来判断「现在在哪个界面 / 按钮叫什么」；
  2. **文本框坐标**——用来决定「点哪里」。
截图本身只作**证据**（失败时落盘给人看），绝不作为 `verdict` 的来源。
`podium-mcp` 的源码把这条写成了文件头注释：
*"Screenshots are NEVER an oracle."*

依赖
============================================================
全部是**可选**的，缺了不影响本项目其它能力（与 `decompile.py` 对
`ilspycmd` 的处理一致）：
  · 截图：`dxcam`（Desktop Duplication，独占全屏最稳）→ 回退 `mss`
  · OCR ：`rapidocr_onnxruntime`（中文好、纯 onnx）→ 回退 `tesseract`
缺哪个由 `get_diagnostics` 的 `screen` 段如实报告，工具在缺失时返回
`ok:false` + 安装指引，**不抛异常、不假成功**。

四条来自 Bot 项目的实战教训（都体现在下面的实现里）
============================================================
1. **只 OCR 局部 ROI**。实测整屏 2560x1440 用 RapidOCR 要 **4.1 秒**，
   而验收场景的按钮区域通常只有几百像素见方 —— 全屏 OCR 既慢又容易
   被无关文本干扰。MaaFramework 的 `OCRer::analyze()` 第一步就是裁 ROI。
2. **坐标必须是屏幕绝对坐标**。裁剪图坐标不回传基准，Agent 会点偏
   （MaaMCP 的 README 专门把这条写进坑）。本模块一律回绝对坐标，
   并附 `coordinate_space` 说明，不给 Agent 留换算的机会。
3. **截图只落盘、不回 base64**。本项目任何返回都不超过 256 KB，而单张
   截图 base64 约 5 万 token（`windows-computer-use-mcp` 实测量级）。
4. **失败要留证据**。识别失败时把当时的截图落盘并在返回里给路径
   （等价 MaaFramework 的 `save_on_error`），而不是只回一句「没找到」。

实测（本机，2560x1440，RapidOCR）
============================================================
  · 引擎初始化约 0.95 秒（首次，之后复用）
  · 整屏 OCR 约 4.1 秒、识别出 94 个文本框
  · 中文可识别（`chi_sim` 亦可用 tesseract 5.5）
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from ..logutil import log

# ---------------------------------------------------------------- 可选依赖

#: 截图后端的探测顺序：Desktop Duplication 优先（对全屏独占 D3D 最稳），
#: 再回退 mss（纯 ctypes、无 GPU 加速、但兼容性最好）。
_CAPTURE_BACKENDS = ("dxcam", "mss")
#: OCR 引擎顺序：RapidOCR（PP-OCR 转 onnx，中文强）优先，回退 tesseract。
_OCR_ENGINES = ("rapidocr_onnxruntime", "pytesseract")

#: dxcam 的相机对象**必须复用**：每次 `dxcam.create()` 都会打印
#: 「DXCamera instance already exists ...」并在某些版本上重复申请
#: Desktop Duplication 资源。实测连续调 3 次就刷 3 行警告。
#: 这里缓存一份，进程内只创建一次。
_DXCAM = None


def _try_import(name: str):
    try:
        return __import__(name)
    except Exception:
        return None


def available() -> dict:
    """探测本机可用的截图 / OCR 能力。给 `get_diagnostics` 用。

    **每一项都给出「能不能用」与「缺什么」**，不让调用方去猜。
    """
    out: dict[str, Any] = {}

    caps = {}
    for name in _CAPTURE_BACKENDS:
        caps[name] = _try_import(name) is not None
    out["capture"] = caps
    out["capture_usable"] = any(caps.values())
    if not out["capture_usable"]:
        out["capture_hint"] = (
            "截图不可用：请安装 `pip install dxcam`（推荐，独占全屏更稳）"
            "或 `pip install mss`"
        )

    ocrs = {}
    for name in _OCR_ENGINES:
        ocrs[name] = _try_import(name) is not None
    out["ocr"] = ocrs
    out["ocr_usable"] = any(ocrs.values())
    if not out["ocr_usable"]:
        out["ocr_hint"] = (
            "OCR 不可用：请安装 `pip install rapidocr-onnxruntime`（推荐，中文好）"
            "或安装 Tesseract-OCR 并 `pip install pytesseract`"
        )
    else:
        eng = ocr_engine_name()
        out["ocr_engine"] = eng
        if eng == "pytesseract":
            out["ocr_note"] = ("当前只有 tesseract；中文需要 chi_sim 语言包"
                               "（`tesseract --list-langs` 确认）")
    return out


def capture_backend_name() -> str:
    """当前会用的截图后端名；都没有则返回空串。"""
    for name in _CAPTURE_BACKENDS:
        if _try_import(name) is not None:
            return name
    return ""


def ocr_engine_name() -> str:
    """当前会用的 OCR 引擎名；都没有则返回空串。"""
    for name in _OCR_ENGINES:
        if _try_import(name) is not None:
            return name
    return ""


# ---------------------------------------------------------------- 区域规范化

def _normalize_region(region: Any) -> list[int] | None:
    """把 region 规范成 `[x, y, w, h]` 列表；认不出来返回 None。

    接受的形态（**故意两种都收**）：
      · `[x, y, w, h]`（list/tuple）—— 引擎 API 与验收脚本的自然写法；
      · `"x, y, w, h"`（字符串）—— MCP 工具层的入参形态。

    宽高必须为正 —— 不然是「零面积区域」，截图会得到一个空图，
    与其让它悄悄成功，不如明确判为格式错误（返回 None 由调用方报错）。
    负坐标是**合法**的（多显示器时窗口可能在主屏左侧）。
    """
    if region is None:
        return None
    try:
        if isinstance(region, str):
            parts = [p.strip() for p in region.split(",")]
            if len(parts) != 4:
                return None
            vals = [int(float(p)) for p in parts]
        elif isinstance(region, (list, tuple)):
            if len(region) != 4:
                return None
            vals = [int(float(v)) for v in region]
        else:
            return None
    except (ValueError, TypeError):
        return None
    if vals[2] <= 0 or vals[3] <= 0:
        return None
    return vals


# ---------------------------------------------------------------- 窗口定位

#: DPI 感知是否已（尝试）设置过。设置一次即可，重复调用无意义。
_DPI_DONE = False
_DPI_MODE = ""


def _ensure_dpi_aware() -> str:
    """尽力让本进程**DPI 感知**，使窗口坐标与截图像素处在同一坐标系。

    为什么必须做（本机实测的缺陷，改前会截错区域）
    ------------------------------------------------
    本机是 **150% 缩放**（物理 2560x1440 / 虚拟 1707x960）。进程若不是
    DPI 感知的，`GetWindowRect` 返回的是**虚拟化**后的坐标：

        游戏窗口 GetWindowRect  → [451, 0, 1319, 517]     （虚拟，1707x960 系）
        DWM 真实边界            → [685, 0, 1969, 767]     （物理，2560x1440 系）

    而 `dxcam` / `mss` 截图用的是**物理像素**。两者混用 → 截到的区域
    整体偏移（实测截出来是旁边那个终端窗口，而不是游戏），
    而 `capture_screen` 会**如实返回 ok=true** —— 典型的假成功。

    修法：进程级 DPI 感知 + 优先用 `DWMWA_EXTENDED_FRAME_BOUNDS`
    （它给的是**视觉上**的窗口边界，不含不可见的调整边框）。

    Returns:
        实际生效的模式名（便于核查）；拿不到时返回空串。
    """
    global _DPI_DONE, _DPI_MODE
    if _DPI_DONE:
        return _DPI_MODE
    _DPI_DONE = True
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        # PER_MONITOR_AWARE_V2 = -4（Win10 1703+）。先试这个 —— 它不会像
        # 老式 SetProcessDPIAware 那样在混合 DPI 多显示器上出问题。
        try:
            ctx = ctypes.c_void_p(-4)
            if user32.SetProcessDpiAwarenessContext(ctx):
                _DPI_MODE = "per_monitor_v2"
                return _DPI_MODE
        except Exception:
            pass
        # 回退 1：shcore.SetProcessDpiAwareness(2 = PER_MONITOR_AWARE)
        try:
            shcore = ctypes.windll.shcore
            if shcore.SetProcessDpiAwareness(2) == 0:
                _DPI_MODE = "per_monitor"
                return _DPI_MODE
        except Exception:
            pass
        # 回退 2：最老的 API
        try:
            if user32.SetProcessDPIAware():
                _DPI_MODE = "system_aware"
                return _DPI_MODE
        except Exception:
            pass
        _ = wintypes  # 仅为保持 import 语义明确
    except Exception:
        pass
    _DPI_MODE = ""
    return _DPI_MODE


def find_window_rect(title_substr: str) -> dict:
    """按标题子串找窗口并返回它的**屏幕物理像素**矩形。

    用 win32gui（pywin32，可选）。**这不是必须的**：拿不到就如实报，
    调用方可以退化成「整屏 + region」。

    为什么要窗口矩形：验收时要截的是**游戏窗口**而不是整个桌面
    （桌面上可能有别的窗口遮挡）。而 OCR 后的坐标必须能直接用来点击，
    所以窗口矩形必须与 `dxcam`/`mss` 截图处在**同一坐标系** ——
    也就是物理像素。见 `_ensure_dpi_aware()` 里记的那个实测缺陷。
    """
    if not title_substr:
        return {"ok": False, "error": "没有给窗口标题"}
    try:
        import win32gui
    except Exception as exc:
        return {"ok": False, "error":
                f"未安装 pywin32（`pip install pywin32`），无法按标题找窗口：{exc}"}

    dpi_mode = _ensure_dpi_aware()

    hits: list[dict] = []
    needle = title_substr.lower()

    def _bounds(hwnd: int) -> tuple[list[int], str]:
        """返回 (rect, 来源)。优先 DWM 真实边界，回退 GetWindowRect。"""
        try:
            import ctypes
            from ctypes import wintypes

            ext = wintypes.RECT()
            # DWMWA_EXTENDED_FRAME_BOUNDS = 9
            hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
                wintypes.HWND(hwnd), ctypes.c_uint(9),
                ctypes.byref(ext), ctypes.sizeof(ext))
            if hr == 0 and (ext.right - ext.left) > 0 and (ext.bottom - ext.top) > 0:
                return [ext.left, ext.top, ext.right, ext.bottom], "dwm_extended_frame"
        except Exception:
            pass
        r = win32gui.GetWindowRect(hwnd)
        return list(r), "get_window_rect"

    def _cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            t = win32gui.GetWindowText(hwnd) or ""
            if needle in t.lower():
                rect, src = _bounds(hwnd)
                hits.append({"hwnd": hwnd, "title": t, "rect": rect, "source": src})
        except Exception:
            return

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception as exc:
        return {"ok": False, "error": f"枚举窗口失败：{exc}"}

    if not hits:
        return {"ok": False, "error": f"没有标题含 {title_substr!r} 的可见窗口",
                "searched": True}
    # 取面积最大的那个（游戏主窗口通常最大；同名小窗如启动器会被排掉）
    def _area(h):
        r = h["rect"]
        return max(0, r[2] - r[0]) * max(0, r[3] - r[1])

    best = max(hits, key=_area)
    return {
        "ok": True,
        "hwnd": best["hwnd"],
        "title": best["title"],
        "rect": best["rect"],
        "rect_source": best["source"],
        "dpi_awareness": dpi_mode or "none",
        "candidates": len(hits),
    }


# ---------------------------------------------------------------- 截图

def grab(region: list[int] | None = None, window: str = "",
         save_path: str = "", max_side: int = 0) -> dict:
    """截取屏幕（或屏幕的一部分）并存成 PNG。返回**文件路径**，不返回 base64。

    Args:
        region:    [x, y, w, h]，屏幕绝对坐标；不传则截整个主屏。
        window:    窗口标题子串；给了就优先用该窗口的矩形（覆盖 region）。
        save_path: 保存路径；不传则落到临时目录并返回该路径。
        max_side:  最长边上限（等比例缩小）；0 = 不缩放。

    Returns:
        {ok, path, width, height, region, backend, ...}
    """
    backend = capture_backend_name()
    if not backend:
        a = available()
        return {"ok": False, "error": "没有可用的截图后端",
                "hint": a.get("capture_hint", "")}

    win_info = None
    if window:
        win_info = find_window_rect(window)
        if not win_info.get("ok"):
            # 窗口没找到 **不静默退化成整屏** —— 那会截到别的窗口，
            # 调用方却以为截的是游戏。如实报错，让调用方决定。
            return {"ok": False, "error": f"找不到窗口 {window!r}："
                    f"{win_info.get('error')}",
                    "hint": "可以改用 region=[x,y,w,h] 指定屏幕区域",
                    "window_search": win_info}
        r = win_info["rect"]
        region = [r[0], r[1], r[2] - r[0], r[3] - r[1]]

    t0 = time.time()
    img = None
    used = ""
    try:
        # region 允许 `[x,y,w,h]` 或 `"x,y,w,h"`（见 `_normalize_region`）。
        # 旧实现在两处分支各自 `for v in region`，传字符串时会逐字符迭代
        # 并抛 ValueError —— 同一个缺陷的另一半。
        if region:
            _reg = _normalize_region(region)
            if _reg is None:
                return {"ok": False,
                        "error": f"region 格式不对：{region!r}，"
                                 f"应为 [x,y,w,h] 或 \"x,y,w,h\""}
            region = _reg
        if backend == "dxcam":
            global _DXCAM
            import dxcam  # type: ignore
            if _DXCAM is None:
                _DXCAM = dxcam.create(output_color="RGB")
            cam = _DXCAM
            if cam is None:
                raise RuntimeError("dxcam.create() 返回 None（可能没有可用输出）")
            if region:
                x, y, w, h = region
                dx_region = (x, y, x + w, y + h)
            else:
                # ⚠️ 不能传 `region=(0, 0, None, None)` —— dxcam 内部会拿它
                # 跟整数比大小，抛 `'>=' not supported between instances of
                # 'int' and 'NoneType'`（实测）。整屏要**不传**这个参数。
                dx_region = None

            # ⚠️ dxcam 在「没有新帧」时返回 None —— 而**画面静止恰恰是
            # 验收场景的常态**（停在菜单上等我们点）。若把 None 当成失败，
            # 就会在完全正常的场景下报错，属于典型的**假失败**。
            # 所以：短暂重试若干次；仍拿不到就**回退 mss**（它总是返回
            # 当前帧），并如实标注实际用的后端。
            frame = None
            for attempt in range(4):
                frame = (cam.grab(region=dx_region) if dx_region is not None
                         else cam.grab())
                if frame is not None:
                    break
                time.sleep(0.05 * (attempt + 1))
            if frame is None:
                # ⚠️ `log` 是从 logutil 导入的 **logger 对象**，不是函数 ——
                # 必须 `log.debug(...)`。写成 `log("...")` 会抛
                # `'Logger' object is not callable`（实测踩到的缺陷：
                # 只在「dxcam 拿不到新帧、走这条回退分支」时才触发，
                # 平时跑到不，属于典型的**隐藏分支缺陷**）。
                log.debug("dxcam 连续 4 次未拿到新帧（画面静止），回退 mss")
                used_fallback = True
            else:
                used_fallback = False
            img = frame
            used = "dxcam"
        else:
            used_fallback = True

        if img is None:
            # mss 分支，或 dxcam 回退
            import mss  # type: ignore
            with mss.mss() as sct:
                if region:
                    x, y, w, h = region      # 已在上面规范化过
                    mon = {"left": x, "top": y, "width": w, "height": h}
                else:
                    mon = sct.monitors[1]
                shot = sct.grab(mon)
                img = _mss_to_array(shot)
            if used:
                used = used + "+mss"
            else:
                used = "mss"
            used_fallback = True
    except Exception as exc:
        return {"ok": False, "error": f"截图失败（后端 {backend}）：{exc}",
                "backend": backend}

    if img is None:
        return {"ok": False, "error": "截图返回空图像", "backend": backend}

    h, w = int(img.shape[0]), int(img.shape[1])

    # 缩放（只影响**存盘证据**，不影响坐标语义：本模块一律回绝对坐标）
    scaled_note = ""
    if max_side and max(h, w) > max_side:
        try:
            import cv2  # type: ignore
            scale = max_side / float(max(h, w))
            img = cv2.resize(img, (int(w * scale), int(h * scale)),
                             interpolation=cv2.INTER_AREA)
            h, w = int(img.shape[0]), int(img.shape[1])
            scaled_note = f"证据图等比缩放到最长边 {max_side}"
        except Exception as exc:
            scaled_note = f"缩放跳过（{exc}）"

    if not save_path:
        import tempfile
        save_path = os.path.join(tempfile.gettempdir(),
                                 f"gamemod_shot_{int(time.time()*1000)}.png")
    save_path = str(Path(save_path).expanduser())
    try:
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        _save_png(save_path, img)
    except Exception as exc:
        return {"ok": False, "error": f"保存截图失败：{exc}", "path": save_path}

    out: dict[str, Any] = {
        "ok": True,
        "path": save_path,
        "width": w,
        "height": h,
        "backend": used,
        "elapsed_ms": int((time.time() - t0) * 1000),
        # 明确告诉调用方这张图对应屏幕的哪一块，以及**别拿它当判据**
        "region": region or "full_primary_screen",
        "coordinate_space": "screen_absolute",
        "note": "截图是**证据**，不是判据。要判断「在哪个界面」请用 ocr()。",
    }
    if win_info:
        out["window"] = {"title": win_info.get("title"),
                         "rect": win_info.get("rect")}
    if scaled_note:
        out["scale_note"] = scaled_note
    return out


def _mss_to_array(shot) -> Any:
    """mss 的 ScreenShot → numpy RGB 数组（不依赖 mss 自带的转换，避免 BGRA 坑）。"""
    import numpy as np
    arr = np.frombuffer(shot.rgb, dtype=np.uint8)
    return arr.reshape((shot.height, shot.width, 3))


def _save_png(path: str, img) -> None:
    """存 PNG。优先用 cv2（能处理 RGB/BGR），回退 PIL。"""
    cv2 = _try_import("cv2")
    if cv2 is not None:
        # cv2 的 imwrite 按 BGR 解释，本模块统一 RGB，所以先转
        cv2.imwrite(path, cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
        return
    from PIL import Image  # type: ignore
    Image.fromarray(img).save(path)


# ---------------------------------------------------------------- OCR

def ocr(region: list[int] | None = None, window: str = "",
        expected: str = "", match: str = "contains",
        threshold: float = 0.3, case_sensitive: bool = False,
        max_items: int = 200, save_on_failure: str = "") -> dict:
    """对屏幕（或其中一块区域）做 OCR，返回**文本 + 屏幕绝对坐标**。

    这是「弱视觉」的主力：验收时绝大多数判断只需要
    「屏幕上有没有这几个字、它在哪」。

    **只对 ROI 做 OCR**（实测整屏 2560x1440 要 4.1 秒；ROI 通常几十毫秒）。

    Args:
        region:   [x, y, w, h] 屏幕绝对坐标；给了就只识别这一块。
        window:   窗口标题子串；用它定 ROI（与 region 二选一）。
        expected: 期望出现的文本；给了就做匹配并把命中的单独列出来。
        match:    `contains`（默认）/ `exact` / `regex`。
        threshold: OCR 置信度下限，低于它的结果丢弃（默认 0.3）。
        case_sensitive: 文本匹配是否区分大小写。
        max_items: 最多返回多少个文本框。
        save_on_failure: 给了路径时，若 `expected` 没匹配到，把截图存到这里
                   （等价 MaaFramework 的 `save_on_error`）—— 失败要留证据。

    Returns:
        {ok, engine, results: [{text, score, box, center, click_point}],
         matched: [...], coordinate_space: "screen_absolute", ...}
        坐标一律是**屏幕绝对坐标**，可直接用于点击，不必换算。
    """
    engine = ocr_engine_name()
    if not engine:
        a = available()
        return {"ok": False, "error": "没有可用的 OCR 引擎",
                "hint": a.get("ocr_hint", "")}

    # 定 ROI：窗口优先（与 grab 一致），拿不到就如实失败而不是截整屏
    origin_x, origin_y = 0, 0
    win_info = None
    if window:
        win_info = find_window_rect(window)
        if not win_info.get("ok"):
            return {"ok": False, "error": f"找不到窗口 {window!r}："
                    f"{win_info.get('error')}",
                    "hint": "可以改用 region=[x,y,w,h]",
                    "window_search": win_info}
        r = win_info["rect"]
        region = [r[0], r[1], r[2] - r[0], r[3] - r[1]]
    # region 允许两种形态：`[x, y, w, h]` 列表，或 `"x,y,w,h"` 字符串。
    #
    # ⚠️ 这两种**都必须支持**（实测踩到的缺陷）：
    # MCP 工具层收到的是字符串并已经 `_parse_region` 成列表；
    # 但本函数是**公开的引擎 API**，`devtools/verify_screen.py` 与
    # 分享包的验收脚本都会**直接**用 `[x,y,w,h]` 调它。
    # 旧实现只写了 `int(region[0])`：
    #   · 传列表 → 正常；
    #   · 传字符串 → `int(',')` 抛 ValueError（"0,0,300,120" 的 [1] 是逗号）。
    # 结果：**测试脚本直接调引擎 API 就崩**，而工具层调用却没事 ——
    # 一个只在「换个调用方」时才暴露的接口契约缺陷。
    if region:
        reg = _normalize_region(region)
        if reg is None:
            return {"ok": False,
                    "error": f"region 格式不对：{region!r}，"
                             f"应为 [x,y,w,h] 或 \"x,y,w,h\""}
        region = reg
        origin_x, origin_y = int(region[0]), int(region[1])

    shot = grab(region=region)
    if not shot.get("ok"):
        return {"ok": False, "error": f"OCR 前截图失败：{shot.get('error')}",
                "capture": shot}

    t0 = time.time()
    try:
        img = _load_rgb(shot["path"])
    except Exception as exc:
        return {"ok": False, "error": f"读取截图失败：{exc}"}

    try:
        if engine == "rapidocr_onnxruntime":
            items = _ocr_rapidocr(img)
        else:
            items = _ocr_tesseract(img)
    except Exception as exc:
        return {"ok": False, "error": f"OCR 失败（{engine}）：{exc}",
                "engine": engine, "image": shot.get("path")}

    elapsed = int((time.time() - t0) * 1000)

    results: list[dict] = []
    for it in items:
        if float(it.get("score") or 0) < threshold:
            continue
        x0, y0, x1, y1 = it["box"]
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        results.append({
            "text": it["text"],
            "score": round(float(it.get("score") or 0), 4),
            # 屏幕绝对坐标（已把 ROI 原点加回去）
            "box": [x0 + origin_x, y0 + origin_y, x1 + origin_x, y1 + origin_y],
            "center": [cx + origin_x, cy + origin_y],
            "click_point": [cx + origin_x, cy + origin_y],
        })
        if len(results) >= max_items:
            break

    out: dict[str, Any] = {
        "ok": True,
        "engine": engine,
        "elapsed_ms": elapsed,
        "count": len(results),
        "results": results,
        "texts": [r["text"] for r in results],
        "region": region or "full_primary_screen",
        # 这一行是给 Agent 的关键约定：坐标可直接用，不用换算
        "coordinate_space": "screen_absolute",
        "image": shot.get("path"),
        "threshold": threshold,
    }
    if win_info:
        out["window"] = {"title": win_info.get("title"),
                         "rect": win_info.get("rect")}

    # ---- 期望文本匹配
    if expected:
        hit = _match_all(results, expected, match, case_sensitive)
        out["expected"] = expected
        out["match"] = match
        out["matched"] = hit
        out["matched_count"] = len(hit)
        if hit:
            # 给一个「最该点的那个」：取置信度最高的一条（不是第一条 ——
            # 文本里出现两次时，先出现的不一定是想要的），并说明依据
            best = max(hit, key=lambda r: r.get("score") or 0)
            out["best_match"] = best
            out["click_point"] = best["click_point"]
        else:
            # 失败要留证据（等价 save_on_error）
            note = (f"ROI 内共识别到 {len(results)} 个文本框，"
                    f"但没有匹配 {expected!r} 的。"
                    f"这是「本次没找到」，**不等于**「游戏里没有」——"
                    f"可能是界面还没加载、或文字被拆成了多个文本框。")
            out["note"] = note
            if save_on_failure:
                try:
                    kept = grab(region=region, save_path=save_on_failure)
                    if kept.get("ok"):
                        out["failure_evidence"] = kept["path"]
                except Exception as exc:
                    out["failure_evidence_error"] = str(exc)
            else:
                out["failure_evidence"] = shot.get("path")
            # 不回滚 ok：OCR 本身成功了，只是没匹配到。调用方据
            # matched_count==0 判断即可 —— 但 note 必须说清区别。
    return out


def _load_rgb(path: str) -> Any:
    """读 PNG 成 RGB 数组。"""
    cv2 = _try_import("cv2")
    if cv2 is not None:
        bgr = cv2.imread(path)
        if bgr is None:
            raise RuntimeError(f"cv2 读不出这张图：{path}")
        return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    from PIL import Image  # type: ignore
    import numpy as np
    return np.array(Image.open(path).convert("RGB"))


#: RapidOCR 引擎实例缓存（实测初始化约 0.95 秒，不能每次重建）
_RAPID_OCR = None


def _ocr_rapidocr(img) -> list[dict]:
    """用 RapidOCR 识别，返回 [{text, score, box:[x0,y0,x1,y1]}]。"""
    global _RAPID_OCR
    if _RAPID_OCR is None:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
        _RAPID_OCR = RapidOCR()
    res, _elapse = _RAPID_OCR(img)
    out: list[dict] = []
    for row in (res or []):
        try:
            box_pts, text, score = row[0], row[1], row[2]
            xs = [float(p[0]) for p in box_pts]
            ys = [float(p[1]) for p in box_pts]
            out.append({
                "text": str(text),
                "score": float(score),
                "box": [int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))],
            })
        except Exception:
            continue
    return out


def _ocr_tesseract(img) -> list[dict]:
    """回退：tesseract。取词级框（`image_to_data`）。

    中文需要 `chi_sim` 语言包；没有就只用 `eng` 并如实标注（见 available()）。
    """
    import pytesseract  # type: ignore
    from PIL import Image

    langs = "eng"
    try:
        have = set(pytesseract.get_languages(config="") or [])
        if "chi_sim" in have:
            langs = "eng+chi_sim"
    except Exception:
        pass

    data = pytesseract.image_to_data(Image.fromarray(img), lang=langs,
                                     output_type=pytesseract.Output.DICT)
    out: list[dict] = []
    n = len(data.get("text") or [])
    for i in range(n):
        txt = (data["text"][i] or "").strip()
        if not txt:
            continue
        try:
            conf = float(data["conf"][i])
        except Exception:
            conf = 0.0
        if conf < 0:
            continue
        x, y = int(data["left"][i]), int(data["top"][i])
        w, h = int(data["width"][i]), int(data["height"][i])
        out.append({"text": txt, "score": conf / 100.0,
                    "box": [x, y, x + w, y + h]})
    return out


def _match_all(results: list[dict], expected: str, mode: str,
               case_sensitive: bool) -> list[dict]:
    """在 OCR 结果里找期望文本。三种模式：contains / exact / regex。"""
    hits: list[dict] = []
    if mode == "regex":
        import re
        try:
            pat = re.compile(expected,
                             0 if case_sensitive else re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"正则非法：{exc}") from exc
        for r in results:
            if pat.search(r.get("text") or ""):
                hits.append(r)
        return hits

    needle = expected if case_sensitive else expected.lower()
    for r in results:
        t = r.get("text") or ""
        hay = t if case_sensitive else t.lower()
        if mode == "exact":
            if hay == needle:
                hits.append(r)
        else:                       # contains（默认）
            if needle in hay:
                hits.append(r)
    return hits


# ---------------------------------------------------------------- 游戏日志

def read_log(path: str, pattern: str = "", max_lines: int = 200,
             tail_bytes: int = 262144, encoding: str = "auto") -> dict:
    """读游戏日志（或任意文本日志）的尾部，可按正则过滤。

    **为什么单独做这件事**：调研的 7 个游戏自动化 Bot 项目里，
    **没有一个**用游戏日志来判断结果 —— 它们靠视觉、注册表或 CDP。
    而用户明确要的验收流程里有一步是「通过日志确认结果」，所以这是
    本项目的差异化能力，也是「弱视觉」之外最可靠的结果确认通道。

    做法刻意保守：
      · 只读**尾部** `tail_bytes`（日志可能几百 MB，不许整个读进来）；
      · `pattern` 给正则时只返回匹配行，并**额外给匹配计数**；
      · 编码 `auto` 时按 utf-8 → utf-8-sig → gbk 依次试（中文游戏常见 GBK）。
    """
    p = Path(path).expanduser()
    if not p.is_file():
        return {"ok": False, "error": f"日志文件不存在：{path}",
                "hint": "确认路径；游戏日志常在 <游戏目录>/*.log、"
                        "或 %APPDATA%\\..\\LocalLow\\<厂商>\\<游戏>\\"}

    try:
        size = p.stat().st_size
        with p.open("rb") as f:
            start = max(0, size - int(tail_bytes))
            f.seek(start)
            raw = f.read()
    except Exception as exc:
        return {"ok": False, "error": f"读取日志失败：{exc}"}

    text, used_enc = _decode_auto(raw, encoding)
    if text is None:
        return {"ok": False, "error": "日志解码失败（试过 utf-8 / gbk）",
                "path": str(p), "size": size}

    lines = text.splitlines()
    truncated_head = start > 0
    if truncated_head and lines:
        lines = lines[1:]           # 首行可能被截断，丢弃避免误读

    matched: list[dict] = []
    if pattern:
        import re
        try:
            pat = re.compile(pattern)
        except re.error as exc:
            return {"ok": False, "error": f"正则非法：{exc}"}
        for i, ln in enumerate(lines):
            if pat.search(ln):
                matched.append({"line_no": i + 1, "text": ln})
        sel = matched
    else:
        sel = [{"line_no": i + 1, "text": ln} for i, ln in enumerate(lines)]

    out: dict[str, Any] = {
        "ok": True,
        "path": str(p),
        "size": size,
        "encoding": used_enc,
        "tail_bytes": tail_bytes,
        "truncated_head": truncated_head,
        "total_lines_in_tail": len(lines),
        "returned": min(len(sel), max_lines),
        "pattern": pattern or None,
    }
    if pattern:
        out["matched_count"] = len(matched)
    # 默认给**最后** max_lines 行（日志通常是「最新的在最后」）
    out["lines"] = sel[-max_lines:] if max_lines > 0 else sel
    if len(sel) > max_lines:
        out["has_more"] = True
        out["note"] = (f"尾部共 {len(lines)} 行，其中匹配 {len(sel)} 行，"
                       f"只返回最后 {max_lines} 行（`has_more=true`）")
    return out


def _decode_auto(raw: bytes, encoding: str) -> tuple[str | None, str]:
    """按 utf-8 → utf-8-sig → gbk 依次试解码。返回 (文本, 实际编码)。"""
    order = [encoding] if encoding and encoding != "auto" else \
            ["utf-8", "utf-8-sig", "gbk"]
    for enc in order:
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return None, ""
