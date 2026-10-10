"""验收：`server.py` / `session.py` 里的**纯函数助手**（P2-5 剩余项）。

这几处原来都有「看起来对、实际是错的」行为，而且都不需要游戏就能验证：

  1. `_split_argv()` —— `launch_game(args=...)` 原来用 `args.split()`，
     含空格的路径会被拆成多个参数。改的时候差点照抄「应该用 `shlex.split`」
     这条建议，实测发现**更糟**：

         shlex.split(r'--path C:\\Games\\My Game\\a.exe --x')
         posix=True （默认）-> ['--path', 'C:GamesMy', 'Gamea.exe', '--x']
         posix=False        -> ['--path', 'C:\\\\Games\\\\My', 'Game\\\\a.exe', '--x']

     默认 posix 模式把反斜杠当转义符，而 Windows 路径全是反斜杠 ——
     照那条建议改会把 `C:\\Games\\Game\\a.exe` 变成 `C:GamesGamea.exe`。
     所以自己按 Windows 习惯拆，本脚本把这几条都钉住。

  2. `_field_read_width()` —— `read_field_value` 原来**固定读 8 字节**，
     4 字节字段会连带读到相邻字段的字节：既给出无意义的 `as_int64`
     （两个字段拼出来的数），也会在字段贴近页尾时把一次本可成功的读取
     变成假失败。

  3. `_parse_arg_specs()` / `_parse_retval_type()` —— Hook 的规格名原来
     照单全收，写错的 `flaot` 会静默按指针读一个整型寄存器，给出「看着
     有值、其实是错值」的结果。契约第 3 条要求收下的参数真的影响行为，
     所以写错必须报错。

  4. `_retval_spec_for()` —— 返回值读法不能一律按指针：float / double 在
     XMM0 而不在 RAX，按 ptr 读会给出毫无意义的整数（实测 `Mathf.Abs(-2.5f)`
     得到 `0x20da0a48360`）。

  5. `_overlapping_hook()` —— `write_memory` 的护栏：写已被 Hook 的函数入口
     （哪怕写回刚读到的同样字节）实测会让游戏数秒内崩在 `0xc0000005`。

这些都是纯函数，所以本脚本**不需要任何样本**，永远会跑。
真正端到端的那部分（拿真实字段偏移写标记值再读回来、捕获参数与返回值）
在 `verify_mcp_extra.py` / `verify_mcp_tools.py` 里，那边要一个活着的游戏。
"""

from __future__ import annotations

import os
import sys

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
    print("=" * 74)
    print(title)
    print("=" * 74)


#: (输入, 期望)。反斜杠在源码里写成 `\\`，即运行期就是一个反斜杠。
ARGV_CASES: list[tuple[str, list[str]]] = [
    ("", []),
    ("   ", []),
    ("--x", ["--x"]),
    ("-a b", ["-a", "b"]),
    ("  -a   -b  ", ["-a", "-b"]),                       # 连续空白只算一个分隔
    ("-a\t-b", ["-a", "-b"]),                            # tab 也是空白
    ("-n 3", ["-n", "3"]),
    # 不带引号的含空格路径 —— Windows 上也必须是两个 token（这是正确行为，
    # 不是 bug：命令行里含空格的路径必须加引号）
    (r"C:\Games\My Game\a.exe", [r"C:\Games\My", r"Game\a.exe"]),
    # 带引号：一个 token，且**反斜杠一个都不能少**
    (r'-path "C:\Games\My Game\a.exe" -v',
     ["-path", r"C:\Games\My Game\a.exe", "-v"]),
    # 引号只包住一段
    (r'--name="a b"', ["--name=a b"]),
    # 不带引号、不含空格的反斜杠路径：一个 token，反斜杠原样保留
    # （最容易被 shlex 吃掉的情形）
    (r"C:\Games\Garden\GameAssembly.dll",
     [r"C:\Games\Garden\GameAssembly.dll"]),
    (r'"C:\Games\My Game\GameAssembly.dll"',
     [r"C:\Games\My Game\GameAssembly.dll"]),
    # 未闭合引号：一直引到串尾（不报错）
    ('"unterminated', ["unterminated"]),
    ('--a "b', ["--a", "b"]),
    # 空引号不产生空 token（已在 docstring 里写明）
    ('--empty ""', ["--empty"]),
]

