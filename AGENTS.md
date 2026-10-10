# AGENTS.md —— GameModMCP 项目规范

本文件是**本项目的唯一权威约定**。任何 Agent（人或 AI）在本仓库工作时都必须遵守。
与全局 `~/.dsh/AGENTS.md` 冲突时，以本文件为准（本文件更具体）。

---

## 1. 项目定位

GameMod MCP 是一个 **stdio MCP 服务器**，把「游戏 Mod 开发」能力暴露成标准 MCP 工具，
供通用 Agent（Claude Code / Codex / Cursor / 自研 Agent）调用。

它同时以 **DSH 组合包**（bundle）的形式分发：`package.json` 声明
`dsh.bundle.patch`，`cordis.patch.yml` 插入「工具链 + MCP 客户端」两行，
`index.js` 是 Host 插件。在 DSH 里安装本包即完成接入，
`mcp__gamemod__*` 工具直接可用 —— **不需要手工建 venv、不需要改配置**。

支持三类引擎，方法论统一为**两阶段**：

```
静态阶段：解析元数据 → SQLite 索引        → 拿到「有哪些类/字段/方法/token」
运行时阶段：Frida 注入 → 真实偏移/地址     → 拿到「字段在哪个偏移、方法在哪个地址」
对账：静态 token == 运行时 token           → 这是「落点正确」的唯一证据
```

**核心断言（不许说反）**：

| 信息 | 静态元数据 | 运行时探针 |
|---|---|---|
| 类 / 字段 / 方法的**存在与签名** | ✅ 有 | ✅ 有 |
| 字段的**字节偏移** | ❌ **没有** | ✅ 有（`runtime_class_info`） |
| 方法的 **RVA / 绝对地址** | ❌ **没有**（只有 token） | ✅ 有（`find_hook_target`） |

> 历史教训：早期文档与 `instructions` 曾写「元数据给出字段偏移与方法 RVA」，
> 这与 `engine/adapt.py` 的 `method_address_in_metadata: False` 直接矛盾，
> 会误导 Agent 在没注入时就去算地址。**任何文档都不许再出现这种说法。**

---

## 2. 硬性规范

### 2.1 编码

- 所有文本文件必须 **UTF-8 无 BOM**。
- 写完文件后运行校验：`pwsh -ExecutionPolicy Bypass -File .\devtools\check_encoding.ps1`
- 该脚本会遍历交付目录，报出带 BOM 或非 UTF-8 的文件。

### 2.2 语言

- 与人交互、代码注释、文档正文：**中文**。
- 标识符（变量 / 函数 / 类 / 文件）：**英文**。
- 提交信息：中文。

### 2.3 不许编造

- 不确定就**明确说不确定**并给出如何验证，绝不猜一个看起来合理的值。
- 工具的返回值不许出现「假成功」：拿不到就 `ok: false` + `error`。
- 文档里的数字（工具数、条目数、命中数）必须是**跑出来的**，不是估计的。

### 2.4 依赖

- 运行期只依赖 `mcp` + `frida`（见 `requirements.txt`）。
- 引擎解析器（PE / IL2CPP / Mono / UE）**全部手写，只用标准库**。
  新增第三方依赖必须先说明理由并获得确认。

---

## 3. 架构与职责边界

```
src/gamemod_mcp/
├── server.py         MCP 工具层（@mcp.tool）。只做：参数校验 → 调下层 → 组装返回
├── engine/
│   ├── state.py      进程级单例会话（当前游戏 / 索引 / 运行时）
│   ├── discovery.py  进程发现与引擎识别（不再做全机 Steam 扫描）
│   ├── pe.py         PE 解析
│   ├── il2cpp_meta.py  global-metadata.dat 解析
│   ├── mono_meta.py    ECMA-335 程序集解析
│   ├── ue.py           Unreal 容器 / 清单 / 脚本对象提取
│   ├── adapt.py        跨引擎能力抽象（IL2CPP 与 Mono 的差异都收敛在这里）
│   └── index.py        SQLite 索引与查询
└── runtime/
    ├── session.py    Frida 会话封装（attach / spawn / hook / 事件）
    └── agent.js      注入到游戏进程内的探针（JS）
```

