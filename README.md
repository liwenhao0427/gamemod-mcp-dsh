# GameMod MCP

**用元数据定位 Hook 落点的游戏 Mod 工具集** —— 一个 MCP 服务器，让任意通用 Agent（Claude Code / Codex / Cursor / …）都能做游戏 Mod。

支持 **IL2CPP**、**Mono (.NET)**、**Unreal** 三类引擎，45 个工具覆盖
「找游戏 → 建索引 → 查类/字段/方法 → 注入 → Hook → 读内存」全流程。

---

## 它解决什么问题

传统做法是拿内存修改器（Cheat Engine 式）反复搜值、改值、再筛。
问题在于：**每一次都是在猜**，游戏一更新全部作废，而且改完不知道为什么生效。

本工具集换一条路：**游戏自己就带着完整的类型系统**。

| | 传统 CE 式 | GameMod MCP |
|---|---|---|
| 怎么找 "金币" | 反复搜内存值、改值、再筛 | 查元数据拿到 `UserData.gold` 的**字段名与类型**，注入后取它的**真实偏移** |
| 怎么找函数 | AOB 特征码猜机器码 | 查元数据拿方法 **token**，注入后用 token 对账换算出**真实地址** |
| 稳定性 | 游戏更新即失效 | 元数据变了才失效，且自动重建缓存 |
| 靠猜吗 | 靠猜（试错） | 基本不靠猜（可枚举、可比对、可验证） |

> 注意两侧的分工：**静态元数据没有字段偏移、也没有方法地址**（只有 token）。
> 偏移与地址一律来自注入后的运行时探针，`token` 是把两侧对上的钥匙。

具体做法是两段式：

1. **离线解析元数据** —— IL2CPP 的 `global-metadata.dat`、Mono 的托管 dll。
   拿到类型、字段、方法、token 的完整清单。
2. **在线问运行时** —— 注入 Frida 探针，问游戏「这个字段真实偏移多少 /
   这个方法真实地址在哪」。两边的 **token 对上号**，就是「我挂的确实是
   我想挂的那个方法」的硬证据。

AOB 扫描（`scan_pattern`）保留，但**只是符号被剥离时的兜底**。

---

## 快速开始

### 1. 装进 DSH（推荐）

本项目本身就是一个 **DSH 组合包**。在 DSH 里安装这个目录即可，
依赖由组合包自己装好，**没有手工步骤**：

```
让 Agent 用 plugin_manager 的 install_bundle 安装这个目录
```

装完 `mcp__gamemod__*` 工具直接可用，skill 也已随包注册。
细节与原理见下面「安装（DSH 组合包）」一节。

### 2. 接其它 MCP 客户端

不用 DSH 的话，本项目仍是标准的 stdio MCP 服务器：

```bash
pip install -r requirements.txt      # 只有 mcp + frida
python install.py                    # 一键安装（含冒烟测试）
python install.py --vision           # 连可选弱视觉（截图 / OCR）一起装
```

引擎解析器（PE / IL2CPP / Mono / UE）**全部手写、只用标准库**，不依赖
`pefile` / `lief` / 任何反编译框架。

> **本项目只支持 Windows。**
> `install.ps1` 仍保留，但**只能用 `pwsh`（PowerShell 7）跑** ——
> 用 Windows 自带的 `powershell`（5.1）会因中文编码问题直接语法报错，
> 所以主推纯 Python 的 `install.py`。

各客户端的现成配置在 [`mcp-config/`](mcp-config/README.md)，支持
Claude Code、Codex CLI、Cursor 以及任何标准 MCP 客户端。

以 Claude Code 为例，项目根目录建 `.mcp.json`：

```json
{
  "mcpServers": {
    "gamemod": {
      "command": "python",
      "args": ["C:/path/to/gamemod-mcp/src/gamemod_mcp/server.py"],
      "env": {
        "PYTHONPATH": "C:/path/to/gamemod-mcp/src",
        "PYTHONUTF8": "1"
      }
    }
  }
}
```

### 3. 开跑

```
你：帮我找到 Garden of Witches 里的存档服务，看看金币字段叫什么

Agent 会自动：
  get_diagnostics()         → 确认 Frida 可用
  inspect_game(...)         → 识别为 IL2CPP
  open_game(...)            → 建索引（首次约 2s，之后 10ms）
  search_fields("gold")     → 定位候选字段
  class_detail(...)         → 确认所属类与类型
  inject_process()          → 注入探针
  runtime_class_info(...)   → 拿真实字段偏移
```

> **skill 不用手工装**：DSH 组合包会把
> [`skills/gamemod-mod/`](skills/gamemod-mod/SKILL.md) 直接注册进 skill 表
> （含完整工作流、各引擎差异、配方、以及「查不到东西怎么办」的排查表）。
> 非 DSH 客户端才需要把它放进 Agent 的 skill 目录。

---

<!-- BEGIN GENERATED: tools -->
## 工具一览（45 个）

> **本清单由 `python devtools/gen_docs.py` 从代码生成**（来源：各工具函数的 docstring 首段 + `server.py` 的层节注释）。
> **不要手工编辑这一节** —— 下次 `--check` 会报红。
> 要改描述就改该工具的 docstring 首段，再跑 `python devtools/gen_docs.py --write`。
>
> 为什么改成生成：校验只能**发现**漂移，生成本**不会产生**漂移 —— 本表的工具数曾手工漂过 5 次以上。
> 改造前做过「知识上移」核对：原单元格里有 **402 个特征词**是 docstring 没有的，
> 直接生成会丢掉那些手写事实，所以先把它们补进了 docstring 首段。

### 发现层
| 工具 | 作用 |
|---|---|
| `find_game_processes` | 列出正在运行的游戏进程，并识别其运行时（il2cpp / mono / 其它）。 |
| `inspect_binary` | 分析 PE 文件（exe/dll）：架构、节区、导出表、导入表、运行时类型。 |
| `inspect_game` | 探测一个游戏目录：识别引擎、找出主程序与关键文件。 |

### 会话层
| 工具 | 作用 |
|---|---|
| `close_session` | 关闭当前会话：卸载所有 Hook、断开注入、关闭索引。**只有真的恢复原状才 `ok=true`**。 |
| `get_session` | 查看当前会话状态：选中的游戏、索引统计、运行时注入情况。 |
| `open_game` | 选定游戏，并构建（或复用）它的元数据索引（**跨引擎自动识别**）。 |

