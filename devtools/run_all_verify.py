"""一条命令跑完所有验收脚本，并在结束时给出动态覆盖率。

为什么需要这个（而不是在 PowerShell 里写个 foreach）：

· **区分「断言失败」与「进程崩溃」**。`verify_agent_js.py` 会把 Frida
  注入**自己的进程**来验证探针能被编译加载 —— 这在 Windows 无显示环境
  下偶发访问违例（exit 0xC0000005），脚本自己抓不住（进程直接死了）。
  崩了不等于产品有问题，但**也不能当成通过**，所以要单独归类并重试。
· **重试必须留痕**。允许重试是为了吸收环境抖动；如果重试了却不报告，
  就等于把偶发失败悄悄洗成通过。所以这里会明确打印哪些脚本重试过。
· **结束时跑覆盖率门禁**，把「工具是否真的被调用过」一起卡住。

用法：
    python devtools/run_all_verify.py              # 全部 + 覆盖率门禁
    python devtools/run_all_verify.py --no-cov     # 只跑验收脚本
    python devtools/run_all_verify.py --retries 2  # 崩溃时最多重试几次
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time

# 中文 Windows 的控制台默认 GBK，本脚本会打印 ✅ / ❌ / 💥 之类的字符。
# 不做这一步，**汇总打印会在最后一步崩掉**（实测 'gbk' codec can't encode
# '\u2705'）—— 所有验收脚本都跑完了、结论却拿不到，是最亏的一种失败。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 已知会**自身崩溃**（而非断言失败）的脚本：Frida 自注入本进程，
# 无显示环境下偶发访问违例。给它们额外的重试预算。
SELF_ATTACH = {"verify_agent_js"}

# 退出码含义：0 通过；其余为失败。Windows 上被强杀/访问违例
# 会落在很大的负数或 1，这里统一按「崩溃」处理。
CRASH_CODES = {-1073741819, 3221225477, -1073740791, 3221225725}

#: 本脚本自己造出来的退出码：脚本超时被终止（见 `run()` 的注释）。
#: 与「崩溃」分开记 —— 一个是被系统/断言打断，一个是根本没结束。
TIMEOUT_CODE = -9

COV_LOG = os.path.join(ROOT, ".cache-test", "tool_calls.jsonl")
LOCK = os.path.join(ROOT, ".cache-test", "run_all_verify.lock")


def pid_alive(pid: int) -> bool:
    """进程是否还活着（Windows 上**不能**用 `os.kill(pid, 0)`）。

    Python 文档写得很清楚：Windows 上 `os.kill` 的第二个参数只要不是
    那两个控制台事件，就会被当成 TerminateProcess —— 拿它做「探活」
    会把目标进程**杀掉**。
    """
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return str(pid) in (out.stdout or "")


def acquire_lock() -> bool:
    """同一时间只允许一套完整验收在跑。

    实测教训：两套同时跑会互相踩。① 覆盖率日志是共享的，先跑的那套会把它
    清空，于是「证据」串味；② Unity 样本游戏带**单实例锁**，第二个实例
    直接退出 —— 另一边看到的现象是「游戏启动后 60 秒一直没就绪、
    会话已失效」，一个纯粹的假失败。
    """
    try:
        with open(LOCK, encoding="utf-8") as fh:
            old = int((fh.read() or "0").strip() or 0)
    except (OSError, ValueError):
        old = 0
    if old and old != os.getpid() and pid_alive(old):
        print(f"❌ 已有一套验收在跑（pid={old}）："
              f"{os.path.relpath(LOCK, ROOT)} 是它的锁。")
        print("   等它跑完再跑 —— 两套同时跑会互相踩：覆盖率日志串味，"
              "样本游戏又带单实例锁，会造出「游戏没就绪」这类假失败。")
        return False
    try:
        os.makedirs(os.path.dirname(LOCK), exist_ok=True)
        with open(LOCK, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except OSError as exc:
        print(f"⚠️  写不了锁文件（{exc}），本次不加锁继续跑")
    return True


def release_lock() -> None:
    try:
        with open(LOCK, encoding="utf-8") as fh:
            if (fh.read() or "").strip() == str(os.getpid()):
                os.remove(LOCK)
    except OSError:
        pass


def child_env() -> dict:
    """给子进程带上动态覆盖率垫片。

    必须由本脚本统一带上（而不是让使用者自己 export）：
    否则覆盖率门禁会读到**上一次**留下的陈旧日志，
    出现「脚本明明没跑到某工具，门禁却通过」的假通过 ——
    这正是本项目最忌讳的那类错误。
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(HERE, "_cov")
    env["GAMEMOD_COV_OUT"] = COV_LOG
    return env