**分层规则**：

- `server.py` 不许写解析逻辑；逻辑放 `engine/` / `runtime/`，`server.py` 只编排。
- 引擎差异只允许出现在 `adapt.py`，不许散落到 `server.py` 或工具文档里。
- `agent.js` 里的能力必须有对应的 MCP 出口；没有出口的函数是死代码，应删除。

### 3.1 MCP 工具契约

1. **返回值**：永远是 dict，且**必定**含 `ok`（`True`/`False`）。失败带 `error`。
2. **日志**：一律走 stderr（`logutil`）。stdout 是 JSON-RPC 通道，写任何东西都会破坏协议。
3. **单一职责**：一个能力只暴露**一个**工具。
   若两个工具做同一件事（或一个是另一个的特例），**合并成一个**，
   用参数区分，而不是留给 Agent 去猜该用哪个。
4. **找不到就明说**：查询无结果返回 `total: 0` 并附说明，
   不许返回空列表让 Agent 误以为「不存在」。
5. **不返回超大结果**：任何可能上万条的返回都必须支持 `limit` / `offset` 或 `pattern` 过滤，
   并在 `has_more` 里如实标注是否被截断。
   此外**任何返回都不超过 256 KB**，这一条由 `src/gamemod_mcp/cap.py` +
   `@_cap_response` 在**所有**工具外层统一兜底（P2-4），不靠各工具自觉 ——
   实测 `limit` 上限 2000 时，`search_classes('', limit=2000)` 的线上文本是 856 KB。
   超限时截断**最大的那个顶层列表**、完整列表进句柄，并如实改写
   `has_more` / `returned`（明明还有却说没有，比超限更糟）。
   新增工具**不需要**自己处理这件事，但**不许**绕过它（例如在 `server.py`
   里直接向客户端推送大对象）。

---

## 4. 工具增删改：必须同步的**五**处

改动任何 MCP 工具（新增 / 删除 / 改名 / 改签名）时，以下位置**必须同一次改完**：

| # | 位置 | 内容 |
|---|---|---|
| 1 | `src/gamemod_mcp/server.py` | 工具实现 + 模块 docstring 的「工具分层」列表 + `FastMCP(instructions=...)` |
| 2 | `README.md` | 「工具一览」表格 |
| 3 | `skills/gamemod-mod/SKILL.md` | 工作流、配方、引擎差异里出现的该工具 |
| 4 | `devtools/verify_mcp_*.py` | 覆盖该工具的验收断言 |
| 5 | `devtools/verify_tool_annotations.py` | 在语义表里**表态它危不危险**（只读？破坏性？要活着的游戏进程？） |

第 5 条是 P2-5 加的：MCP 的 `destructiveHint` 默认是 **True**、
`openWorldHint` 默认也是 **True**，所以不表态就等于告诉客户端
「它会破坏东西、且与外部世界交互」。新增工具不在这张表里，
`verify_tool_annotations.py` 会直接报红。

**工具清单以代码为准，文档不许硬编码数量。**
用 `python devtools/verify_tool_inventory.py` 校验，该脚本覆盖 **9 处**：