### 静态查询层（**不需要游戏在运行**）
| 工具 | 作用 |
|---|---|
| `analyze_dependency` | 分析一个类型引用了哪些其它类型（字段/父类/方法返回类型）。**近似，非精确调用图**。 |
| `class_detail` | 查看一个类的完整信息：字段（含类型）、方法（含 token）、继承链；给 `method_name` 可只看某方法的重载。 |
| `class_hierarchy` | 查看类的继承体系（向上找基类、向下找派生类）。`direction="down"` 是**逐层**的：`depth` 是所在层、`parent` 是**真实**直接父类；单层超 500 条时 `down_has_more=true`。 |
| `decompile` | 反编译 Mono/.NET 程序集，拿到**真正的 C# 源码**（薄封装 `ilspycmd`）。 |
| `find_derived_classes` | 找出所有继承自某个基类的类型（含间接继承）；配 `assembly=` 可筛出游戏自己的 MonoBehaviour 脚本（**找脚本的必备工具**）。 |
| `list_images` | 列出游戏里的程序集（dll）及各自的类型数量。支持 `offset`/`limit` 翻页并如实给 `has_more`。 |
| `search_classes` | 搜索游戏里的类、结构体、枚举 —— Mod 开发最主要的入口。支持按基类与程序集过滤。 |
| `search_fields` | 按名字搜索字段，返回所属类与字段类型。**按字段名搜**（定位「金币」「血量」这类目标的首选入口）。 |
| `search_methods` | 按名字搜索方法，返回所属类、返回类型与 token。`owner` 语义与 `search_fields` 一致。 |

### 结果句柄层（大结果集翻页）
| 工具 | 作用 |
|---|---|
| `read_query_result` | 按句柄翻页读取之前某次查询的完整结果；给了 `pattern` 就在结果内再过滤。 |

### 运行时层
| 工具 | 作用 |
|---|---|
| `drain_events` | 取走探针缓冲的 Hook 事件（调用记录）。`kind` 过滤作用于**整条队列**（不是只看队首）。 |
| `eval_script` | 在游戏进程内执行**任意** Frida JS 代码 —— 应急逃生舱。 |
| `find_hook_target` | 定位某个方法可 Hook 的地址（含 RVA 与 token）。同样支持完整嵌套名，歧义时拒绝。 |
| `find_instances` | **枚举类的活对象实例**（拿 `instance` 指针的正路 —— 此前只能靠 Hook 碰运气）。走 GC liveness；**IL2CPP 可用（实测毫秒级）**、Mono 受官方限制会如实报 `unsupported`（**不用空列表冒充「没有实例」**），值类型无堆实例则明确报错。 |
| `gc_control` | 暂停/恢复游戏的 GC（垃圾回收）。 |
| `hook_address` | 在**任意地址**安装 Hook（不限于托管方法）。适用于原生函数、系统 API、以及「模块基址 + RVA」算出的地址。 |
| `hook_method` | 在目标方法上安装 Hook，捕获它的调用（参数、调用来源、返回值）。用 `token` 指定重载；多候选未指定则**拒绝**。 |
| `hook_status` | 列出当前所有 Hook 及其触发次数。 |
| `inject_process` | 把运行时探针注入正在运行的游戏进程。目标换了游戏时会**把会话身份与索引一起换过去**，并在返回里给 `context_switched`。 |
| `launch_game` | 启动游戏并**在游戏初始化之前**注入探针（**能抢在初始化前挂钩**）。 |
| `read_field_value` | 读取某个对象实例上某个字段的原始值。**读几个字节由字段类型决定**（bool 1 / int 4 / long·double·引用 8）。 |
| `read_memory` | 读取游戏进程内存（十六进制返回）。 |
| `runtime_class_info` | 查类的**运行时**实况：**真实字段偏移**与**方法真实地址**（RVA）。 |
| `runtime_info` | 查看已注入游戏的**探针状态**：IL2CPP 模块、映像数量、可用 API。含静态/运行时一致性自检（`image_count_matches`）。 |
| `scan_pattern` | 在游戏模块里做 AOB（特征码）扫描。**这是兜底手段** —— 优先用元数据 + RVA 定位。 |
| `set_field_value` | **写对象字段值**（`read_field_value` 的对称入口）：自动查偏移、按类型定宽度与编码、写完回读核对。**溢出直接报错不截断**；**引用类型字段拒绝**（改引用属改逻辑，请用 `eval_script`/Hook）。 |
| `unhook` | 卸载 Hook。hook_id 留空则卸载全部。 |
| `write_memory` | 写游戏进程内存。 |

### 弱视觉 / UI 层（截图 · OCR · 日志 · 等待）
| 工具 | 作用 |
|---|---|
| `capture_screen` | 截屏 / 截窗口**存盘并返回路径**（不回 base64）。**截图是证据、不是判据**；窗口找不到会如实失败，不静默改截整屏。 |
| `ocr` | **OCR 识别文本并给屏幕绝对坐标**（可直接用于点击）。只对给定 ROI 做（实测整屏 4.1s vs ROI 数十 ms）。 |
| `probe_log` | 读游戏日志**尾部**并按正则过滤（编码自动 utf-8/gbk）。**只读、不消费**，可反复轮询。 |
| `verify_state` | **出一份可证伪的验收结论**：每条断言独立求值，并要求**足够多的独立证据**才判成立。 |
| `wait_for` | **轮询等待条件成立并给出证据包**（验收闭环里「等到可以判了」那一步）。五种条件：`field` / `instances` / `ocr` / `log` / `window`。返回三态 `verdict`（`pass` / `fail` / **`unverifiable`** —— 把「没法判」与「没通过」严格区分）+ `polls` / `evidence`；**超时如实报 fail，不静默当通过**。注意 `kind="log"` 等的是**新增**（先记基线再等增量），不是「当前存在」。 |

### Unreal 引擎层
| 工具 | 作用 |
|---|---|
| `detect_ue` | 判断一个 exe 是否为 Unreal 引擎游戏，并给出可核查的证据与置信度。 |
| `ue_list_entries` | 列出 .pak / .utoc 里**打包了哪些资产**（可按名字过滤、翻页）。返回 `returned`/`has_more`；翻页空页会说明是哪一种「空」。 |
| `ue_manifest` | 读取 UE 的打包清单 `Manifest_*Files*.txt`。支持 `offset`/`limit` 并有 `has_more`。 |
| `ue_profile` | **UE 游戏概况**：容器、清单、**脚本模块**。汇总一个 Unreal 游戏的可提取信息（资源容器、清单、脚本对象）。 |
| `ue_script_objects` | 从 UE 游戏 exe 里提取 /Script/ 模块名与对象路径。失败时按契约给 `ok=false` + `error`。 |

### 诊断层
| 工具 | 作用 |
|---|---|
| `get_diagnostics` | 检查运行环境：Python、Frida、各引擎解析器是否可用。**默认不扫本机进程**（见 `scan_processes`）。 |

<!-- END GENERATED: tools -->

### 句柄格式与作用域

