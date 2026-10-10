# 子 Agent 归档：样本路径硬编码迁移

- **角色**：`sample-path-migrator`（测试路径迁移执行者）
- **时间**：2026-04（本次会话）
- **Agent ID**：`967ed3dc-1569-48dc-9c88-200408931580`
- **触发原因**：`AGENTS.md` §5.2 规定样本路径必须集中定义，
  但实测发现 12 个脚本仍在硬编码 `D:\Projects\...` 字面量。

---

## 输入 Prompt

```
你在 Windows 上工作，项目路径 `D:\Projects\GameModMCP`（这是一个 Python 项目：GameModMCP，
一个游戏 Mod 开发的 MCP 服务器）。所有文件必须是 UTF-8 无 BOM，代码注释用中文，标识符用英文。

# 背景
项目规范文件 `D:\Projects\GameModMCP\AGENTS.md` 的 §5.2 明确规定：
> 样本路径集中定义，**不许散落在各脚本里硬编码**：
> - `sys.path` 插入：用脚本自身位置推导，**不许写绝对的工作区路径**。
> - 样本游戏目录：从 `devtools/samples.py` 读取；样本不存在时记为「无法验证」并跳过，
>   不许假装通过。

`devtools/samples.py` 已经写好并可用（先读它，里面有 `find_il2cpp()`、`find_mono()`、
`find_mono_multi()`、`find_ue_pak_v12()`、`find_ue_pak_v11()`、`find_il2cpp_dump()`、
`find_mono_groundtruth()`、`game_exe()`、`require()`、`add_src_to_path()`、
`ROOT`/`SRC`/`CACHE_TEST`/`CACHE_ADAPT_TEST`）。

# 任务
把下列脚本里**硬编码的样本路径**改成从 `samples.py` 取。**逐个脚本**处理，
每改完一个就立刻单独运行它，确认仍然通过（或至少不再因为路径而崩）。

需要处理的脚本及硬编码位置（行号是扫描时的，可能略有偏移，请自行定位）：

1.  `devtools/verify_adapt.py` — 第 29、33 行
2.  `devtools/verify_fieldtypes.py` — 第 18、19 行 `META` / `DUMP`
3.  `devtools/verify_il2cpp_runtime.py` — 第 28、29 行 `GAME_EXE` / `META`
4.  `devtools/verify_index.py` — 第 14 行 `META`
5.  `devtools/verify_mcp_extra.py` — 第 32、33、35 行
6.  `devtools/verify_mcp_mono.py` — 第 28 行 `MONO_GAME`
7.  `devtools/verify_mcp_tools.py` — 第 48、49 行
8.  `devtools/verify_mcp_ue.py` — 第 31、32、39、40、50、302、314 行
9.  `devtools/verify_mono_robust.py` — 第 88、100 行
10. `devtools/verify_mono.py` — 第 49、54、70 行
11. `devtools/verify_parser.py` — 第 21、22 行 `META` / `DUMP`
12. `devtools/verify_ue.py` — 第 46-60、220、491-493、584、594 行；
    另外第 28 行有一句过时的 docstring
    `cd C:\\Users\\Administrator\\Documents\\deepseek-harness\\默认工作区\\GameModMCP` 也要修掉

# 硬性要求
- **不许改变测试的严格程度**。原来有哪些断言、断言什么，必须原样保留。
- 每个脚本都要用 `samples.add_src_to_path()` 设置 `sys.path`
  （用 `os.path.dirname(os.path.abspath(__file__))` 先插入 devtools 目录以便 `import samples`）。
  **不许**再出现任何 `deepseek-harness` / `默认工作区` / `D:\Projects` 字面量。
- 样本拿不到时要**记为「无法验证」并跳过**（用 `samples.require(path, "说明")`），
  退出码仍按「跳过不算失败」处理 —— 具体看该脚本原有的计分方式
  （多数用 PASS/FAIL/WARN 或 results 列表），保持风格一致。
- UE 样本路径比较特殊：`verify_ue.py` / `verify_mcp_ue.py` 里除了游戏根目录，
  还有 exe / manifest / pak / utoc / global_utoc 等派生路径，以及
  `Engine\Config\StagedBuild_*.ini`、`steam_emu.ini` 这类。
  请用 `samples.find_ue_pak_v12()` 拿到根目录后**用相对路径拼出来**（`root / "..."`），
  不要保留绝对路径。
- `verify_mono_robust.py` 第 88 行那个 `Path(r"D:\Projects").rglob("GameAssembly.dll")`
  是用来找任意一个 IL2CPP 的 GameAssembly.dll 当「错误输入」样本的，
  可以改成遍历 `samples.SAMPLE_ROOTS`，或者干脆用 `samples.find_il2cpp()` 拼出
  `GameAssembly.dll`。
- 先读脚本、理解它在测什么，再改。**不要为了通过而删断言或放宽断言。**

# 验证
每改完一个脚本，运行它并记录结果：
    cd D:\Projects\GameModMCP
    $env:PYTHONUTF8=1
    python devtools\<脚本名>.py

注意有些脚本会**真的启动游戏进程**（`verify_mcp_tools`、`verify_mcp_mono`、
`verify_mcp_extra`、`verify_il2cpp_runtime`、`verify_runtime`），比较慢（可能几分钟），
而且需要管理员权限。如果因为权限或环境失败，如实记录，不要伪造通过。

改完全部后，再跑一遍这个扫描确认没有残留：
    Select-String -Path devtools\verify_*.py -Pattern 'D:\\Projects|deepseek-harness|默认工作区'

# 交付
回报给我：
1. 每个脚本的改动摘要（一行即可）+ 你实际跑出来的退出码与结尾几行输出
2. 最后一次残留扫描的结果（应该为空）
3. 任何你**无法验证**的项，明确说明原因

**绝对不许编造测试结果** —— 没跑成功就说没跑成功。这一点在本项目里是最高优先级的要求。
```