#: (类型名, 期望宽度)
WIDTH_CASES: list[tuple[str, int]] = [
    # 1 字节
    ("System.Boolean", 1), ("bool", 1), ("System.Byte", 1),
    ("byte", 1), ("System.SByte", 1), ("sbyte", 1),
    # 2 字节
    ("System.Char", 2), ("char", 2), ("System.Int16", 2), ("short", 2),
    ("System.UInt16", 2), ("ushort", 2),
    # 4 字节
    ("System.Int32", 4), ("int", 4), ("System.UInt32", 4), ("uint", 4),
    ("System.Single", 4), ("float", 4),
    # 8 字节
    ("System.Int64", 8), ("long", 8), ("System.UInt64", 8), ("ulong", 8),
    ("System.Double", 8), ("double", 8),
    ("System.IntPtr", 8), ("System.UIntPtr", 8),
    # 引用类型 = 指针 = 8 字节（这是原来固定 8 的行为，不能改坏）
    ("System.String", 8), ("UnityEngine.TextAsset", 8),
    ("System.Threading.CancellationTokenSource", 8),
    # 查不到 / 空 / None → 8（保守，等同原行为）
    ("", 8), ("MyNamespace.MyUnknownStruct", 8),
    ("  System.Int32  ", 4),                             # 前后空白要容忍
]