句柄形如 `il2cpp-g3-classes-1`（`{引擎}-g{代次}-{种类}-{序号}`）。
**代次段是正确性要求**：句柄表是每个索引实例各自的、序号从 0 重来，
不带代次就会让旧句柄在切游戏后**静默读到另一个游戏的结果集**（实测过）。
所以切游戏或重建索引后旧句柄一律失效，失败时会返回 `available_handles`。
见 `devtools/verify_handle_scope.py`。

### 响应体积上限（256 KB）

**任何**工具返回都不会超过 256 KB —— 这不是各工具自觉，而是
`src/gamemod_mcp/cap.py` 在**所有**工具外面统一兜底（`@_cap_response`）。

超限时截断**最大的那个顶层列表**，并把完整列表登记成句柄：

- 返回里多出 `capped: true`、`cap_bytes`，`note` 写明「只回了前 N 条」；
- `has_more` 一律改成 `true`、`returned` 改成实际条数（**不许**明明还有却说没有）；
- `note` 里给出续读方式 `read_query_result(handle=..., offset=N)`，
  **一条数据都不会丢** —— 完整列表在句柄里。
- 已有 `handle` 的返回（`search_*`）**复用**它的句柄，不再另造一个；
  此时续读的 `offset` 是「原 offset + 已回条数」，按「已回条数」算会跳过前面的条目。

为什么必须有：契约第 5 条原先只靠各工具的 `limit` 自觉遵守，但 `limit` 上限是
2000，Agent 完全可以合法地要 2000 条 —— 实测 `search_classes('', limit=2000)`
的线上文本是 **856 KB**。截断后同一调用是 260 KB。
见 `devtools/verify_cap.py`（机制）与 `devtools/verify_response_size.py`（体积）。

## 资源（MCP resources）

只有一个：**`gamemod://guide`** —— 这份 MCP 自己的完整使用指南
（与 `skills/gamemod-mod/SKILL.md` 同源，逐字节相同）。

```text
gamemod://guide   text/markdown   约 24 KB
```

支持 resources 的客户端可以直接读它，不必先在系统提示里塞进这份长文档。
它也是全项目**唯一没有工具出口**的内容：其余（会话状态、引擎能力矩阵、
缓存与依赖清单）都已经由 `get_session` / `open_game` / `get_diagnostics`
返回，再开一个资源出口就是同一份数据的第二个来源（契约第 3 条）。

### 为什么没有 prompts

MCP 还有 `prompts` 这一面，本服务器**不做**，这是量过的结论而不是遗漏：

| 协议面 | 客户端（DSH）实测 | 结论 |
|---|---|---|
| `resources/list` `resources/templates/list` `resources/read` | 客户端实现了，且把资源包成给模型用的工具（模型够得着） | ✅ 做 |
| `prompts/list` `prompts/get` | 客户端**一个 prompt 方法都没有**，也没有对应的包 | ❌ 不做 |

证据：在 `E:\DSH\resources\app.asar` 里按路径检索 JS，
`dsh-mcp-client` 的请求分发 `switch` 里只有 `resources/*` 三个分支，
`prompts/*` **零命中**。加了 prompt 也传不到模型面前，
只会是一份没人读的声明 —— 与当初否决 `outputSchema` 是同一套判断方式
（见 `docs/项目梳理与优化建议.md` §4.14 附近）。

> 反过来说：**换个客户端就要重新量一次**。如果某个客户端确实消费 prompts，
> 「把 SKILL.md 挂成带 `engine` 参数的 prompt」是现成的做法。
> `devtools/verify_mcp_resources.py` 已经把「资源清单恰好只有 guide」
> 做成断言，将来补资源时会强制过一次讨论。

---

## 目录结构

```
gamemod-mcp/
├── README.md                    本文件
├── pyproject.toml               打包配置（pip install -e . 可用）
├── requirements.txt             运行依赖（只有 mcp + frida）
├── install.py                   一键安装（非 DSH 场景）+ 冒烟测试
├── install.ps1                  同上，但**只能用 pwsh 7**（5.1 读中文会语法报错）
│
├── src/gamemod_mcp/
│   ├── server.py                MCP 服务器（45 个工具）
│   ├── logutil.py               日志（只走 stderr，stdout 留给 JSON-RPC）
│   ├── engine/                  静态分析层
│   │   ├── pe.py                纯标准库 PE/COFF 解析器
│   │   ├── discovery.py         进程枚举 / 引擎识别（不再做全机 Steam 扫描）
│   │   ├── il2cpp_meta.py       IL2CPP global-metadata.dat 解析器
│   │   ├── mono_meta.py         ECMA-335 元数据解析器（45 张表）
│   │   ├── ue.py                Unreal 提取器（pak / utoc / 清单 / 脚本对象）
│   │   ├── adapt.py             **引擎适配层**（让索引层不关心引擎差异）
│   │   ├── decompile.py         Mono 反编译薄封装（可选外部依赖 ilspycmd）
│   │   ├── index.py             SQLite 索引 + 结果句柄
│   │   └── state.py             会话状态（发现→索引→运行时的粘合层）
│   └── runtime/                 动态分析层
│       ├── agent.js             Frida 探针（IL2CPP 59 + Mono 56 个 API，双后端）
│       └── session.py           Frida 生命周期管理
│
├── skills/gamemod-mod/SKILL.md  Agent 使用指南（随组合包注册给 Agent）
├── mcp-config/                  各客户端的 MCP 配置样例
├── agent_logs/                  子 Agent 委派记录（prompt + 产出）
│
├── package.json                 **DSH 组合包 manifest**（声明 dsh.bundle.patch）
├── cordis.patch.yml             **组合包层**：插入「工具链 + MCP 客户端」两行
├── index.js                     **Host 插件**：准备托管 venv、发布服务、注册 skill
├── locale/en.json, zh.json      组合包展示元数据（DSH 插件页读这里）
├── icon.svg                     组合包图标
│
└── devtools/                    验证脚本（见 devtools/README.md）
    ├── verify_*.py              验收脚本（对真实游戏与地面真值）
    ├── check_encoding.ps1       编码校验（UTF-8 无 BOM）
    ├── archive/                 归档的过程产物（不参与验收）
    │   └── probes/              一次性逆向侦察脚本（格式出问题时重启定位用）
    └── groundtruth/             真值来源（MonoGroundTruth / 官方 dump）
```

---

## 引擎支持程度