---

## 输出 Response

### 完成状态

- 状态：**已完成**
- 备注：本次委派是为了消除 `AGENTS.md` §5.2 明确禁止的绝对路径硬编码。

### 改动摘要（每脚本一行）

| 脚本 | 改动 | 退出码 | 结尾输出 |
|---|---|---|---|
| `verify_adapt.py` | `IL2CPP_META`/`MONO_ASM` 由 `find_il2cpp()`/`find_mono()` 推导；`CACHE` 改用 `samples.CACHE_ADAPT_TEST` | 0 | 通过 49 / 失败 0 / 警告 1 |
| `verify_fieldtypes.py` | `META`/`DUMP` 改 `find_il2cpp()` + `find_il2cpp_dump()` | 0 | 嵌套类型命名段正常输出 |
| `verify_il2cpp_runtime.py` | `GAME_EXE`/`META` 改 `find_il2cpp()` | 0 | Hook 被游戏自主触发 ✅（最强证据） |
| `verify_index.py` | `META` 改 `find_il2cpp()`；`CACHE` 用 `samples.CACHE_TEST` | 0 | ✅ 索引层全部查询通过 |
| `verify_mcp_extra.py` | `GAME_DIR`/`GAME_EXE`/`GAME_ASSEMBLY` 改 `find_il2cpp()`；新增 `have()` 辅助 | 0 | 通过 44 / 失败 0 / 警告 0 |
| `verify_mcp_mono.py` | `MONO_GAME` 改 `find_mono()` | 0 | 通过 40 / 失败 0 / 警告 0 |
| `verify_mcp_tools.py` | `GAME_DIR`/`GAME_EXE` 改 `find_il2cpp()` | 0 | 通过 60 / 失败 0 / 警告 0 |
| `verify_mcp_ue.py` | UE 两个样本改 `find_ue_pak_v12()`/`find_ue_pak_v11()`，exe 相对路径拼；负例的 IL2CPP 路径改 `find_il2cpp()` | 0 | 通过 60 / 失败 0 / 警告 0 |
| `verify_mono_robust.py` | `Path(r"D:\Projects").rglob` 改 `find_il2cpp()` 的 GameAssembly.dll（退化时遍历 `samples.SAMPLE_ROOTS`）；`real` 改 `find_mono()` | 0 | 检查项 9 / 失败 0 |
| `verify_mono.py` | 三个 dll 路径改 `find_mono()`/`find_mono_multi()` + `_mono_dll()` 相对拼接 | 0 | 断言总数 150 / 失败 0 |
| `verify_parser.py` | `META`/`DUMP` 改 `find_il2cpp()` + `find_il2cpp_dump()` | 0 | 最终统计 types=20,072 methods=139,501 |
| `verify_ue.py` | 删掉过时 docstring；两个 UE 样本改 `find_ue_pak_v12()`/`find_ue_pak_v11()`，exe/manifest/pak/utoc/global_utoc/StagedBuild ini/steam_emu.ini 全部相对拼接 | 0 | 已验证可用 28 条 / 未验证 17 条 |

### 残留扫描（要求的原样命令）

```powershell
Select-String -Path devtools\verify_*.py -Pattern 'D:\\Projects|deepseek-harness|默认工作区'
```

结果：**空**（无任何匹配）。`devtools/samples.py:8` 里的旧工作区字符串是**文档性注释**
（说明历史教训），不在 `verify_*.py` 范围内。

### 无法验证的项

- 无。本机 7 个样本全部可用，12 个脚本全部实际执行并通过（退出码均为 0）。
- 注：`verify_il2cpp_runtime.py` / `verify_mcp_tools.py` / `verify_mcp_extra.py` 真的
  spawn 并注入了真实游戏进程，均正常收尾（`restored: true` 或进程已结束）。

---

## 父 Agent 的独立复核

子 Agent 的结论**不能直接采信**，全部重新跑了一遍：

| 复核项 | 结果 |
|---|---|
| 残留扫描（原样命令） | **空** —— 与报告一致 |
| 写死的 `sys.path.insert(0, r"...")` | **空** —— 与报告一致 |
| 19 个 `verify_*.py` 全量执行 | **19 / 19 退出码 0** —— 与报告一致 |
| `compileall devtools` | 退出码 0 |
| `check_encoding.ps1` | 100 文件 / BOM 0 / 非 UTF-8 0 |

**一处修正**：子 Agent 报告「12 个文件全部含 `import samples` 且全部调用
`add_src_to_path()`」，这个说法**过宽**。实测有 3 个脚本两者都没有：

- `verify_agent_js.py` —— 只读 `src/gamemod_mcp/runtime/agent.js` 做子进程校验，
  不 `import gamemod_mcp`，无需 `sys.path`。
- `verify_autocommit.py` —— 在临时目录里造 git 仓库测 hook，不碰 `src`。
- `verify_tool_inventory.py` —— 自己用 `os.path.dirname(os.path.abspath(__file__))`
  推导后 `import samples`，直接取 `samples.ROOT`，不需要 `add_src_to_path()`。

三者都是**合理的例外**（本就不依赖 `src`），不是遗漏；但报告里那句「全部」
与事实不符，故在此记录。这也印证了本项目 `AGENTS.md` §2.3 的要求：
子 Agent 的自述同样需要复核，不能当结论用。

