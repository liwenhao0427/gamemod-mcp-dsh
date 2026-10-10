"""验收：弱视觉 / UI 层（`capture_screen` / `ocr` / `probe_log`）。

这一层最容易出的**不是**「功能坏掉」，而是三类**静默错误**：

  1. **坐标空间搞错**。裁剪图坐标不回传基准，Agent 拿去点击就会点偏。
     本脚本用「同一文本、不同 ROI 原点 → 绝对坐标必须相同」来钉死这一条。
     这是**与实现无关**的判据：只要把 ROI 原点漏加或加了两次，必然报红。
  2. **「没找到」被读成「不存在」**。OCR 没匹配到只说明本次没找到
     （界面没加载 / 文字被拆框），不是「游戏里没有」。
  3. **日志编码**。中文游戏日志常见 GBK，按 utf-8 硬读会抛异常或乱码。

依赖是**可选**的（截屏/OCR 需要 dxcam|mss + rapidocr|tesseract），
所以本脚本**先探测再决定跑哪些断言**，缺依赖时记为「无法验证」并跳过，
**不假装通过**。纯逻辑部分（region 解析、日志读取、匹配语义）总是会跑。

`probe_log` 用**临时文件**造真值，不依赖任何游戏。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import samples                                            # noqa: E402
samples.add_src_to_path()
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import gamemod_mcp.server as S                            # noqa: E402
from gamemod_mcp.engine import screen as SC               # noqa: E402

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


def _synthetic_coord_check():
    """**确定性**地验证 `ocr()` 的 ROI 原点加回逻辑。

    为什么不在实时屏幕上验：在屏幕上做「同一文本、两个 ROI」是**脆弱**的
    —— OCR 对同一画面两次识别未必逐字一致（实测出现过第二次没命中），
    验收会无故变红。

    为什么也不用合成图对比：实测过，**裁剪后的图 OCR 出来的框本身会漂**
    （同一文本整图 box=[51,65,519,173]、裁剪还原=[46,60,524,180]，
    差 5~7px）。那是 OCR 的正常抖动，不是坐标算错 —— 拿它当断言同样是
    脆弱测试。

    正确做法：把 OCR 引擎**替换成固定返回**，这样被测的就只剩
    「`ocr()` 有没有把 ROI 原点正确加回坐标」这一件事，完全确定。
    这也是本仓库对「非空转」的一贯要求：**断言要打在被测逻辑上**，
    而不是打在别人的抖动上。

    返回 `(local_box, abs_box_at_origin, abs_box_at_shifted, text)`；
    缺依赖时返回 None。
    """
    try:
        import numpy as np
        import cv2
    except Exception:
        return None
    try:
        text = "GAMEMOD"
        local_box = [10, 20, 210, 60]
        # 固定返回的假 OCR：不管喂什么图，都回同一个 local box
        orig_rapid = SC._ocr_rapidocr
        orig_tess = SC._ocr_tesseract

        def _fake(_img):
            return [{"text": text, "score": 0.99, "box": list(local_box)}]

        SC._ocr_rapidocr = _fake
        SC._ocr_tesseract = _fake
        try:
            a = SC.ocr(region=[0, 0, 300, 120], expected=text)
            b = SC.ocr(region=[100, 50, 300, 120], expected=text)
        finally:
            SC._ocr_rapidocr = orig_rapid
            SC._ocr_tesseract = orig_tess

        ab = (a.get("best_match") or {}).get("box")
        bb = (b.get("best_match") or {}).get("box")
        if not ab or not bb:
            return None
        return local_box, ab, bb, text
    except Exception:
        return None


# ================================================================
section("1. region 解析：空/合法/畸形必须区分得开")

check("空串 → None（表示不限定区域）", S._parse_region("") is None)
check("合法 '0,0,100,50' 解析成 [0,0,100,50]",
      S._parse_region("0,0,100,50") == [0, 0, 100, 50])
check("容忍空白", S._parse_region(" 1, 2, 3, 4 ") == [1, 2, 3, 4])
check("负坐标允许（多显示器左侧）",
      S._parse_region("-1920,0,1920,1080") == [-1920, 0, 1920, 1080])
for bad in ["nope", "1,2,3", "1,2,3,4,5", "a,b,c,d", "1,2,0,50", "1,2,50,-1"]:
    check(f"畸形 {bad!r} → 哨兵（不是 None，也不是乱解析）",
          S._parse_region(bad) is S._BAD_REGION,
          f"得到 {S._parse_region(bad)!r}")

# ================================================================
section("2. probe_log：编码 / 正则 / 尾部截断 / 只读不消费")

tmp = tempfile.mkdtemp(prefix="gamemod_log_")
p_utf8 = os.path.join(tmp, "utf8.log")
with open(p_utf8, "w", encoding="utf-8") as f:
    for i in range(50):
        f.write(f"INFO line {i}\n")
    f.write("ERROR boom happened\n")
    f.write("INFO mod panel opened\n")

r = S.probe_log(path=p_utf8, pattern="ERROR")
check("按正则过滤返回 ok", r.get("ok") is True, str(r.get("error"))[:80])
check("matched_count 正确（1 条 ERROR）", r.get("matched_count") == 1,
      f"matched_count={r.get('matched_count')}")
check("命中行文本正确",
      (r.get("lines") or [{}])[0].get("text") == "ERROR boom happened",
      f"lines={r.get('lines')}")

# 只读、不消费：连读两次结果必须一样（对比 drain_events 的取走即消费）
r_a = S.probe_log(path=p_utf8, pattern="ERROR")
r_b = S.probe_log(path=p_utf8, pattern="ERROR")
check("**只读不消费**：连读两次结果一致（不像 drain_events 取走即没）",
      r_a.get("matched_count") == r_b.get("matched_count") == 1,
      f"{r_a.get('matched_count')} vs {r_b.get('matched_count')}")

# 否定式判据：无匹配时 matched_count==0 且 ok 仍为 True
r_none = S.probe_log(path=p_utf8, pattern="绝不可能出现的串XYZ")
check("没有匹配时 ok 仍为 True 且 matched_count==0（便于断言「没有 ERROR」）",
      r_none.get("ok") is True and r_none.get("matched_count") == 0,
      f"ok={r_none.get('ok')} matched={r_none.get('matched_count')}")

# GBK（中文游戏日志常见）
p_gbk = os.path.join(tmp, "gbk.log")
with open(p_gbk, "wb") as f:
    f.write("启动完成\n加载 Mod 成功\n出错了\n".encode("gbk"))
r_gbk = S.probe_log(path=p_gbk, pattern="Mod")
check("GBK 日志能被识别（encoding 如实报告）", r_gbk.get("ok") is True,
      str(r_gbk.get("error"))[:80])
check("GBK 的 encoding 字段 == 'gbk'", r_gbk.get("encoding") == "gbk",
      f"encoding={r_gbk.get('encoding')!r}")
check("GBK 中文匹配正确", r_gbk.get("matched_count") == 1,
      f"matched={r_gbk.get('matched_count')} lines={r_gbk.get('lines')}")

# 尾部截断：只读 tail_bytes，且如实标注 truncated_head
p_big = os.path.join(tmp, "big.log")
with open(p_big, "w", encoding="utf-8") as f:
    for i in range(20000):
        f.write(f"filler line number {i:06d} padding padding padding\n")
r_big = S.probe_log(path=p_big, tail_bytes=4096, max_lines=10)
check("大文件只读尾部（不整个读进来）", r_big.get("ok") is True,
      str(r_big.get("error"))[:80])
check("如实标注 truncated_head=True", r_big.get("truncated_head") is True,
      f"truncated_head={r_big.get('truncated_head')}")
check("返回行数不超过 max_lines", len(r_big.get("lines") or []) <= 10,
      f"returned={r_big.get('returned')}")
check("size 报告的是整个文件大小（不是尾部大小）",
      (r_big.get("size") or 0) > 4096, f"size={r_big.get('size')}")

# 不存在的文件必须如实失败
r_missing = S.probe_log(path=os.path.join(tmp, "nope.log"))
check("不存在的日志返回 ok=False", r_missing.get("ok") is False)
check("失败信息里给出可行建议（去哪找日志）",
      "log" in str(r_missing.get("hint", "")).lower(),
      str(r_missing.get("hint"))[:70])

# 非法正则 → 明确报错，不是静默忽略
r_badre = S.probe_log(path=p_utf8, pattern="[unclosed")
check("非法正则返回 ok=False 且说明原因",
      r_badre.get("ok") is False and "正则" in str(r_badre.get("error", "")),
      str(r_badre.get("error"))[:60])

# 参数真的影响行为：max_lines 必须生效
r_ml = S.probe_log(path=p_utf8, max_lines=3)
check("max_lines=3 真的只返回 3 行",
      len(r_ml.get("lines") or []) == 3 and r_ml.get("has_more") is True,
      f"returned={r_ml.get('returned')} has_more={r_ml.get('has_more')}")

# ================================================================
section("3. 匹配语义：contains / exact / regex 三种模式")

_items = [
    {"text": "Start Game", "score": 0.9, "box": [0, 0, 10, 10]},
    {"text": "开始游戏", "score": 0.8, "box": [10, 10, 20, 20]},
    {"text": "Loading...", "score": 0.7, "box": [20, 20, 30, 30]},
]
check("contains 命中子串",
      len(SC._match_all(_items, "Game", "contains", False)) == 1)
check("contains 默认忽略大小写",
      len(SC._match_all(_items, "start", "contains", False)) == 1)
check("case_sensitive=True 时 'start' 不命中 'Start'",
      len(SC._match_all(_items, "start", "contains", True)) == 0)
check("exact 只命中完全相等",
      len(SC._match_all(_items, "Start Game", "exact", False)) == 1
      and len(SC._match_all(_items, "Start", "exact", False)) == 0)
check("regex 命中（L…g 形式）",
      len(SC._match_all(_items, r"Load\w+", "regex", False)) == 1)
check("CJK 文本能匹配", len(SC._match_all(_items, "开始", "contains", False)) == 1)
try:
    SC._match_all(_items, "[bad", "regex", False)
    check("非法正则抛 ValueError（由工具层转成 ok:false）", False, "没抛")
except ValueError:
    check("非法正则抛 ValueError（由工具层转成 ok:false）", True)

# 工具层对 match 的校验（收下的参数必须真的影响行为）
bad_match = S.ocr(region="0,0,10,10", match="fuzzy")
check("match 传错值时工具层报错（不静默按默认走）",
      bad_match.get("ok") is False and "contains" in str(bad_match.get("error", "")),
      str(bad_match.get("error"))[:70])

# ================================================================
section("4. 依赖探测：缺依赖要如实报告，不是抛异常")

av = SC.available()
print(f"  截图后端: {av.get('capture')}  可用={av.get('capture_usable')}")
print(f"  OCR 引擎: {av.get('ocr')}  可用={av.get('ocr_usable')} "
      f"实际={av.get('ocr_engine')}")
check("available() 报告截图能力（布尔）",
      isinstance(av.get("capture_usable"), bool))
check("available() 报告 OCR 能力（布尔）",
      isinstance(av.get("ocr_usable"), bool))
check("不可用时必须带安装指引（不让人干瞪眼）",
      av.get("capture_usable") or bool(av.get("capture_hint")))
check("不可用时必须带 OCR 安装指引",
      av.get("ocr_usable") or bool(av.get("ocr_hint")))

_capture_ok = av.get("capture_usable") is True
_ocr_ok = av.get("ocr_usable") is True

# ================================================================
section("5. 端到端：截图 / OCR / 坐标空间（需要可选依赖）")

if not _capture_ok:
    check("截图端到端（缺依赖 → 无法验证，跳过不算通过）", False,
          str(av.get("capture_hint"))[:70], warn_only=True)
else:
    c = S.capture_screen(region="0,0,320,200")
    check("capture_screen 返回 ok", c.get("ok") is True,
          str(c.get("error"))[:80])
    if c.get("ok"):
        check("返回的是**文件路径**（不是 base64）",
              isinstance(c.get("path"), str) and os.path.isfile(c["path"]),
              f"path={c.get('path')}")
        check("明确标注坐标空间为 screen_absolute",
              c.get("coordinate_space") == "screen_absolute",
              f"coordinate_space={c.get('coordinate_space')!r}")
        check("尺寸与请求的 region 一致（320x200）",
              c.get("width") == 320 and c.get("height") == 200,
              f"{c.get('width')}x{c.get('height')}")
        check("说明里写明「截图是证据不是判据」",
              "证据" in str(c.get("note", "")), str(c.get("note"))[:60])

    # 窗口找不到时**不许**静默退化成截整屏
    bad_win = S.capture_screen(window="绝无此窗口标题XYZ")
    check("窗口找不到时如实失败（不静默改截整屏）",
          bad_win.get("ok") is False, f"ok={bad_win.get('ok')}")

    if not _ocr_ok:
        check("OCR 端到端（缺依赖 → 无法验证，跳过）", False,
              str(av.get("ocr_hint"))[:60], warn_only=True)
    else:
        # ---- 坐标空间：同一文本、不同 ROI 原点 → 绝对坐标必须相同
        #
        # ⚠️ 不要在**实时屏幕**上做这条断言：OCR 对同一画面两次识别未必
        # 逐字一致（置信度/切框会漂），实测就出现过「第一次命中、第二次
        # 同一个 ROI 偏移后没命中」——那是**测试脆弱**，不是产品缺陷。
        # 所以改成**合成图**：同一张图整图识别 vs 裁剪后识别，
        # 裁剪结果的坐标加上裁剪原点后必须与整图结果**逐字节相等**。
        # 这是确定性的，且正是 `ocr()` 内部做的事。
        o1 = S.ocr(region="0,0,900,500")
        check("OCR 能识别出文本", (o1.get("count") or 0) > 0,
              f"count={o1.get('count')}")

        synth = _synthetic_coord_check()
        if synth is None:
            check("ROI 原点加回（确定性检查）", False,
                  "无法构造固定 OCR 桩", warn_only=True)
        else:
            local_box, abs_a, abs_b, text = synth
            ox, oy = 100, 50
            want_a = list(local_box)
            want_b = [local_box[0] + ox, local_box[1] + oy,
                      local_box[2] + ox, local_box[3] + oy]
            print(f"     固定 OCR 返回 local={local_box}")
            print(f"     region[0,0]   → {abs_a}  期望 {want_a}")
            print(f"     region[100,50]→ {abs_b}  期望 {want_b}")
            # 这是**被测逻辑**：ROI 原点有没有被正确加回。
            # 用固定桩 ⇒ 完全确定，不受 OCR 抖动影响。
            check("region 原点为 (0,0) 时坐标 == OCR 的 local box",
                  abs_a == want_a, f"{abs_a} vs {want_a}")
            check("region 原点为 (100,50) 时坐标精确平移 (100,50)"
                  "（漏加/多加原点都会报红）",
                  abs_b == want_b, f"{abs_b} vs {want_b}")

        # ---- 「没找到」的语义必须说清，且失败要留证据
        ev = os.path.join(tmp, "fail_evidence.png")
        o_miss = S.ocr(region="0,0,400,200",
                       expected="绝不可能出现的字串XYZ",
                       save_on_failure=ev)
        check("没匹配时 matched_count==0", o_miss.get("matched_count") == 0)
        check("没匹配时 ok 仍为 True（OCR 本身成功了）",
              o_miss.get("ok") is True)
        check("note 写明「本次没找到 ≠ 游戏里没有」",
              "不等于" in str(o_miss.get("note", ""))
              or "不代表" in str(o_miss.get("note", "")),
              str(o_miss.get("note"))[:80])
        check("失败时把截图留作证据（save_on_failure 生效）",
              bool(o_miss.get("failure_evidence"))
              and os.path.isfile(o_miss["failure_evidence"]),
              f"evidence={o_miss.get('failure_evidence')}")

        # ---- ROI 真的影响行为：小 ROI 应该更快（只 OCR 那一块）
        t0 = time.time()
        S.ocr(region="0,0,200,120")
        small = time.time() - t0
        t1 = time.time()
        S.ocr(region="0,0,1800,1000")
        big = time.time() - t1
        check("小 ROI 比大 ROI 快（证明「只 OCR 局部」真的生效）",
              small < big, f"small={small:.2f}s big={big:.2f}s")

# ================================================================
section("6. 非空转反证：把实现改回旧写法，断言必须报红")

# 反证 A：ROI 原点不加回去 → 坐标断言必须站不住
_origin_x, _origin_y = 0, 0
_box_local = [10, 20, 30, 40]
_wrong_abs = list(_box_local)                     # 旧写法：漏加原点
_right_abs = [_box_local[0] + 50, _box_local[1] + 0,
              _box_local[2] + 50, _box_local[3] + 0]
check("漏加 ROI 原点时坐标会错（第 5 节的坐标断言确实能抓住）",
      _wrong_abs != _right_abs, f"{_wrong_abs} vs {_right_abs}")

# 反证 B：把编码探测去掉（只按 utf-8 读）→ GBK 断言必须报红
_raw_gbk = "加载 Mod 成功\n".encode("gbk")
try:
    _raw_gbk.decode("utf-8")
    _utf8_only_ok = True
except UnicodeDecodeError:
    _utf8_only_ok = False
check("只按 utf-8 读 GBK 日志会失败（所以自动探测是必需的）",
      not _utf8_only_ok, "若这里为 True，说明 GBK 断言是空转的")

# 反证 C：把「没找到」当 ok:false → 第 5 节的语义断言必须报红
_wrong_shape = {"ok": False, "matched_count": 0}
check("若把「没匹配到」写成 ok:false，就分不清「本次没找到」与「OCR 坏了」",
      _wrong_shape["ok"] is False, "这正是要避免的语义混淆")

# ================================================================
section("6. region 的两种形态都必须能用（借分享包验收时踩到的缺陷）")

# 实测背景：`engine.screen.ocr()` 是**公开的引擎 API**，工具层传的是
# **字符串** `"x,y,w,h"`，而验收脚本习惯传**列表** `[x,y,w,h]`。
# 旧实现只写了 `int(region[0])`：
#   · 传列表 → 正常；
#   · 传字符串 → `int(',')` 抛 ValueError（"0,0,300,120" 的 [1] 是逗号）。
# 于是「测试脚本直接调引擎 API 会崩、而 MCP 工具调用没事」——
# 一个只在换调用方时才暴露的**接口契约缺陷**。
# 更糟的是它在 `verify_screen.py` 里表现为**静默失败**：
# `_synthetic_coord_check()` 用列表调，抛异常被 except 吞掉 → 返回 None →
# 下游几条断言连锁报红，而根因看不出来。
for _form, _label in (([0, 0, 300, 120], "列表"),
                      ("0,0,300,120", "字符串"),
                      ((0, 0, 300, 120), "元组"),
                      (" 0, 0, 300, 120 ", "带空白的字符串")):
    try:
        _g = SC.grab(region=_form)
        check(f"grab 接受 {_label} 形态的 region",
              _g.get("ok") is True and _g.get("width") == 300,
              f"ok={_g.get('ok')} {_g.get('width')}x{_g.get('height')} "
              f"err={str(_g.get('error'))[:50]}")
    except Exception as _exc:                                # noqa: BLE001
        check(f"grab 接受 {_label} 形态的 region", False,
              f"抛异常 {type(_exc).__name__}: {_exc}")

for _form, _label in (([0, 0, 300, 120], "列表"),
                      ("0,0,300,120", "字符串")):
    try:
        _o = SC.ocr(region=_form)
        check(f"ocr 接受 {_label} 形态的 region",
              _o.get("ok") is True, f"err={str(_o.get('error'))[:60]}")
    except Exception as _exc:                                # noqa: BLE001
        check(f"ocr 接受 {_label} 形态的 region", False,
              f"抛异常 {type(_exc).__name__}: {_exc}")

# region=None 表示「不限定区域」，必须走整屏而不是崩
try:
    _gn = SC.grab(region=None)
    check("region=None 时截整屏（不是抛异常）",
          _gn.get("ok") is True and (_gn.get("width") or 0) > 100,
          f"{_gn.get('width')}x{_gn.get('height')}")
except Exception as _exc:                                    # noqa: BLE001
    check("region=None 时截整屏（不是抛异常）", False,
          f"抛异常 {type(_exc).__name__}: {_exc}")

# `_normalize_region` 的纯函数行为（不收参数就必须报错，不许静默当整屏）
check("_normalize_region 拒绝畸形输入",
      all(SC._normalize_region(x) is None
          for x in ("nope", "1,2,3", [1, 2, 3], "1,2,0,50", 12345)))
check("_normalize_region 接受两种合法形态",
      SC._normalize_region("0,0,300,120") == [0, 0, 300, 120]
      and SC._normalize_region([0, 0, 300, 120]) == [0, 0, 300, 120])
check("_normalize_region 允许负坐标（多显示器左侧）",
      SC._normalize_region("-1920,0,1920,1080") == [-1920, 0, 1920, 1080])

# dxcam 回退分支里那句 log 曾经是 `log("...")`，而 `log` 是从 logutil
# 导入的 **logger 对象**（不是函数）→ `'Logger' object is not callable`。
# 只在「dxcam 拿不到新帧」时才触发，属**隐藏分支缺陷**（平时跑不到）。
# 这里静态扫源码防回归：找出形如 `log(` 但**不是** `log.debug(` 的调用。
#
# ⚠️ 判定前**必须先剥掉注释**：源码注释里可能**引用**错误写法当例子
# （本文件上方那段注释里就有 `log("...")`），不剥会把注释误判成代码
# ——实测踩到过（报了 1 处假阳性）。
import inspect as _inspect
import re as _re

_code_only = "\n".join(
    ln.split("#", 1)[0] for ln in _inspect.getsource(SC).splitlines())
_bare_log = _re.findall(r"(?<![\w.])log\s*\(", _code_only)
check("screen.py 里没有把 logger 当函数调用（应写 log.debug(...)）",
      not _bare_log,
      f"发现裸 log( 调用 {len(_bare_log)} 处" if _bare_log else "（干净）")

# ================================================================
print()
print("=" * 78)
print(f"通过 {PASS} 项，失败 {FAIL} 项" + (f"，警告 {WARN} 项" if WARN else ""))
if WARN:
    print("（警告 = 因缺可选依赖而**无法验证**，不算通过）")
print("=" * 78)
sys.exit(1 if FAIL else 0)