| | IL2CPP | Mono (.NET) | Unreal |
|---|---|---|---|
| 静态查询类型 | ✅ 完整 | ✅ 完整 | ❌ 无类型系统可查 |
| 字段类型精度 | ⚠️ 启发式（~90%） | ✅ 精确（ECMA-335 签名） | — |
| 字段偏移 | ✅ 运行时取 | ✅ 运行时取（`mono_field_get_offset`） | ❌ 需运行时，未实现 |
| 方法地址 | ✅ 运行时取（含 RVA） | ✅ 运行时取（JIT 机器码，**无 RVA**） | — |
| 运行时 Hook | ✅ | ✅（地址在 mono 模块外，直接按地址挂） | — |
| GC 暂停 | ✅ `il2cpp_gc_disable` | ❌ 本版 Mono 无 `mono_gc_disable` 导出 | — |
| **资源容器清单** | — | — | ✅ **pak/utoc 100% 准确** |
| 可反编译出源码 | ❌（已转 C++） | ✅（ILSpy/dnSpy 接近源码） | ❌ |

> **两个引擎的运行时配方一致**：注入 → `runtime_class_info` 取偏移 →
> `find_hook_target` 取落点 → `hook_method` + `drain_events`。
> 探针里是两套实现（`il2cpp_*` 59 个 API / `mono_*` 56 个 API），
> 对外工具与 token 对账方式完全相同（静态 token == 运行时 token）。
> Mono 上有四条差异，都是**如实声明**而非「未实现」：
>
> 1. **没有 RVA**：地址来自 `mono_compile_method` 触发的 JIT，
>    代码不在 `mono-2.0-*.dll` 内，所以 `in_module: false` 且不给 `rva`；
>    元数据里的 RVA 指向 **IL 字节码**，不能拿来 Hook。
> 2. **`gc_control` 不支持**：没有 `mono_gc_disable` / `mono_gc_enable`
>    导出（四个 Mono 样本一致核实），调用会明确返回 `ok:false`。
> 3. **程序集懒加载**：刚注入时只枚举到十来个程序集，`UnityEngine.*`
>    要过几秒才出现（实测 13 → 164）。**注入成功后探针会等它加载稳定再返回**
>    （判据是数量连续 4 次采样不变，实测等了 4.0 秒、看到 144 个），
>    结果放在 `init.assemblies` 里；返回里的 `searched_images`
>    也会如实给出当前数量，若仍未稳定可稍后重试。
>    为什么必须等（不是保险起见）：矩阵实测 Archon Soul 在只有 13 个程序集时
>    被 Hook 并强制调用 `get_frameCount`，**进程随后消失** ——
>    「注入成功」与「可以 Hook 托管方法」在 Mono 上不是同一件事。
> 4. **静态索引的范围**：Mono 的静态索引默认只收游戏自己的程序集
>    （`Assembly-CSharp.dll`），`UnityEngine.*` 这类框架类型**静态查不到**。
>    `class_detail` 会明确说明这一点并指向 `runtime_class_info`，
>    而不是只回一句「用 search_classes 搜短名」（那条路走不通，它压根没被索引）。

实测数据（真实游戏，非构造样本）：

- **IL2CPP**（Garden of Witches，17 MB 元数据）：
  20,072 类型 / 139,501 方法 / 81,872 字段 / 94 映像。
  字段类型解析率 90.10%，**与地面真值 dump 比对 27,264 条 0 错（100% 精度）**。
  建索引 2.6s，缓存复用 1.3s。
- **Mono**（Archon Soul，Assembly-CSharp.dll）：
  1,387 类型 / 6,611 方法 / 6,343 字段。
  与 Microsoft 官方 `System.Reflection.Metadata` 交叉验证 **45 张表 0 差异**，
  每类型的全名/基类/字段名/方法名 0 差异，签名解析 100%。
  **运行时**（Frida 注入同一游戏，实测）：
  `mono_*` 导出绑定 56 个、零缺失（四个样本的 PE 导出表逐个核实）；
  程序集清单随游戏运行从 **13 涨到 164**（Mono 是懒加载，跨调用缓存会永久漏掉
  `UnityEngine.*`）；`UnityEngine.Time` 的 token `0x020002e4` 与静态元数据
  **完全一致**（`Time.get_frameCount` `0x06002468` 亦然，独立复现了
  `System.Reflection.Metadata` 真值）；短名解析在 27,222 行类型定义里
  0.1s 完成；Hook 上 `get_frameCount` 后捕获到真实返回值（游戏运行 14.5s 时
  `retval=872`，与 60fps 自洽）。方法地址是 **JIT 机器码**，`in_module=false`，
  因此**没有 RVA**（不给假数据）；`gc_control` 如实报不支持。
  另一个实测到的坑：游戏跑约 90 秒后，**同一个程序集会被加载出两个映像**，
  `UnityEngine.Time` 于是被枚举出两条**行号/token/命名空间/程序集名全同、
  只有 klass 指针不同**的记录 —— 不按（程序集名 + 命名空间 + 短名）去重的话，
  一次本可解析的查询会变成「命中 2 个同名类」，而调用方**无法消解**
  （两条的 image / namespace / full_name 一模一样）。顺带一提，
  内存加载的程序集 `mono_image_get_filename` 返回的是 `data-00000219A9D5A040`
  这种**合成名**，不是路径，按路径去重不会命中。
  第四个样本 Gambonanza 上复现并修掉了「注入太早就 Hook」这个缺陷：
  修复前注入后 4 秒时只有 15 个程序集，`Transform` / `Time` 一律「没找到类」；
  修复后注入即等到 144 个程序集稳定（4.0 秒），`UnityEngine.Transform`、
  `UnityEngine.Time`、`UnityEngine.GameObject` 全部解析成功，
  `find_hook_target` 给出候选，10 秒后进程仍在。
- **Unreal**（CatapultHero + Dinoblade 两个样本）：
  `.pak` v11/v12 索引与 `.utoc` v8 目录索引完整解析，
  **与打包清单交叉校验 4,556/4,556、4,565/4,565、7,178/7,178、18,956/18,956 全部 100% 命中**；
  含 UTF-16 中文路径（`字库`、`精灵片`、`角色`）正确解码。
  **FName 池、属性偏移、`.usmap` 未实现**——静态样本中不可得，诊断里如实标注。

---

## 可信度：怎么证明它是对的

这个项目**不靠「看起来能跑」**。`devtools/` 下有 51 个可复现的验证脚本，
每个都对着**真实游戏**和**地面真值**跑（其中 50 个进自动套件，
`verify_tool_coverage.py` 是静态导航图、不是门禁，故不参加）：