def main() -> int:
    section("1. `_split_argv()` —— launch_game 的命令行拆分")

    bad: list[str] = []
    for text, want in ARGV_CASES:
        got = S._split_argv(text)
        if got != want:
            bad.append(f"{text!r} -> {got!r}（期望 {want!r}）")
    check(f"{len(ARGV_CASES)} 条拆分用例全部正确", not bad,
          f"不符 {len(bad)} 条：{bad[:3]}" if bad else "含引号/反斜杠/tab/未闭合")

    # 把「这条修的是什么」变成可判定的断言，而不只是几条用例：
    # 原来 args.split() 的行为必须**为假**，否则这条修复等于没做。
    spaced = r'"C:\Games\My Game\a.exe"'
    old_behaviour = spaced.replace('"', "").split()
    new_behaviour = S._split_argv(spaced)
    check("含空格路径不再被拆成多个参数",
          len(new_behaviour) == 1 and len(old_behaviour) > 1,
          f"旧 {old_behaviour} -> 新 {new_behaviour}")

    # 反斜杠必须原样保留 —— 这正是 `shlex.split` 默认模式会破坏的地方
    preserve = r'"C:\Games\My Game\a.exe"'
    got = S._split_argv(preserve)
    check("反斜杠不当作转义符（`shlex.split` 默认模式会吃掉它）",
          got == [r"C:\Games\My Game\a.exe"],
          f"得到 {got!r}")

    # 顺带把「为什么不直接用 shlex」也钉住：一旦有人换成 shlex.split，
    # 本条会立刻报红，比留一句注释可靠。
    import shlex
    shlex_posix = shlex.split(r'--path C:\Games\My Game\a.exe')
    ours = S._split_argv(r'--path "C:\Games\My Game\a.exe"')
    check("`shlex.split` 默认模式确实会破坏 Windows 路径（换掉它的理由成立）",
          shlex_posix != ours and r"C:\Games" not in " ".join(shlex_posix),
          f"shlex={shlex_posix} 我们={ours}")

    section("2. `_field_read_width()` —— 按字段类型决定读几个字节")

    badw: list[str] = []
    for type_name, want in WIDTH_CASES:
        got_w = S._field_read_width(type_name)
        if got_w != want:
            badw.append(f"{type_name!r} -> {got_w}（期望 {want}）")
    check(f"{len(WIDTH_CASES)} 条宽度用例全部正确", not badw,
          f"不符 {len(badw)} 条：{badw[:3]}" if badw else
          "bool/int/float/long/引用类型/未知")
    check("None（拿不到类型名）按 8 字节",
          S._field_read_width(None) == 8, "保守，等同原行为")

    # 非空转：这个函数不能是个常量。原来「固定读 8」的写法会让下面这条为假。
    widths = {S._field_read_width(t) for t in
              ("System.Boolean", "System.Int16", "System.Int32",
               "System.Int64", "UnityEngine.TextAsset")}
    check("宽度真的随类型变化（不是一个恒为 8 的常量）",
          widths == {1, 2, 4, 8}, f"取到的宽度集合 = {sorted(widths)}")
    check("4 字节字段不会读成 8 字节（这就是原来的 bug）",
          S._field_read_width("System.Int32") == 4,
          "原来固定读 8，会连带读到相邻字段的 4 个字节")

    section("3. Hook 参数规格校验 —— 收下的参数必须真的影响行为")
    # 契约第 3 条：`args_type` 以前照单全收，写错的规格名（例如 `flaot`）
    # 会落到探针的 `default: raw.toString()` —— 于是**悄悄**按指针读一个
    # 整型寄存器，调用方以为拿到的是 float，实际是个地址字符串。
    # 这类「看起来有值、其实是错值」比直接报错危险得多。
    for good in ("int", "uint", "long", "ulong", "bool", "float", "double",
                 "ptr", "string", "cstring"):
        got_g = S._parse_arg_specs(good)
        if got_g != [good]:
            check(f"合法规格 {good} 被接受", False, f"得到 {got_g}")
            break
    else:
        check("10 个合法规格名全部被接受", True, "int/uint/long/ulong/bool/"
              "float/double/ptr/string/cstring")
    check("逗号分隔 + 前后空白被容忍",
          S._parse_arg_specs(" ptr , int ") == ["ptr", "int"],
          str(S._parse_arg_specs(" ptr , int ")))
    check("空串 → 空列表（不解析参数）", S._parse_arg_specs("") == [])
    for bad_s in ("flaot", "intt", "Int", "float32", "ptr,int,flaot"):
        try:
            got_b = S._parse_arg_specs(bad_s)
            check(f"非法规格 {bad_s!r} 必须报错", False, f"却返回 {got_b}")
        except ValueError as exc:
            check(f"非法规格 {bad_s!r} 报错并列出可用值",
                  bad_s.split(",")[-1] in str(exc), str(exc)[:60])
    check("retval_type 留空 = 按静态返回类型自动推断",
          S._parse_retval_type("") == "")
    try:
        S._parse_retval_type("flaot")
        check("非法 retval_type 必须报错", False)
    except ValueError as exc:
        check("非法 retval_type 报错", "flaot" in str(exc), str(exc)[:50])

    section("4. `_retval_spec_for()` —— 返回值读法不能一律按指针")
    # float / double 的返回值在 XMM0，不在 RAX：按 ptr 读会给出一个
    # 看着像地址、其实毫无意义的整数（实测 Mathf.Abs(-2.5f) 得到
    # 0x20da0a48360）。所以这个映射必须自动做对。
    from gamemod_mcp.runtime import session as _sess          # noqa: E402
    RETVAL_CASES = [
        ("System.Single", "float"), ("System.Double", "double"),
        ("System.Boolean", "bool"), ("System.Int32", "int"),
        ("System.UInt32", "uint"), ("System.Int64", "long"),
        ("System.UInt64", "ulong"), ("System.String", "string"),
        # 认不出来的一律 ptr：原样给出寄存器值，不含糊
        ("UnityEngine.Vector3", "ptr"), ("MyGame.SomeClass", "ptr"),
        ("", "ptr"), (None, "ptr"),
    ]
    badr: list[str] = []
    for tname, want in RETVAL_CASES:
        got_r = _sess._retval_spec_for(tname)
        if got_r != want:
            badr.append(f"{tname!r} -> {got_r}（期望 {want}）")
    check(f"{len(RETVAL_CASES)} 条返回类型映射全部正确", not badr,
          f"不符 {len(badr)} 条：{badr[:3]}" if badr else
          "浮点→float/double，整型按宽度，其余→ptr")
    check("浮点返回类型不会退化成 ptr（这就是那个 bug）",
          _sess._retval_spec_for("System.Single") != "ptr",
          "按 ptr 读 RAX 会给出无意义的整数")
    check("映射真的随类型变化（不是恒为 ptr 的常量）",
          len({_sess._retval_spec_for(t) for t, _ in RETVAL_CASES}) >= 5,
          str(sorted({_sess._retval_spec_for(t) for t, _ in RETVAL_CASES})))

    section("5. `_overlapping_hook()` —— write_memory 的护栏")
    # 实测：写已被 Hook 的函数入口（哪怕写回刚读到的同样字节）会让游戏
    # 在数秒内崩在 0xc0000005；同一操作写在模块头（base）上则完全无害。
    # 这个纯函数决定「要不要拦」，所以在这里钉住它。
    class _StubRT:
        def __init__(self, hooks):
            self._hooks = hooks

        def hook_status(self):
            return {"ok": True, "hooks": self._hooks}

    class _StubState:
        def __init__(self, hooks, boom=False):
            self.runtime = _StubRT(hooks)
            self._boom = boom

    hooks = [{"hook_id": "h1", "label": "verify.frameCount",
              "target": "0x7ffa2cc4b170"}]
    st_h = _StubState(hooks)
    check("写入被 Hook 的入口 → 命中（必须拦）",
          (S._overlapping_hook(st_h, "0x7ffa2cc4b170", 16) or {}).get("hook_id") == "h1",
          "首地址相同")
    check("写入入口 +4 字节处 → 仍命中（跳转指令有多字节）",
          S._overlapping_hook(st_h, "0x7ffa2cc4b174", 4) is not None,
          f"余量 {S._HOOK_ENTRY_SLACK} 字节")
    check("写入入口 +32 字节处 → 不命中（不是整段都拦）",
          S._overlapping_hook(st_h, "0x7ffa2cc4b190", 4) is None)
    check("写入模块头（base）→ 不命中（实测这样写无害）",
          S._overlapping_hook(st_h, "0x7ffa29f70000", 16) is None)
    check("写入跨越入口的一段（起于入口前 8 字节、长 32）→ 命中",
          S._overlapping_hook(st_h, "0x7ffa2cc4b168", 32) is not None,
          "重叠判定要按区间，不是只看起点")
    check("没有 Hook 时一律放行",
          S._overlapping_hook(_StubState([]), "0x7ffa2cc4b170", 16) is None)
    check("地址解析不了时放行（护栏只拦已知会崩的写法）",
          S._overlapping_hook(st_h, "不是地址", 16) is None)
    check("size<=0 时放行", S._overlapping_hook(st_h, "0x7ffa2cc4b170", 0) is None)

    class _BoomRT:
        def hook_status(self):
            raise RuntimeError("探针不通（模拟）")

    class _BoomState:
        runtime = _BoomRT()

    check("hook_status 抛异常时放行（不把写入变成不可用）",
          S._overlapping_hook(_BoomState(), "0x7ffa2cc4b170", 16) is None)

    print()
    print("=" * 74)
    if FAIL:
        print(f"\u274c 通过 {PASS} 项，失败 {FAIL} 项（警告 {WARN}）")
    else:
        print(f"\u2705 通过 {PASS} 项，失败 0 项"
              + (f"（警告 {WARN}）" if WARN else ""))
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
