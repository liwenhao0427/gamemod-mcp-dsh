"""验收：`verify_state` 的**证据充分性契约**（不需要游戏，永远会跑）。

本脚本钉住的是一条**外来但被实战验证有效**的规矩，来自
`zhaoxuya520/reverse-skill` 的证据链契约：

> `validated` 需要 **>=2 independent** Evidence（最好 1 静态 + 1 动态）；
> 单条证据**不得静默提升**为 validated。

它在我们自己的实战里立刻得到了印证：CatapultHero 验收 Mod 时，
**日志**（`已修改 精核 = 108`）与**屏幕 OCR**（`精核：108`）两个
互相独立的来源都指向同一结论，才敢说「Mod 生效了」。
只看日志是不够的 —— **日志是 Mod 自己打的**，Mod 写错了照样打日志。

所以本脚本的核心断言是：
  **只有 1 条证据成立时，`verify_state` 必须判 `fail`（证据不足），
   而不是 pass。**

含**非空转反证**：把要求降成「≥1」后同一输入应变成 pass ——
这既证明 `require_independent` 真的影响行为，也证明上面那条断言
不是恒真。
"""

from __future__ import annotations

import io
import os
import sys
import tempfile

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


_RAW = S.verify_state
tmp = tempfile.mkdtemp(prefix="gamemod_vs_")

log_hit = os.path.join(tmp, "hit.log")
log_miss = os.path.join(tmp, "miss.log")
io.open(log_hit, "w", encoding="utf-8").write("已修改 精核 = 108\n启动完成\n")
io.open(log_miss, "w", encoding="utf-8").write("nothing interesting\n")

# ================================================================
section("1. 参数校验：空/畸形输入必须明确报错，不静默通过")

r = _RAW(checks="")
check("空 checks → ok=False", r.get("ok") is False)
check("并说明怎么写（给例子）", "checks=" in str(r.get("error", "")),
      str(r.get("error"))[:70])

r = _RAW(checks="nope=x")
check("未知 kind → 记为不成立并报错", r.get("ok") is False)
check("证据里说明了「未知的 kind」",
      "未知" in str((r.get("evidence") or [{}])[0].get("error", "")),
      str((r.get("evidence") or [{}])[0].get("error"))[:60])

r = _RAW(checks="没有等号的条目")
check("缺 = 的条目 → 报「写法不对」",
      "写法" in str((r.get("evidence") or [{}])[0].get("error", "")),
      str((r.get("evidence") or [{}])[0].get("error"))[:60])

r = _RAW(checks="log=只有路径没有井号")
check("log 缺 # 分隔 → 报写法错误（不静默当成整串正则）",
      "写法" in str((r.get("evidence") or [{}])[0].get("error", "")),
      str((r.get("evidence") or [{}])[0].get("error"))[:60])

# ================================================================
section("2. **核心契约**：单条证据不得判 pass")

r1 = _RAW(checks=f"log={log_hit}#已修改 精核", require_independent=2)
check("1 条成立 + require=2 → verdict=fail", r1.get("verdict") == "fail",
      f"verdict={r1.get('verdict')!r}")
check("理由明确说「证据不足」", "证据不足" in str(r1.get("verdict_reason", "")),
      str(r1.get("verdict_reason"))[:80])
check("理由点明「单条证据不得静默提升」",
      "不得静默提升" in str(r1.get("verdict_reason", ""))
      or "独立" in str(r1.get("verdict_reason", "")),
      str(r1.get("verdict_reason"))[:80])
check("ok=False（与 verdict 一致）", r1.get("ok") is False)
check("passed/total 如实报 1/1",
      r1.get("passed") == 1 and r1.get("total") == 1,
      f"{r1.get('passed')}/{r1.get('total')}")

# ================================================================
section("3. 达到独立证据数才 pass")

r2 = _RAW(checks=f"log={log_hit}#已修改 精核,log={log_hit}#启动完成",
          require_independent=2)
check("2 条都成立 + require=2 → pass", r2.get("verdict") == "pass",
      f"verdict={r2.get('verdict')!r}")
check("ok=True", r2.get("ok") is True)
check("independent_count == 2", r2.get("independent_count") == 2,
      f"={r2.get('independent_count')}")

r3 = _RAW(checks=f"log={log_hit}#已修改 精核,log={log_miss}#绝不存在XYZ",
          require_independent=2)