| 脚本 | 验证内容 |
|---|---|
| `verify_parser.py` | IL2CPP 元数据表结构与记录布局 |
| `verify_report_fixes.py` | **《重启验证与运行时理解报告》§4 那 5 个缺陷的修复**：同步工具不许占住事件循环（分发路径必须换线程）、探针 RPC 必须有超时且卡死时不假成功、失败的 `open_game` 不许污染会话身份、`ue_manifest` 必须优先挑 UFS 清单、候选截断必须标 `has_more`。**每条都带非空转反证**（把实现改回旧写法必须报红） |
| `verify_typedef_stride.py` | **typeDefinitions 记录大小**：按版本号硬编码会错（v29 实测是 88 不是 92，用错会让 Σfield_count 变 6.5 亿、`open_game` 撞 `UNIQUE constraint failed: fields.idx`）。本脚本用**合成元数据**验证「字段区间恰好铺满 fields 表」这条判据本身、自验证改选必须 warn、并复现历史 bug（不依赖本机装了哪款游戏） |
| `verify_index.py` | 索引层：构建 / 缓存复用 / 各类查询 / 结果句柄 |
| `verify_lazy_handles.py` | 结果句柄惰性物化：用**解析条数**断言证明翻页只解析一页 |
| `verify_handle_scope.py` | 结果句柄的**作用域**：跨游戏/重开索引的旧句柄必须被拒绝，不许读到另一个结果集 |
| `verify_session_switch.py` | **会话身份**：切换游戏时上一个游戏的 `exe` 不许留下来（纯函数注入 + 真实 Mono→IL2CPP→UE 切换 + 进程级 stdio，含非空转反证） |
| `verify_context_follow.py` | **上下文跟随目标**：`launch_game(exe=…)` / `inject_process(pid=…)` 换了游戏时必须把会话身份与索引一起换过去（否则 `image_count_matches=false`，静态 token 与运行时地址属于两个游戏）；反推不出游戏目录时不许硬猜、不许把上下文指到盘符根（`C:\` 曾被误判为 Mono 游戏）；对齐过程不许关掉刚注入的运行时。含非空转反证 |
| `verify_drain_kind_filter.py` | **`drain_events(kind=…)` 的过滤作用于整条队列**，不是只看队首（旧写法遇到队首那条 `notice`(probe_ready) 就 `break`，实测在真游戏上返回 0 条而待取 201 条 —— 调用方会误判成「Hook 没触发」而去排查落点）。含不匹配事件不许被吞、超 `limit` 不许丢、顺序不变、`kind=""` 行为不变，以及把旧写法搬回来的反证 |
| `verify_mcp_discovery.py` | **目录布局判定**：容器目录（装着多款游戏的父目录）不许被当成某一个游戏、UE 不许被送去建索引、不支持的引擎要给出家族提示（真实样本 + 合成布局 + 进程级 stdio，含非空转反证） |
| `verify_subtype.py` | 继承过滤反过来查：**与旧的逐条判定逐条相等** + 邻接表只建一次 |
| `verify_fieldtypes.py` | 字段类型解析率与**精度**（对 27,264 条 dump 真值） |
| `verify_params.py` | **方法参数**：v31 记录里哪个偏移才是 `parameterStart`（+12 恒定在表外、+16 单调且落在参数表内）、解析率、参数名精度（对 89,134 组 dump 真值 99.9%）、参数个数对账，含「改回旧偏移命中 0」的反证 |
| `verify_adapt.py` | **跨引擎**：同一套查询 API 吃 IL2CPP + Mono |
| `verify_mono.py` | Mono 解析器，对官方 `System.Reflection.Metadata` 真值 |
| `verify_mono_coverage.py` | Mono 覆盖到的表 / 特性范围 |
| `verify_mono_robust.py` | Mono 对畸形输入的健壮性 |
| `verify_ue.py` | UE 提取器对两个真实样本（pak / utoc，对打包清单） |
| `verify_runtime.py` | Frida 运行时层 |
| `verify_runtime_target.py` | 运行时定位 Hook 落点（token 对账） |
| `verify_il2cpp_runtime.py` | 静态 token ↔ 运行时 token 交叉验证 |
| `verify_mono_runtime.py` | **Mono 运行时探针**：56 个 `mono_*` 导出对四个样本的 PE 导出表逐个核实（含伪造名字反证）、`int/uint32/bool` 返回类型不许用 `.toInt32()`（一个真实崩溃的回归）、程序集清单按 RPC 刷新（13 → 164 的懒加载证据）、**注入后等程序集加载稳定**（用假会话测增长/恒定/一直增长/探针报错四种情形，并反证 IL2CPP 一次都不查）、真游戏上取 `UnityEngine.Time` 的真实地址与 token 对账、Hook + 事件、`gc_control` 如实报不支持 |
| `verify_mcp_tools.py` | **通过 MCP 工具层**跑完整流程（IL2CPP） |
| `verify_mcp_mono.py` | **通过 MCP 工具层**跑完整流程（Mono） |
| `verify_mcp_ue.py` | **通过 MCP 工具层**验证 UE 能力真的接上了 |
| `verify_mcp_decompile.py` | `decompile` 工具（`ilspycmd` 薄封装） |
| `verify_mcp_extra.py` | 补全主流程没覆盖到的工具 |
| `verify_server_helpers.py` | `server.py` 的纯函数：命令行拆分（`launch_game`）与字段读取宽度（`read_field_value`） |
| `verify_field_write.py` | **字段写入的编码**（`set_field_value`）：宽度按类型、溢出必须报错不截断、浮点/整数不混、引用类型被拒。含**非空转反证**（改回「一律 8 字节」或去掉范围检查，断言必须报红） |
| `verify_find_instances.py` | **实例枚举的语义**（`find_instances`）：`max_count`/`namespace` 真的传下去、**派生类混入可核查**（`exact+derived==count`；实测 `Transform` 全返回 `RectTransform`）、「拿不到」不许冒充「没有」（Mono 时机限制必须 `unsupported`）、只读标注。含非空转反证 |
| `verify_screen.py` | **弱视觉/UI 层**（`capture_screen`/`ocr`/`probe_log`）：**ROI 原点加回坐标**（用固定 OCR 桩做到确定性）、「没找到 ≠ 不存在」的语义、失败留证据、日志 GBK 自动探测与**只读不消费**、region 畸形输入。含非空转反证；缺可选依赖时记为「无法验证」而非通过 |
| `verify_wait_for.py` | **轮询等待与三态判定**（`wait_for`）：超时如实报 `fail`（不静默当通过）、**`unverifiable` 与 `fail` 严格区分**、`timeout_ms`/`interval_ms` 真的影响行为、条件**后来**成立时能等到、`evidence` 有上限但 `polls` 计数完整、**`kind="log"` 等的是增量而非存在**（旧日志已有时不许秒过）。含非空转反证 |
| `verify_verify_state.py` | **证据充分性契约**（`verify_state`）：**单条证据不得判 `pass`**（`require_independent` 默认 2）、三态区分、`checks` 写法校验、与 `wait_for` 共用同一套条件语义（`_eval_condition`）。含非空转反证（把要求降到 1 则同一输入变 `pass`） |
| `verify_docs_admin_claim.py` | **不许再出现「注入需要管理员权限」这个错误说法**（曾扩散到 8 处以上，源头是 `session.py` 失败提示里的一句猜测性归因；实测普通权限注入成功）。同时**要求**正面给出「不需要管理员」+ 权限级例外 —— 只删错误说法不留正确说法 = 丢信息。第 4 节检查**分发层**（组合包产物）不许写死绝对路径、顺序必须由 `inject` 表达。含非空转反证（用**真实出现过**的原句做样本） |
| `verify_dsh_bundle.py` | **DSH 组合包层**（`index.js` / `cordis.patch.yml` / `package.json`）：manifest 声明与展示元数据位置（`readPluginMeta` 读的是 `locale/*.json` 的 `meta`，**不是** `package.json.meta`）、patch 不写死路径、顺序靠 `inject` 而非行序、`mcp<2` 三处一致；**行为层**真的 `import` 并调 `apply()`，用假 ctx 核对发布的服务字段、可选 `skills` 注入、skill 真的被注册且 frontmatter 已剥离、坏 config 必须被拒。含非空转反证（把 patch 改坏必须报红，且**不污染被测产物**） |
| `verify_response_size.py` | **响应体积**：P2-4 前后对比（用 `S.RAW_TOOLS` 调未套上限的原函数）、上限不变量、旧记录订正 |
| `verify_cap.py` | **体积上限的机制**：截断/句柄/字段诚实性/不丢数据（纯函数 + 端到端两层） |
| `verify_mcp_resources.py` | **MCP 资源层**：`gamemod://guide` 与磁盘 SKILL.md 逐字节相同、资源清单恰好只有它、找不到文件时如实说明 |
| `verify_mcp_context.py` | **P2-3 的决策门禁**（不是行为测试）：钉住「进度无消费方、取消够不着、tasks 会弄坏」这三条否决依据，变了就报红 |
| `verify_stdio_entry.py` | **进程级入口**：脚本形式（`python <repo>/src/gamemod_mcp/server.py`，且不带 `PYTHONPATH`）与模块形式都能握手；两种形式的工具清单一致；stdout 只有 JSON-RPC；脚本形式下真调 `ue_*` / `decompile`（惰性导入曾在这里炸） |
| `verify_spawn_stdio.py` | **子进程 stdout 不许污染协议**：`launch_game` 启动的游戏会往同一个 stdout 打 30 行日志（其中 `"memorysetup-..."` 是合法 JSON 字符串）；断言产品用的是 `stdio="pipe"`、默认 `inherit` 确实会漏（反证）、`pipe` 不漏且 4 MB 洪水不会卡死子进程 |
| `verify_thread_attach.py` | **探针线程附着 IL2CPP 域**：模块已加载 ≠ 域已建好（实测 6/9 个 IL2CPP 游戏上 `il2cpp_domain_get()` 返回野指针 0x135）；断言不再「附着失败 → 记一笔就继续」（附旧写法反证）、`wait_ready` 重试契约（桩对象确定性验证）、真游戏上没有任何一次「ok=true 却带着附着失败」、附着后 il2cpp 调用可用且**游戏进程仍活着**、读野指针确实抛 access violation |
| `verify_class_resolve.py` | **运行时类解析的三条规则**：只给短名要能解析到顶层类型（老版本在 4 个游戏上报「未找到类 Transform」）、三条路（短名/显式 namespace/完整名）必须指向同一个类（比字段偏移表）、嵌套类型只认完整嵌套名（反证：7056 个嵌套类型里挑一个短名不撞顶层的，短名必须查不到、完整名必须查到）、不存在的类要给出 namespace 提示 |
| `verify_cache.py` | **索引缓存的生命周期**（P3-2）：只许删形状正确的文件（含反证）、三条护栏、命中时 touch、`cache` 统计与磁盘一致 |
| `verify_lazy_meta.py` | **元数据解析何时发生**（P1-3b 的决策门禁）：命中不解析不许退化、解析每次 open 只发生一次、逐题量哪些查询会逼出解析器、以及「载入失败不许被降级成『没有基类』」（含把旧写法搬回来的反证） |
| `verify_merged_tools.py` | 合并后的工具：参数区分而不是各留一个 |
| `verify_agent_js.py` | Frida 探针能编译、能加载、能 ping |
| `verify_tool_inventory.py` | 文档与代码的工具清单、脚本清单一致 |
| `verify_tool_annotations.py` | 45 个工具的**语义标注**：危险工具必须声明 `destructiveHint` |
| `verify_tool_coverage.py` | 静态扫「哪个工具没人写测试」（导航图，**会高估**） |
| `verify_encoding_console.py` | 验收脚本在 GBK 控制台不崩 |

> 上表与 `devtools/verify_*.py` 的一致性由 `verify_tool_inventory.py` 校验，
> 所以它不会像以前那样悄悄腐坏（本节曾写「10 个脚本」而实际已有 20 多个）。

几个关键的「硬证据」：

- **静态 token == 运行时 token**（50/50、17/17、9/9 全中）
  → 证明确实挂到了目标方法，而不是同名重载。
- **`PresetLoaderV2.userDataPresetFile` 运行时偏移 = 0x20**，
  与地面真值 dump 里的 `/*0x20*/` 一致 → 证明字段偏移是真的。
- **运行时映像数 94 == 静态映像数 94** → 证明探针挂到的是同一个游戏实例。
- **字段类型 27,264 条与 dump 比对 0 错** → 证明 `derived` 那一档没有蒙错。

跑法（**一条命令跑全部**，自带垫片、开跑前清空旧日志、结束时卡覆盖率门禁）：

```powershell
python devtools\run_all_verify.py
```

只跑一两个排查时用 `--only`（注意此时会**跳过**覆盖率门禁，因为子集不可能覆盖全部工具）：

```powershell
python devtools\run_all_verify.py --only verify_lazy_handles
```

> `verify_mcp_tools.py` 会**真的启动一个游戏**并在结束时结束它。
> 它需要 Frida 与一个 IL2CPP 样本（如 Garden of Witches）。
> **不需要管理员权限** —— 实测普通用户权限即可（见下方「权限」一节）。
> 样本不存在的脚本会记为「无法验证」并跳过 —— **跳过不算通过**。

### 多游戏矩阵：能力边界不靠单一样本推断

单一样本只能证明「在某个游戏上能用」。`devtools/` 下有两个**跑遍全部样本**
的报告脚本（只出报告、不改代码，退出码恒为 0）：

```powershell
python devtools\report_multi_game_matrix.py   # 静态：样本 × 工具的候选分类
python devtools\report_runtime_matrix.py      # 运行时：真的启动/注入/Hook，逐工具判定
```

`report_runtime_matrix.py` 会**真的**启动每个游戏、注入探针、装 Hook、
强制调用并读回参数与返回值，最后按「游戏 × 45 个工具」输出
`ok / 不适用 / 失败` 三态（`--json` 落盘 + `--md` 出报告）。
最近一次全量结果见 [`docs/多游戏运行时兼容性报告.md`](docs/多游戏运行时兼容性报告.md)。

> 它对「正确拒绝」与「工具坏了」分得很清：`decompile` 在 IL2CPP 上返回
> 「不是 .NET 程序集」、短名有歧义时返回 `ambiguous` + 候选，都算
> **不适用 / 正确拒绝**，不算失败 —— 否则报告会把设计行为统计成缺陷。

### 验收套件没全绿？先看失败清单

`python devtools\run_all_verify.py` 的汇总行只打印失败脚本的**名字**，
看不出「环境缺样本」还是「代码真坏了」——这两类必须分开处理。
逐项的现象 / 根因 / 修复路径 / 验证方法见
[`docs/验收失败清单.md`](docs/验收失败清单.md)。

> 该文件是**滚动更新**的：修掉一项就划掉一项。当前快照（2026-10-02）为
> `通过 33 / 40`，7 项失败中 **1 项是产品缺陷、2 项是脚本用了 Python 3.12
> 语法、4 项是缺样本**。

---

## 设计取舍

### 为什么不做 MCP 的进度、取消与 tasks

这三样都是**协议里有、但这个客户端不消费**的东西，量过之后才决定不做：

| 协议面 | 实测 | 结论 |
|---|---|---|
| `notifications/progress` | 客户端调工具时只传 `{signal, timeout}`，**从不传 `onprogress`** → 请求里没有 `progressToken` → `report_progress` 是静默 no-op；而且它是 `async`，同步工具根本调不了 | 不做 |
| `notifications/cancelled` | 客户端**确实**会传取消信号，但服务器这一侧够不着：FastMCP 对同步工具是**内联调用**（`return fn(...)`，不开线程），事件循环被占满，连取消通知都读不到 | 不做 |
| `notifications/message`（MCP logging） | 同样只有通用 SDK 里有，没有任何客户端包消费 | 不做 |
| tasks 扩展（`taskSupport`） | 客户端桥**直接抛错**：`requires task-based execution, which this bridge does not support` | 不做（声明 `required` 会主动弄坏） |

**代价是诚实的**：一个 60 秒的 `launch_game` 期间，这个服务器什么都干不了
（同步工具占着事件循环），用户在客户端上只能看着转圈。
要改就得把三个长任务改成 `async` + `anyio.to_thread.run_sync` 包一层 ——
那是结构性改造，而协议那头的收益恰好是没人消费的。

这三条依据由 `devtools/verify_mcp_context.py` 钉住：**变了就报红**，
提醒重读判断，而不是让断言变绿。

### 为什么引擎解析器全部手写，不用现成库

`pefile` / `lief` 更省事，但会带来三个问题：依赖体积、版本漂移、
以及**遇到魔改文件时无从下手**。自己写 PE 解析不到 400 行，
IL2CPP 元数据解析 1,000 行——换来的是零依赖和完全可控的错误信息。

### 为什么要有 `adapt.py` 这一层

IL2CPP 和 Mono 是**两个根本不同的元数据体系**：

- **IL2CPP**：字段/方法各自的索引轨道相互独立，字段的 `typeIndex`
  甚至不在自己的索引空间里，得靠「比它小的最大 byval 键」推算（~90% 可解）。
- **Mono**：标准 ECMA-335，类型都编码在 `#Blob` 签名里，能**精确**解出
  完整类型名（数组、泛型、指针都对）。