def run(script: str, timeout: float = 1200.0) -> tuple[int, str]:
    """跑一个验收脚本：输出走**临时文件**，并带超时。

    为什么不能用 `capture_output=True`（实测踩到）：管道要等到 **EOF** 才算读完，
    而验收脚本会 spawn 出游戏进程；只要有进程继承了那两根管道还继续活着，
    `subprocess.run` 就**永远**等不到 EOF。
    现象很好认：`run_all_verify.py` 只用了 0.1 秒 CPU、一个子进程都没有、
    整轮卡在 `verify_mcp_tools` 上不动，而那个游戏进程在另一头烧 CPU。
    实测确认：`Stop-Process` 掉那个游戏，父进程**立刻**继续往下跑。

    改成写临时文件后，`subprocess.run` 只等**子进程自己退出**，
    谁还攥着文件句柄都不影响它 —— 这类挂死从根上消失。
    超时仍然保留：脚本自己死循环时，要报红（`TIMEOUT_CODE`），不能把整套挂住。
    """
    with tempfile.TemporaryFile(mode="w+b") as fh:
        try:
            proc = subprocess.run(
                [sys.executable, os.path.join(HERE, script)],
                stdout=fh, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, cwd=ROOT, env=child_env(),
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            fh.seek(0)
            tail = fh.read().decode("utf-8", "replace").strip()[-2000:]
            return (TIMEOUT_CODE,
                    f"[超时 {timeout:.0f}s 被终止] 输出尾部：\n{tail}")
        fh.seek(0)
        return proc.returncode, fh.read().decode("utf-8", "replace")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-cov", action="store_true",
                    help="不跑动态覆盖率门禁")
    ap.add_argument("--retries", type=int, default=1,
                    help="崩溃时最多重试次数（默认 1）")
    ap.add_argument("--only", action="append", default=None,
                    metavar="脚本名",
                    help="只跑指定的验收脚本（可重复；可写 verify_x 或 "
                         "verify_x.py）。用于排查单个脚本，"
                         "也让 verify_encoding_console.py 能廉价地"
                         "验证本脚本的汇总打印路径。")
    args = ap.parse_args()

    scripts = sorted(
        f for f in os.listdir(HERE)
        if f.startswith("verify_") and f.endswith(".py")
        # 这个脚本是静态清单检查，跑它很快但不算「验收工具」；
        # 仍要跑，只是注意它不是动态证据。
        and f != "verify_tool_coverage.py"
    )

    if args.only:
        wanted = {
            (w if w.endswith(".py") else w + ".py") for w in args.only
        }
        unknown = sorted(w for w in wanted if w not in scripts)
        if unknown:
            print(f"⚠️  --only 指定的脚本不存在或不可跑：{unknown}")
            print(f"     可用的有：{scripts}")
            return 2
        scripts = [s for s in scripts if s in wanted]

    # 加锁只在「跑整套」时做：`--only` 会被 verify_encoding_console.py
    # 嵌套调用，外层已经拿着锁，内层再抢一次会自己把自己挡住。
    locked = False
    if not args.only:
        if not acquire_lock():
            return 2
        locked = True

    print("=" * 74)
    print(f"验收脚本总览：{len(scripts)} 个")
    print("=" * 74)

    # 先清掉旧日志：覆盖率必须是**本次**跑出来的证据，
    # 不能与上一轮的结果混在一起。
    #
    # `--only` 时**绝不能清**：本脚本会被 verify_encoding_console.py
    # 以 `--only` 嵌套调用（用来验证汇总打印路径），若嵌套的这次把日志删了，
    # 就会把外层整套跑到一半、已经积累的覆盖率证据抹掉 ——
    # 外层门禁随即报「工具没被调用过」的假失败。
    if not args.no_cov and not args.only:
        try:
            os.remove(COV_LOG)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"⚠️  旧覆盖率日志删不掉（{exc}），结果可能混入上一次的数据")

    failed: list[str] = []
    crashed: list[str] = []
    timedout: list[str] = []
    retried: list[str] = []

    for name in scripts:
        stem = name[:-3]
        code, out = run(name)

        # 崩溃 / 超时：重试。断言失败（正常非零退出）不重试 —— 那是确定的结论。
        # 自注入 Frida 的脚本（SELF_ATTACH）本身就是崩溃高发户，
        # 给它们**多一次**预算，但仍要留痕（重试过就记进 retried）。
        budget = args.retries + (1 if stem in SELF_ATTACH else 0)
        attempts = 0
        while (code in CRASH_CODES or code == TIMEOUT_CODE) and attempts < budget:
            attempts += 1
            what = "超时" if code == TIMEOUT_CODE else "崩溃"
            print(f"[{what}] {stem} exit={code}"
                  f"（{out.strip().splitlines()[-1][:60] if out.strip() else '无输出'}）"
                  f" —— 第 {attempts} 次重试…")
            time.sleep(1.0)
            code, out = run(name)

        if code == 0:
            mark = "✅"
            if attempts:
                retried.append(stem)
                mark = "✅(重试)"
            print(f"[{mark}] {stem}")
        elif code == TIMEOUT_CODE:
            timedout.append(stem)
            print(f"[⏱] {stem} —— **超时被终止**（脚本没结束，不是断言失败）")
            for line in out.strip().splitlines()[-6:]:
                print(f"       {line}")
        elif code in CRASH_CODES:
            crashed.append(stem)
            print(f"[💥] {stem} exit={code} —— 进程崩溃（非断言失败）")
            for line in out.strip().splitlines()[-4:]:
                print(f"       {line}")
        else:
            failed.append(stem)
            print(f"[❌] {stem} exit={code}")
            for line in out.strip().splitlines()[-10:]:
                print(f"       {line}")

    print()
    print("=" * 74)
    print(f"通过 {len(scripts) - len(failed) - len(crashed) - len(timedout)}"
          f" / {len(scripts)}"
          f"    断言失败 {len(failed)}    进程崩溃 {len(crashed)}"
          f"    超时 {len(timedout)}")
    if retried:
        print(f"重试后通过（环境抖动）：{', '.join(retried)}")
        print("  ⚠️  出现重试说明本机存在不稳定因素，结论比一次跑通要弱。")
    if failed:
        print(f"失败：{', '.join(failed)}")
    if crashed:
        print(f"崩溃：{', '.join(crashed)}")
        print("  崩溃多为 Frida 自注入本进程导致，非产品缺陷；")
        print("  但**不算通过**，需在有显示环境下复核（与管理员权限无关）。")
    if timedout:
        print(f"超时：{', '.join(timedout)}")
        print("  超时说明脚本**自己没结束**（死循环，或卡在某个调用上）；"
              "输出走的是临时文件，所以这不可能是「等管道 EOF」造成的。")
    print("=" * 74)

    rc = 1 if (failed or crashed or timedout) else 0

    if not args.no_cov:
        # --only 时**必须**跳过门禁：只跑一两个脚本，不可能覆盖全部工具，
        # 跑门禁只会得到一个恒假的「未覆盖」结论。这不是放水 ——
        # 覆盖率门禁的意义是「整套跑完后的证据」，只跑子集时它没有意义。
        if args.only:
            print()
            print("已用 --only 指定子集，跳过动态覆盖率门禁"
                  "（子集不可能覆盖全部工具，跑了也没有意义）。")
        else:
            print()
            print("动态覆盖率门禁（devtools/report_tool_coverage.py --strict）")
            print("-" * 74)
            cov = subprocess.run(
                [sys.executable, os.path.join(HERE, "report_tool_coverage.py"),
                 "--strict"],
                cwd=ROOT, text=True, encoding="utf-8", errors="replace",
            )
            if cov.returncode != 0:
                print("覆盖率门禁未通过：有工具没被真正调用过。")
                rc = 1
            else:
                print("覆盖率门禁通过。")
            print("  注：覆盖率日志由本脚本统一设置（PYTHONPATH=devtools/_cov），")
            print("     已在开跑前清空，因此是本次运行的证据。")

    if locked:
        release_lock()
    return rc


if __name__ == "__main__":
    sys.exit(main())