| 校验对象 | 强度 |
|---|---|
| README「工具一览」表格 | **双向完全一致**（不多、不少） |
| README 正文的数量声明（「N 个工具」） | 数值必须等于实际 |
| `server.py` docstring 的「工具分层」列表 | 双向完全一致 |
| `FastMCP(instructions=...)` | 只许提到存在的工具 |
| `SKILL.md`（含**代码块里的调用形式**） | 只许提到存在的工具 |
| 已删除 / 已合并的工具名 | 在**上面四处**都已清理（逐个点名，见 `REMOVED_TOOLS`） |
| README「验证脚本」表 | 与 `devtools/verify_*.py` **双向完全一致** |
| **所有 `*.md`**（排除 `agent_logs/`）提到已删工具时 | 同一行必须能看出「它已经没了」；否则 Agent 会照着去调 |
| **组合包层**（`package.json` / `cordis.patch.yml` / `index.js`） | 见 §4.1；由 `verify_dsh_bundle.py` 校验（静态 + **行为**两层） |

「所有 `*.md` 提到已删工具必须标注」那一行是 P1-3b 那轮补的：它前面的七处只管
「README 表 / docstring / instructions / SKILL.md」，而
`docs/项目梳理与优化建议.md` 长期写着「全部工具清单（41 个）」、
§三 的工作流还让 Agent 去调**已删除**的 `detect_games()` —— 没有任何检查覆盖 `docs/`。
所以规则放宽到「提到可以，但必须同行标注」，并且扫描脚本自己要有
**非空转保证**（确认这些名字确实还在文档里被提到、确认扫到了文件），
否则「没有未标注的提及」可能只是因为扫了个空集合。

**组合包层是「另一种语言、另一套运行时」，Python 侧的脚本一行都碰不到它**：
60 多个 `verify_*.py` 全绿与「组合包根本装不上」可以同时成立。
所以 `devtools/verify_dsh_bundle.py` 单独覆盖它，并且必须做**行为**验收
（真的 `import` 并调 `apply()`），只读文本的话，`apply()` 里写错一个字段名
也照样「通过」。

> 早期版本靠「把 `skills/` 手工复制到 `~/.dsh/skills/`」来分发 skill，
> 而那份副本不在任何门禁范围内：实测曾落后 **139 行**，开头第一句还让 Agent
> 去调一个**已删除**的工具 —— 也就是说「仓库修好了、门禁全绿」与
> 「Agent 照着已删除的工具去调」可以同时成立。
> **现在改为随组合包注册**（`index.js` 里的 `ctx.inject(['skills'], …)`），
> Agent 看到的就是**这个版本包里的那一份**，不存在副本漂移。

**工具的语义标注**另由 `python devtools/verify_tool_annotations.py` 校验
（4 个语义位必须显式有值、分组双向一致、只读 ⇒ 不破坏不副作用）。

**「README 验证脚本表」那一行不属于「工具」，但属于同一类问题**：
**手工维护的清单必然漂移**，所以一并卡住
（README 曾写「10 个可复现的验证脚本」而实际已有 20 多个）。

不一致时返回非零退出码，是判断「文档是否实时更新」的唯一依据。

**工具的返回字段与参数不许「说了话但不干活」**（P2-5 行为批立的规矩）：

- 收下的参数必须真的影响行为。`session.scan_pattern(scope=...)` 曾把 `scope`
  放进 opts 而 `agent.js` 从不读它 —— 调用方会以为扫描范围被限制了。
  **没有任何读取方的参数就该删掉**，而不是留着当文档。
- 返回给客户端的字段不许**恒为空/恒定值**。`find_game_processes` 曾一直输出
  `"architectures": []`（上游的第二个返回值历来写死 `[]`），客户端会读成
  「没检测到任何架构标记」。**拿不到就明确给 `null` 并说明**，不要给一个
  看起来像测量结果的默认值（`is_64` 现在用 `None` 表示「没测出来」，
  而不是拿 `False` 冒充「32 位」）。
- 数值字段的**读取宽度必须按类型来**。`read_field_value` 曾固定读 8 字节，
  4 字节字段会把**相邻字段**的字节一起读进来 —— 既给出拼出来的错值
  （`as_int64`），也会在贴近页尾时把一次本可成功的读取变成假失败。