但两者在「类型/字段/方法的归属关系」上是**同构**的。把类型引用的
表示差异收进 `adapt.py`，索引层就能共用一套代码——这也是
`verify_adapt.py` 存在的意义：证明同一套 API 两边都跑得通。

### 为什么宁可返回 `ok: false` 也不猜

工具返回**必定**带 `ok` 字段。索引层用 `found=False` 表示「查了但没有」，
服务器层会把它转成明确的 `ok: false` + `error`。

原因很直接：**假成功是最危险的一类错误**。如果「类不存在」返回
`ok: true` + 空列表，Agent 会以为类存在但没有字段，然后基于这个
错误结论继续往下走。宁可报错。

同理，IL2CPP v31 的字段记录里**没有 flags 槽**，所以
`field_is_static` 返回 `None` 而不是猜一个 `False`——
猜出来的 `False` 会让 Agent 以为它验证过。

### 索引缓存怎么管（会一直长大的那个目录）

缓存文件名是 `<引擎>-<元数据 sha256 前 16 位>.sqlite`。这个键选得对
（换游戏、换版本必定重建，不会串数据），但它有个**没人管过的副作用**：

> 「玩过多少游戏」+「游戏更新过多少次」，都会**永久**堆在
> `~/.gamemod/cache` 里。实测一个 IL2CPP 游戏的索引 **25.7 MB**，
> 而 Mod 开发本来就要反复更新游戏 —— 每次更新换一个新文件，
> 旧文件再也不可能被命中。