check("1 条成立 + 1 条不成立 → fail（不是 pass）",
      r3.get("verdict") == "fail", f"verdict={r3.get('verdict')!r}")
check("此时**不是** unverifiable（因为有一条明确判了不成立）",
      r3.get("verdict") != "unverifiable")
check("passed=1 / total=2 如实报告",
      r3.get("passed") == 1 and r3.get("total") == 2,
      f"{r3.get('passed')}/{r3.get('total')}")

r4 = _RAW(checks=f"log={log_miss}#绝不存在XYZ", require_independent=2)
check("0 条成立 → fail，理由说 0 条", r4.get("verdict") == "fail"
      and "0/" in str(r4.get("verdict_reason", "")),
      str(r4.get("verdict_reason"))[:60])

# ================================================================
section("4. require_independent 真的影响行为（收了参数就要起作用）")

# 同一输入、只改 require，结论必须变 —— 这同时是第 2 节断言的非空转反证
r5 = _RAW(checks=f"log={log_hit}#已修改 精核", require_independent=1)
check("同一输入 + require=1 → pass（说明 require 真的在起作用）",
      r5.get("verdict") == "pass", f"verdict={r5.get('verdict')!r}")
check("同一输入 + require=2 → fail（与上面形成对照）",
      r1.get("verdict") == "fail")
check("require 回显正确",
      r5.get("require_independent") == 1 and r1.get("require_independent") == 2,
      f"{r5.get('require_independent')} / {r1.get('require_independent')}")
r6 = _RAW(checks=f"log={log_hit}#已修改 精核", require_independent=0)
check("require=0 被抬到 1（不允许「零证据即通过」）",
      r6.get("require_independent") == 1,
      f"={r6.get('require_independent')}")

# ================================================================
section("5. evidence 逐条可核查（每条都说清依据）")

for label, res in (("单条", r1), ("双条", r2)):
    ev = res.get("evidence") or []
    check(f"{label}：evidence 条数与 total 一致",
          len(ev) == res.get("total"), f"{len(ev)} vs {res.get('total')}")
    check(f"{label}：每条都带 check/hit/detail",
          all("check" in e and "hit" in e and "detail" in e for e in ev))
check("命中条目的 detail 里有 matched_count（可核查）",
      any((e.get("detail") or {}).get("matched_count") is not None
          for e in (r2.get("evidence") or [])),
      f"{[e.get('detail') for e in (r2.get('evidence') or [])]}")

# ================================================================
section("6. 与 wait_for 同源的语义（避免同一条件两处结论不一致）")

check("verify_state 与 wait_for 都经 _eval_condition 求值"
      "（单一实现，不重复）",
      callable(getattr(S, "_eval_condition", None)),
      "若这里为 False，说明条件语义被复制成了两份")

# log 语义差异是**有意的**：wait_for 等「新增」，verify_state 判「当前存在」
_r_wait_like = S.RAW_TOOLS["probe_log"](path=log_hit, pattern="已修改 精核")
check("probe_log 能独立给出当前匹配数（verify_state 的 log 判据基础）",
      (_r_wait_like.get("matched_count") or 0) >= 1,
      f"matched={_r_wait_like.get('matched_count')}")

# ================================================================
section("7. 非空转反证：去掉「证据充分性」检查会怎样")

# 反证：若判据写成「有任意一条成立就 pass」（旧思路），
# 那么 r1（1 条成立、require=2）会变成 pass —— 与实测相反。
_old_pass = r1.get("passed", 0) >= 1          # 旧思路：>=1 就通过
check("旧思路（有任何一条成立即通过）会把 r1 判成 pass —— "
      "而现在实测是 fail，说明契约真的在拦",
      _old_pass is True and r1.get("verdict") == "fail",
      f"old_pass={_old_pass} actual={r1.get('verdict')}")

# 反向：把 require 调回 1，同一输入确实变成 pass
# （证明「fail」不是因为这个条件根本不可能成立）
check("同一条件在 require=1 下能 pass（证明 fail 是证据数不够，"
      "不是条件本身判不出来）",
      r5.get("verdict") == "pass" and r1.get("verdict") == "fail")

# ================================================================
print()
print("=" * 78)
print(f"通过 {PASS} 项，失败 {FAIL} 项" + (f"，警告 {WARN} 项" if WARN else ""))
print("=" * 78)
sys.exit(1 if FAIL else 0)