- 改这类行为时**必须同时给纯函数与端到端两层验收**，并做**非空转反证**
  （把代码改回旧写法，看断言是否真的报红）。见 `devtools/verify_server_helpers.py`
  与 `verify_mcp_extra.py` 的三档宽度测试。

> 历史教训：`README.md:159` 曾列出并不存在的 `unhook_all`；
> `server.py:19-30` 的 docstring 漏写了 13 个工具；
> README 写「41 个工具」而实际已变；
> README 同一节写「10 个验证脚本」而表格已有 13 行、实际 20 多个；
> SKILL.md 的配方里留着已删除的 `find_monobehaviours` 与 `detect_games`
> ——最后这个当时**没被任何检查发现**，因为它在 ```python 代码块里、不带反引号。
> **所以校验脚本必须扫代码块，不能只扫反引号。**

### 4.1 组合包层（分发形态）

本项目以 **DSH 组合包**分发。改动以下文件时，`devtools/verify_dsh_bundle.py`
必须同步（它同时做静态与**行为**两层验收）：

| 文件 | 作用 | 不许出现 |
|---|---|---|
| `package.json` | 声明 `dsh.bundle.patch`、`icon`、`exports` | 展示文案写在 `meta`（**没人读**，见下） |
| `cordis.patch.yml` | 插入「工具链 + MCP 客户端」两行 | **任何绝对路径**；顺序靠行序而非 `inject` |
| `index.js` | 准备托管 venv、发布服务、注册 skill | 缺 `mcp>=1.0.0,<2`；stdout 输出 |

三条硬规矩：

1. **不许写死绝对路径**（含盘符、用户名、`$DSH_HOME` 展开值）。
   解释器 / 包根 / `PYTHONPATH` 一律由 `index.js` 用 `import.meta.url`
   在运行时算出，否则包在别人机器上必然失效。
2. **顺序必须由 `inject` 表达**，不能靠 YAML 行序 —— Loader 是**并发**
   挂载各行的，行序不保证顺序。`mcp-client` 行 `inject: [gamemodToolchain]`，
   而 `!!js` 的 config 在「该行声明的注入已激活」之后才求值，
   两者叠加才保证「先装依赖、再拉服务器」。
3. **展示元数据放 `locale/*.json` 的 `meta`**，不是 `package.json.meta`。
   实测 `readPluginMeta` 读的是 `${specifier}/locale/<lang>.json` 的
   `meta.title/description`；放错地方**不报错**，只是永远不显示
   —— 正是「说了话但不干活」那一类。图标则在 `package.json.icon`。

> `index.js` 的 `mcp` 约束必须是 `mcp>=1.0.0,<2`，且与 `requirements.txt`、
> `pyproject.toml` **三处一致**：本代码用 1.x 的 `mcp.server.fastmcp.FastMCP`，
> 2.x 改名成 `MCPServer`。只写 `>=1.0.0` 会让干净环境装到 2.x 然后起不来
> —— 打包分发时实测到过（干净 venv → 2.3.0 → 服务器起不来）。

**验收脚本自己也不许污染被测物**：`verify_dsh_bundle.py` 的反证曾直接改写
`cordis.patch.yml` 再跑一次自己，而那一轮被中断，于是**把产物本身改坏了**。
现在反证一律写到 `.cache-test/` 下的临时副本（用 `GAMEMOD_DSH_PATCH` 指过去），
并带重入标记防止无限递归（否则会静默挂死，比报错更难查）。

---

## 5. 测试要求

测试放 `devtools/`，命名 `verify_<对象>.py`，**对着真实游戏与第三方真值跑**，
不是「断言应该如此」，而是**打印实际观测值**。

两类脚本都必须有（见 `devtools/README.md`）：

| 类型 | 验证对象 | 能发现什么 |
|---|---|---|
| `verify_<模块>.py` | 模块内部逻辑 | 解析算法错 |
| `verify_mcp_<引擎>.py` | **MCP 工具层** | 能力没接上、返回结构不对、假成功 |

**新增或修改工具时，必须补/改对应的 MCP 层验收断言。**

### 5.1 覆盖率门禁（静态 + 动态，两者分工不同）

| 检查 | 手段 | 回答的问题 | 能当门禁吗 |
|---|---|---|---|
| `verify_tool_coverage.py` | 静态扫源码 | 哪个工具**没人写测试** | ❌ 会**高估** |
| `report_tool_coverage.py --strict` | 运行时垫片 | 哪个工具**真的没被跑过** | ✅ 是门禁 |

静态检查只看「工具名有没有出现在脚本里」，会把
`sess.hook_method(...)` 这种**会话对象**调用也算成覆盖 ——
它报「**38/38** 全覆盖」，而带垫片实测只有 **30/38**
（这两个数字是工具数为 38 时测得的；现在工具数已增长，
比例结论不变，看当前值请直接跑门禁）。
差的 8 个工具分两类原因：

- **垫片自身漏挂**（7 个）：垫片只挂了
  `import gamemod_mcp.server`，漏了 `from gamemod_mcp import server as S`
  这种写法（`verify_mcp_tools.py` 正是这么导入的）→ 那批调用没被记录。
  修掉垫片后回升到 **37/38**。
- **确实只有会话对象调用**（1 个）：`list_images`，补了工具层断言后才到 38/38。

教训：**「记录工具」自身也要被验证**，否则「没有数据」会被误读成「已覆盖」。
新增工具时同理：**必须补工具层断言**，否则动态门禁会立刻掉下来。

所以：**静态检查是「补测试」的导航图，动态覆盖率才是证据**。

一条命令跑全部（自带垫片、开跑前清空旧日志、结束时卡门禁）：

```powershell
python devtools\run_all_verify.py
```

`run_all_verify.py` 把「**断言失败**」与「**进程崩溃**」分开归类：
`verify_agent_js` 自注入 Frida 偶发访问违例，崩了不算产品缺陷，
但**也不算通过**；自注入类脚本多一次重试预算，且**重试必须留痕**
（把偶发失败洗成通过比直接报红更糟）。

### 5.2 真值来源（为什么数字可信）

拿**游戏或平台自己生成的**东西当真值，不许拿自己写的第二个解析器当真值：

| 引擎 | 真值来源 |
|---|---|
| IL2CPP 结构 | 独立解析 + 内部一致性 + 逐条比对 |
| IL2CPP 字段类型 | `il2cpp_dump.cs`（游戏官方 dump，含 `/*0x20*/` 偏移注释） |
| Mono | 微软官方 `System.Reflection.Metadata`（`devtools/groundtruth/mono/`） |
| UE | 游戏自带 `Manifest_UFSFiles_Win64.txt` 打包清单 |

### 5.3 测试脚本的路径规范（重要）

样本路径集中定义，**不许散落在各脚本里硬编码**：

- `sys.path` 插入：用脚本自身位置推导，**不许写绝对的工作区路径**。
- 样本游戏目录：从 `devtools/samples.py` 读取；样本不存在时记为
  **「无法验证」并跳过**，不许假装通过。
- 缓存目录：一律 `<repo>/.cache-test/`，已被 `.gitignore` 排除。

> 历史教训：11 个脚本硬编码了旧工作区路径
> `C:\Users\Administrator\Documents\deepseek-harness\默认工作区\GameModMCP\src`，
> 该路径在本机已不存在 → 整套测试跑不起来。绝对路径是本项目最容易腐坏的东西。

---

## 6. 提交规范

### 6.1 提交时机（Agent 自行遵守）

**每完成一批改动就提交一次**，不要攒到最后，也不要问「要不要提交」。

- 一批 = 一个自洽的改动单元（一个缺陷修复 / 一个工具新增 / 一次文档同步）。
- 提交前必须确认**该批的验证已跑过**（见 §5），红着的结果不许提交。
- 提交后确认 `git status --short` 干净；若有非本批的意外改动，先查明来源再处理。
- 会话结束（回合结束）时，**工作区不允许有未提交的改动**。

> 早期版本曾用 DSH `Stop` hook 自动提交，**已移除**。原因：
> hook 是**进程级全局配置**，注册在 `~/.dsh/hooks/` 的 `Stop` 事件上，
> 会对**所有**项目触发，需要靠项目内哨兵文件才能收窄范围 —— 一个只服务于
> 本仓库的功能，却把配置放进了全局家目录，收益（省一条 `git commit`）
> 与代价（跨项目误伤风险、goal 续跑模式下 `Stop` 未必触发、
> 多一份要维护的脚本与测试）不成比例。
>
> **改为规范约定**：提交是 Agent 的职责，不是基础设施的职责。
> 这样提交边界由「这批改动讲不讲得通」决定，而不是由「回合什么时候结束」决定。
>
> 若将来确实需要自动化，优先考虑**项目内**的方案（如 git hook、CI 校验），
> 而不是往家目录塞全局 hook。

### 6.2 提交信息

格式：`类型(范围): 摘要`，中文，动词开头，说清「改了什么、为什么」。

类型：`新增` / `修复` / `重构` / `文档` / `测试` / `构建` / `清理`

```
修复(runtime): ensure_runtime 早退导致指定 pid 被忽略
新增(测试): verify_tool_inventory 校验文档与代码的工具清单一致
文档(README): 移除不存在的 unhook_all，订正字段偏移的说明
```

### 6.3 绝不入库

- 元数据索引缓存（`*.sqlite`，单个实测 25 MB）
- `__pycache__/`、`bin/`、`obj/`
- 任何凭据、密钥、`.env`
- 游戏本体文件或从游戏提取的大体积产物（真值样本除外交由 `devtools/groundtruth/` 管理）

`.gitignore` 已覆盖上述内容。**入库前用 `git status --short` 与 `git diff --cached --stat` 确认。**

---

## 7. 目录约定

```
├── src/gamemod_mcp/     源码（MCP 服务器本体）
├── devtools/            验证脚本 + 真值来源（不是"可选工具"，是交付物的一部分）
├── docs/                设计文档与优化建议
├── skills/gamemod-mod/  给 Agent 的 skill 说明（**随组合包注册**，不再手工复制）
├── mcp-config/          客户端接入示例（非 DSH 客户端用）
├── agent_logs/          子 Agent 的 prompt/输出归档（见 §8）
│
├── package.json         **DSH 组合包 manifest**
├── cordis.patch.yml     **组合包层**（插入工具链 + MCP 客户端两行）
├── index.js             **Host 插件**（托管 venv / 发布服务 / 注册 skill）
├── locale/              组合包展示元数据（DSH 插件页读这里）
├── icon.svg             组合包图标
├── install.py           非 DSH 场景的一键安装（可选）
└── AGENTS.md            本文件
```

---

## 8. 子 Agent 归档

派生、调用或模拟任何子 Agent（规划者、审查员、研究员等）时，
必须把**输入 prompt 与输出 response 完整**落盘：

- 路径：`./agent_logs/`
- 命名：`YYYYMMDD-HHmmss-[角色].md`

---

## 9. 禁止事项

- ❌ 在 `server.py` 里写解析逻辑。
- ❌ 工具返回值缺少 `ok` 字段，或失败时返回空数据冒充成功。
- ❌ 往 stdout 打印任何东西。
- ❌ 在文档里断言「静态元数据能给出字段偏移 / 方法 RVA」。
- ❌ 硬编码绝对工作区路径（`sys.path`、样本目录、缓存目录）。
- ❌ 新增工具/改签名却不同步 README、SKILL.md 与验收脚本。
- ❌ 把缓存、凭据、构建产物提交进 git。
- ❌ 功能重复的工具各留一个（让 Agent 猜该用哪个）。