以前这件事在任何工具返回里都看不见（`get_diagnostics` 只报一个路径字符串）。
现在：

| 能力 | 在哪 | 说明 |
|---|---|---|
| **看得见** | `get_diagnostics` 的 `cache`、`index_info` 的 `cache` | `{dir, files, total_bytes}`，现数现算的真数 |
| **有边界** | 打开索引时自动清理 | 上限 **2 GiB**；最近 **4** 个、**7 天**内用过的永不删 |
| **在用的不会被误删** | 命中缓存时刷新 mtime | 否则天天在用的缓存会被当成「最久没用」先删掉 |
| **删了什么要说** | `index_info` 的 `cache_evicted` | 真删了才有这个字段；没删就不给（不给恒定噪声） |

清理有**三条护栏**，而且**只删文件名形状正确的**（`<引擎>-<16 位十六进制>.sqlite`）：
缓存目录里可能有你自己的东西，删文件不可逆，所以宁可漏删不可错删。
按上面的边界，5 个游戏的 ~130 MB **永远不会被碰**。

想立刻腾空间就直接删 `~/.gamemod/cache` 里的东西 —— 它只是可重建的派生数据，
代价是下次打开那个游戏重新解析（IL2CPP 约 2.6 秒，巨型样本约 12 秒）。
这条策略的验收在 `devtools/verify_cache.py`（含「把正则放宽就会误删」的反证）。

### 那「第一次查询」为什么还要等 400 毫秒

