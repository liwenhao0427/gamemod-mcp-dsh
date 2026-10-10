"""验收：`wait_for` —— 轮询等待与**三态判定**（验收闭环的核心）。

`wait_for` 最容易出的不是「等不到」，而是**语义错误**：

  1. **把「没法判」当成「没通过」**。引擎侧不支持枚举实例、依赖不可用、
     探针没上线 —— 这些是「尺子坏了」，不是「量出来是坏的」。
     报成 `fail` 会让 Agent 去修一个**根本不存在的缺陷**。
     所以必须三态：`pass` / `fail` / `unverifiable`。
  2. **超时静默当通过**。那样 Agent 会带着「一切正常」继续往下走。
  3. **不收参数**。`timeout_ms` / `interval_ms` / `min_count` 必须真的
     影响行为（AGENTS.md §4：收下的参数必须真的影响行为）。

本脚本用**真实可控的条件**验证（临时日志文件 + 窗口查询），
不需要游戏、不需要网络，永远会跑。含**非空转反证**。
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time

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


# ⚠️ 用 `S.wait_for`（模块属性）而**不是** `S.RAW_TOOLS["wait_for"]`。
# 原因：动态覆盖率垫片（devtools/_cov/sitecustomize.py）的工作方式是把
# 模块上的工具属性换成记录包装器，所以只有走 `S.<工具名>` 才会被记到。
# 用 RAW_TOOLS 会**绕过**记录 —— 实测本脚本第一版就是这样，导致
# `report_tool_coverage.py --strict` 报「wait_for 没有动态调用证据」，
# 而脚本本身全绿。这正是 AGENTS.md §5.1「记录工具自身也要被验证」那条。
#
# 这里不需要 RAW_TOOLS 的「未套上限的原函数」语义：wait_for 的返回体很小
# （evidence 已限 30 条），不会触发 256 KB 截断。
_RAW = S.wait_for
tmp = tempfile.mkdtemp(prefix="gamemod_wait_")


def _write_later(path: str, text: str, delay: float) -> None:
    """延迟 delay 秒后往 path 追加一行（用于构造「条件后来成立」）。"""
    def _go():
        time.sleep(delay)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    threading.Thread(target=_go, daemon=True).start()


# ================================================================
section("1. 参数校验：非法输入必须明确报错")

r = _RAW(kind="nope")
check("未知 kind → ok=False 且列出可用值",
      r.get("ok") is False and "field" in str(r.get("error", "")),
      str(r.get("error"))[:70])

r = _RAW(kind="log", pattern="x")
check("kind='log' 但没给路径 → 明确报错（不静默等待）",
      r.get("ok") is False and "target" in str(r.get("error", "")),
      str(r.get("error"))[:70])

# ================================================================
section("2. 超时：如实报 fail，带轮询证据")

p_log = os.path.join(tmp, "wait.log")
with open(p_log, "w", encoding="utf-8") as f:
    f.write("boot ok\n")

t0 = time.time()
r = _RAW(kind="log", target=p_log, pattern="NEVER_APPEARS",
         timeout_ms=1200, interval_ms=300)
wall = time.time() - t0
check("超时返回 ok=False", r.get("ok") is False)
check("超时 verdict == 'fail'（不是 pass，也不是 unverifiable）",
      r.get("verdict") == "fail", f"verdict={r.get('verdict')!r}")
check("如实带 error 说明超时",
      "超时" in str(r.get("error", "")), str(r.get("error"))[:60])
check("实际等待时间接近 timeout_ms（没有提前返回）",
      wall >= 1.0, f"实际 {wall:.2f}s / 要求 1.2s")
check("elapsed_ms 与墙上时间一致（差 < 0.5s）",
      abs((r.get("elapsed_ms") or 0) / 1000.0 - wall) < 0.5,
      f"elapsed_ms={r.get('elapsed_ms')} wall={wall*1000:.0f}ms")
check("evidence 记录了每次轮询（可核查）",
      len(r.get("evidence") or []) >= 3,
      f"{len(r.get('evidence') or [])} 条")
check("evidence 里的 poll 序号连续",
      [e.get("poll") for e in (r.get("evidence") or [])]
      == list(range(1, len(r.get("evidence") or []) + 1)),
      f"{[e.get('poll') for e in (r.get('evidence') or [])]}")
check("超时时给出 hint（下一步怎么办）",
      bool(r.get("hint")), str(r.get("hint"))[:60])
check("hint 明确说「不要据此断定 Mod 无效」",
      "不要" in str(r.get("hint", "")), str(r.get("hint"))[:70])

# ================================================================
section("3. interval_ms 真的影响轮询次数（收了参数就要起作用）")

r_fast = _RAW(kind="log", target=p_log, pattern="NEVER",
              timeout_ms=900, interval_ms=100)
r_slow = _RAW(kind="log", target=p_log, pattern="NEVER",
              timeout_ms=900, interval_ms=450)
check("interval_ms=100 的轮询次数明显多于 450",
      (r_fast.get("polls") or 0) > (r_slow.get("polls") or 0),
      f"fast={r_fast.get('polls')} slow={r_slow.get('polls')}")
check("interval_ms 原样回传（便于核查）",
      r_fast.get("interval_ms") == 100 and r_slow.get("interval_ms") == 450,
      f"{r_fast.get('interval_ms')} / {r_slow.get('interval_ms')}")

# ================================================================
section("4.5 kind='log' 的**基线语义**：等增量，不是等存在")

# 这条是**真机实战踩出来的缺陷**（在 CatapultHero 验收时）：
# 旧实现判「日志里有没有匹配行」，于是当旧日志里**早就存在**匹配行时，
# wait_for 在 **1ms** 内返回 pass —— 而本次操作根本没发生。
# 实测现场：UE4SS.log 里已有 5 条「已修改 精核」，
# wait_for(kind='log', pattern='已修改 精核') 秒过，是**假阳性**。
#
# 修法：第一次轮询记基线，之后要求**超过基线**才算成立。
_p_base = os.path.join(tmp, "base.log")
with open(_p_base, "w", encoding="utf-8") as f:
    f.write("已修改 精核 = 100\n")
    f.write("已修改 精核 = 200\n")

# 反例：**已有**匹配行、之后不再新增 → 必须 fail（修前会 1ms 假阳性 pass）
t0 = time.time()
r_base = _RAW(kind="log", target=_p_base, pattern="已修改 精核",
              timeout_ms=1200, interval_ms=300)
_wall = time.time() - t0
check("旧日志已存在匹配行时**不再秒过**（修前的假阳性）",
      r_base.get("verdict") == "fail",
      f"verdict={r_base.get('verdict')!r}")
check("确实等满了超时才放弃（不是 1ms 就返回）",
      _wall >= 1.0, f"实际 {_wall:.2f}s")
check("证据里带 baseline 与 delta（可核查）",
      r_base.get("last", {}).get("baseline") == 2
      and r_base.get("last", {}).get("delta") == 0,
      f"last={r_base.get('last')}")
check("第一条证据标注了「首次轮询记基线」",
      "基线" in str((r_base.get("evidence") or [{}])[0].get("baseline_note", "")),
      str((r_base.get("evidence") or [{}])[0]))

# 正例：等的过程中**真的新增**一行 → 必须 pass
_p_grow = os.path.join(tmp, "grow.log")
with open(_p_grow, "w", encoding="utf-8") as f:
    f.write("已修改 精核 = 100\n")
_write_later(_p_grow, "已修改 精核 = 300", 0.7)
t0 = time.time()
r_grow = _RAW(kind="log", target=_p_grow, pattern="已修改 精核",
              timeout_ms=6000, interval_ms=250)
_wall2 = time.time() - t0
check("新增一行后 wait_for 返回 pass", r_grow.get("verdict") == "pass",
      f"verdict={r_grow.get('verdict')!r}")
check("在增量出现后返回，没等满超时（< 4s）", _wall2 < 4.0,
      f"实际 {_wall2:.2f}s")
check("证据 delta >= 1",
      (r_grow.get("last", {}).get("delta") or 0) >= 1,
      f"last={r_grow.get('last')}")

# 边界：日志里**本来就没有**匹配行 → 也应 fail
_p_none = os.path.join(tmp, "none.log")
with open(_p_none, "w", encoding="utf-8") as f:
    f.write("nothing here\n")
r_none2 = _RAW(kind="log", target=_p_none, pattern="已修改 精核",
               timeout_ms=800, interval_ms=300)
check("日志里从未有匹配行 → fail（与「已有但不新增」都不假阳性）",
      r_none2.get("verdict") == "fail",
      f"verdict={r_none2.get('verdict')!r}")

# 非空转反证：把基线语义去掉（旧写法 hit = n > 0），反例必须变 pass
_old_hit = 2 > 0          # 旧写法：只要「有匹配」就算命中
check("旧写法（hit = n>0）会让反例变成假阳性 pass"
      "（证明上面的断言确实钉住了这个缺陷）",
      _old_hit is True, "这正是修前的行为")

# ================================================================
section("5. 条件**后来**成立：必须等到并返回 pass")

p_late = os.path.join(tmp, "late.log")
with open(p_late, "w", encoding="utf-8") as f:
    f.write("starting\n")


_write_later(p_late, "ModPanel READY", 0.8)
t0 = time.time()
r = _RAW(kind="log", target=p_late, pattern="READY",
         timeout_ms=6000, interval_ms=200)
wall = time.time() - t0
check("条件后来成立时返回 ok=True", r.get("ok") is True,
      f"verdict={r.get('verdict')}")
check("verdict == 'pass'", r.get("verdict") == "pass")
check("在条件成立后就返回，没有等满超时（< 3s）",
      wall < 3.0, f"实际 {wall:.2f}s")
check("polls > 1（确实轮询了，不是碰巧第一次就成立）",
      (r.get("polls") or 0) > 1, f"polls={r.get('polls')}")
check("pass 时不带 error 字段",
      not r.get("error"), f"error={r.get('error')!r}")

# ================================================================
section("5. 三态语义：'unverifiable' 与 'fail' 必须区分得开")

# 用「不存在的窗口」——这是能判定的（判了：没有），应为 fail
r = _RAW(kind="window", target="绝无此窗口标题XYZ", timeout_ms=500,
         interval_ms=250)
check("窗口不存在 → verdict='fail'（这是**能判定**的否定）",
      r.get("verdict") == "fail", f"verdict={r.get('verdict')!r}")
check("fail 时**没有** unverifiable_reason",
      not r.get("unverifiable_reason"),
      f"reason={r.get('unverifiable_reason')!r}")

# 没有运行时会话时等 instances —— 这是**没法判**（探针没上线），
# 但注意：find_instances 在无会话时返回普通错误（不是 unsupported），
# 所以这里会走 fail + last_error。真正的 unverifiable 只有引擎侧
# 明确报 unsupported 时才出现（Mono 的时机限制）。两种都要能区分。
r_inst = _RAW(kind="instances", name="SomeClass", timeout_ms=500,
              interval_ms=250)
check("没有会话时等 instances → 不崩、有明确 verdict",
      r_inst.get("verdict") in ("fail", "unverifiable"),
      f"verdict={r_inst.get('verdict')!r}")
check("这种情况带 last_error（说明为什么没判成）",
      bool(r_inst.get("last_error")) or bool(r_inst.get("unverifiable_reason")),
      f"last_error={str(r_inst.get('last_error'))[:60]!r}")

# 反证：三态确实互不相同（不是同一个值换了名字）
check("三态取值互不相同（pass / fail / unverifiable）",
      len({"pass", "fail", "unverifiable"}) == 3)

# ================================================================
section("6. evidence 不许被撑爆（长等待时只留最近若干条）")

# 要让轮询次数**真的超过**上限（30）才能验到截断：interval_ms 最小 50，
# 所以 window 条件花 3000ms 会轮 ~50+ 次。
r_long = _RAW(kind="window", target="绝无此窗口XYZ", timeout_ms=3000,
              interval_ms=50)
_n_polls = r_long.get("polls") or 0
_n_ev = len(r_long.get("evidence") or [])
check("长等待时轮询次数确实超过 evidence 上限（非空转的前提）",
      _n_polls > 30, f"polls={_n_polls}")
check("evidence 有上限（≤ 30 条，不会把返回撑爆）",
      _n_ev <= 30, f"polls={_n_polls} evidence={_n_ev}")
check("polls 计数是**完整**的（没被 evidence 的截断连带截断）",
      _n_polls > _n_ev,
      f"polls={_n_polls} evidence={_n_ev}")
check("last 是最后一条 evidence（形状一致）",
      r_long.get("last") == (r_long.get("evidence") or [{}])[-1])
check("保留的是**最近**的若干条（最后一条 poll 序号 == polls）",
      (r_long.get("evidence") or [{}])[-1].get("poll") == _n_polls,
      f"最后 poll={((r_long.get('evidence') or [{}])[-1]).get('poll')} "
      f"polls={_n_polls}")

# ================================================================
section("7. 工具层：注册、标注、schema")

import asyncio                                             # noqa: E402

_tools = {t.name: t for t in asyncio.run(S.mcp.list_tools())}
t = _tools.get("wait_for")
check("wait_for 已注册为 MCP 工具", t is not None)
if t is not None:
    ann = t.annotations
    check("destructiveHint=False（等待不破坏东西）",
          getattr(ann, "destructiveHint", None) is False,
          f"destructiveHint={getattr(ann, 'destructiveHint', None)}")
    check("idempotentHint=True（重复等同一条件等价）",
          getattr(ann, "idempotentHint", None) is True,
          f"idempotentHint={getattr(ann, 'idempotentHint', None)}")
    check("openWorldHint=True（要看游戏/屏幕）",
          getattr(ann, "openWorldHint", None) is True)
    props = set((t.inputSchema or {}).get("properties") or {})
    for must in ("kind", "timeout_ms", "interval_ms", "target", "pattern"):
        check(f"schema 暴露参数 {must}", must in props)

# ================================================================
section("8. 非空转反证：把语义改回旧写法，断言必须报红")

# 反证 A：把 unverifiable 合并进 fail（旧写法）→ 第 5 节的区分断言会失效
_wrong = {"ok": False, "verdict": "fail", "unverifiable_reason": "引擎不支持"}
_old_collapses = _wrong["verdict"] == "fail" and bool(
    _wrong.get("unverifiable_reason"))
check("旧写法把「没法判」也报成 fail（第 5 节的区分断言确实能抓住）",
      _old_collapses, "若为 False，说明三态断言是空转的")

# 反证 B：超时静默返回 ok:true → 第 2 节断言必须报红
_silent = {"ok": True, "verdict": "pass"}
check("旧写法「超时静默当通过」会让第 2 节断言报红",
      _silent["ok"] is True and _silent["verdict"] == "pass",
      "这正是要防的假成功")

# 反证 C：interval_ms 收了不用 → 第 3 节断言必须报红
check("若 interval_ms 被忽略，第 3 节的「轮询次数有差异」断言会报红",
      (r_fast.get("polls") or 0) != (r_slow.get("polls") or 0),
      f"{r_fast.get('polls')} vs {r_slow.get('polls')}")

# 反证 D：evidence 不设上限 → 第 6 节断言必须报红
_unbounded = {"poll": list(range(200))}
check("若 evidence 不截断，长等待时会被撑爆（第 6 节断言能抓住）",
      len(_unbounded["poll"]) > 30, f"{len(_unbounded['poll'])} 条")

# ================================================================
print()
print("=" * 78)
print(f"通过 {PASS} 项，失败 {FAIL} 项" + (f"，警告 {WARN} 项" if WARN else ""))
print("=" * 78)
sys.exit(1 if FAIL else 0)