缓存命中确实不解析元数据（IL2CPP 528 → 19.6 ms），但**紧接着的第一次查询**
会把它逼出来：索引里存的是「有哪些类型」，而基类名、字段类型、方法返回类型
这些**可读名字**是查询时由解析器算的。实测 8 个查询里 7 个会逼出来
（只有 `index_info` 不用），IL2CPP 约 **440 ms**，在 5 查询的一次会话里占 65~78%。

把这些名字也存进 SQLite（路线图里的 **P1-3b**）能省掉这 440 ms。
**已经量过，结论是不做**，理由三条：

1. **半量修复等于零**。`search_classes` 需要解析器只因 `parent` 一个字段，
   但「只存 `parent_name`」省不下任何东西 —— 紧接着的 `class_detail` 仍要解析。
   解析是**全有或全无**，所以「`parent_name`/`field_type` 入库」这种按列拆的
   写法本身就误导：这两列不能分别计价。
2. **完整修复要动分层**。要让 7 个查询都不解析，得把 `class_detail` /
   `class_hierarchy` / `analyze_dependency` 等 **10 个**查询层函数改成完全由
   SQLite 驱动 —— 那等于在索引层**再实现一遍继承语义**，而 AGENTS.md §3
   要求引擎差异只出现在 `adapt.py`。两份继承语义互相漂移，正是本项目反复
   吃亏的那类错误。
3. **省下的是「每次 `open_game` 一次」的 440 ms**，不是每题 440 ms。

理由与数字钉在 `devtools/verify_lazy_meta.py`（既是回归门禁，也是这个决策的
门禁）：哪天那张「哪些查询需要解析器」的表空了，它会提醒回来订正结论。

顺带，这个脚本还捞到并钉住了一个真缺陷：元数据**载入失败**曾被
`except Exception` 降级成「这些类没有基类」（`ok: true` + 200 行 `parent: null`）。
现在载入失败会如实报 `ok: false` + 原因，验收就是那段「把旧写法搬回来」的反证。

---

## 已知限制

- **IL2CPP 字段类型约 10% 解不出**。这是元数据格式本身的限制
  （复合类型只给到元素类型），不是 bug。工具会标记
  `type_confidence: unknown`，需要时用运行时内存读取确认。
- **反调试游戏注入会失败**。带 EAC/BattlEye 的网游基本注入不了，
  这是预期的，不要反复重试。
- **Unreal 支持受限**。UE 没有像 IL2CPP 那样集中的元数据文件，
  结构信息依赖 UE profile / `.usmap`。能力以 `get_diagnostics()` 报告为准。
- **Windows 注入不需要管理员权限**（实测订正）。
  只要 Agent 与游戏的**完整性级别相同**即可 —— 普通权限的 python
  注入普通权限的 IL2CPP 游戏**实测成功**。唯一的例外是
  **游戏以管理员身份运行**（完整性级别 `High`），此时才需要同样提权；
  反之游戏是普通权限、Agent 是管理员，通常也能注入。
  > 这条曾经写错成「通常需要管理员权限」，来源是失败提示里的一句
  > 猜测性归因。实测证据：本进程与游戏进程的完整性级别都是
  > `Medium`，`attach` → `runtime_info` → `find_instances` 全部成功。
- **Headless 环境验证不到自然调用**。没有显示/GPU 的机器上游戏不渲染帧，
  每帧方法不会自己触发。Hook 有效性通过从注入侧主动调用来证明。

---

## 环境要求

- **Windows**（本项目只支持 Windows）
- **Python ≥ 3.10**（实测 3.13.1）
- `mcp ≥ 1.0, < 2`、`frida ≥ 16`（实测 frida 17.17.0）

> ⚠️ `mcp` **必须 < 2**：本代码用 1.x 的 `mcp.server.fastmcp.FastMCP`，
> 2.x 已改名成 `MCPServer`（`mcp.server.mcpserver`）。只写 `>=1.0.0`
> 会让全新环境装到 2.x 然后**服务器起不来** —— 这是打包分享包时实测到的。

> 可选弱视觉（截图 / OCR）：`dxcam`|`mss` + `rapidocr-onnxruntime`|`pytesseract`
> + `numpy`/`opencv-python`/`pillow`/`pywin32`。**不装不影响其它工具**，
> `get_diagnostics()` 的 `screen` 段会如实报告可用性。

---

## 安装（DSH 组合包）

本项目本身就是一个 **DSH 组合包**（bundle）。在 DSH 里装它就完事了：

```
把 E:\project\gamemod-mcp-dsh 这个目录交给 DSH，让 Agent 安装
```

Agent 侧的等价操作是 `plugin_manager` 的 `install_bundle`，
`target` 传这个包的绝对路径。装完 `mcp__gamemod__*` 工具就直接可用。

**装完不需要任何手工步骤。** 组合包在激活时会自己：

1. 在 `$DSH_HOME/gamemod-mcp/venv` 下建一个**本项目专用**的 venv
   （不动系统 Python，不污染别的项目，不需要管理员）；
2. 在里面装好 `mcp>=1.0.0,<2` + `frida`；
3. 确认服务器入口真的能导入（不是只看包装上了没）；
4. 把解释器路径发布成服务，再让 MCP 客户端行启动。

> 首次安装要下载 frida 的原生扩展，**约一两分钟**；之后每次启动只做一次
> 导入探测（几百毫秒），不会重复安装。

### 为什么顺序一定是对的

`mcp-client` 那一行 `inject: [gamemodToolchain]`，而 `!!js` 的 config 是在
「该行声明的注入已激活」之后才求值的 —— 所以「先装依赖、再拉服务器」由
**依赖关系**保证，而不是靠 YAML 里的书写顺序（Loader 是并发挂载各行的，
行序不保证顺序）。

### 可选：弱视觉（截图 / OCR）

默认不装（onnxruntime + opencv 加起来几百 MB）。要用就在组合包 config 里写：

```yaml
config:
  vision: true
```

### 不用 DSH 的话

本项目仍然是一个标准的 stdio MCP 服务器，可以接任何 MCP 客户端：

```powershell
python install.py            # 装依赖 + 冒烟测试
python install.py --vision   # 连弱视觉一起装
python install.py --check    # 只做环境自检
```

然后把 `mcp-config/` 里的样例按实际路径填好即可（README.md 有各客户端说明）。

> **为什么安装入口是 Python 而不是 .ps1**：Windows 自带的 PowerShell 5.1
> 读**无 BOM 的 UTF-8** 中文脚本会按 GBK 解码、直接语法报错，而 `pwsh`
> 对方不一定装了。对方装了 Python 才能跑 MCP，所以 Python 一定在。

### 这个包不含依赖工具

包里只有源码 + 组合包层，**不含** `mcp` / `frida` 的 wheel 或任何二进制。
依赖在**安装时**装进那个托管 venv —— 这也是 DSH 组合包的常规形态。

---

## 许可

MIT

