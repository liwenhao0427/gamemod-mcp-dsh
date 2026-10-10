---
name: gamemod-mod
description: 用 GameMod MCP 工具集做游戏 Mod：定位类/字段/方法、计算 Hook 落点、注入运行时探针、捕获调用事件、读写内存。当用户要求"做 Mod"、"改游戏数值"、"Hook 游戏函数"、"找游戏里的类/字段"、"逆向 Unity/Unreal 游戏"、"分析 IL2CPP/Mono 程序集"、"内存修改器"、"游戏辅助工具"、"反编译游戏脚本"时使用本 skill。也适用于用户提到 GameAssembly.dll、global-metadata.dat、il2cpp、Mono、Unreal、Frida、AOB/特征码扫描、字段偏移、方法 RVA 等场景。
---

# 游戏 Mod 开发（GameMod MCP）

> 这份文档同时也是 MCP 资源 **`gamemod://guide`**（同一份内容，逐字节相同）。
> 没装 skill 的客户端可以直接读该资源拿到完整指南 —— 见 README「资源」一节。

## 先判断引擎——它决定你能做什么

**动手前第一步永远是 `inspect_game(game_dir=...)`**，
因为三件引擎的能力边界差别很大，走错路会白折腾很久：

| 引擎 | 能查类型系统？ | 能查字段偏移？ | 主打能力 |
|---|---|---|---|
| **IL2CPP** | ✅ 类/字段/方法/token | ✅ 运行时取 | 精确定位 Hook 落点 |
| **Mono** | ✅ 同上，且类型更精确 | ✅ 运行时取 | 同上 + 可直接反编译出 C# |
| **Unreal** | ❌ | ❌ | **资源容器**（pak/utoc 里有什么） |

> **两个引擎的运行时配方基本一致**：`launch_game` → `runtime_class_info` 取真实偏移
> → `find_hook_target` 取落点 → `hook_method` + `drain_events`。
> Mono 侧探针走的是 `mono_*` API，与 IL2CPP 的 `il2cpp_*` 是两套实现，
> 但对外的工具、返回结构与 token 对账方式**完全一样**。
>
> Mono 上有五条差异要知道（都是如实声明的，不是缺陷）：
>
> 1. **没有 RVA**。方法地址是 `mono_compile_method` 触发 JIT 后的机器码地址，
>    JIT 代码不在 `mono-2.0-*.dll` 模块内，所以 `in_module: false` 且**不给**
>    `rva`/`rva_hex`（给一个假 RVA 比不给更糟）。Hook 用 `pointer` 即可。
> 2. **`gc_control` 在本版 Mono 上不支持**：没有 `mono_gc_disable` / `mono_gc_enable`
>    导出（四个样本一致核实过）。它会明确返回 `ok:false`，不要当成失败重试。
> 3. **程序集是懒加载的**，而且**注入成功不等于可以 Hook**。
>    刚 `launch_game` 完成时只能枚举到十来个程序集（BepInEx 自己那批），
>    `UnityEngine.*` 要过几秒才出现。实测（Archon Soul）在只有 13 个程序集时
>    就去 Hook 并强制调用 `get_frameCount`，**游戏进程直接消失**。
>    所以探针会在注入成功后**等程序集加载稳定再返回**（数量连续 4 次采样不变，
>    实测等了 4.0 秒、看到 144 个），结论在 `init.assemblies` 里。
>    若 `settled` 为 `false`，说明游戏还在加载 —— 此时**先别 Hook**，
>    过几秒再查。判断依据还有返回里的 `searched_images`（实测 13 → 164），
>    它应当随游戏运行变多；始终不变才是真的没有这个类。
> 4. **短名解析只认顶层类型**（与 IL2CPP 一致）：`runtime_class_info(name="Time")`
>    会命中 `UnityEngine.Time`；若某个短名在多个顶层类型里都有，
>    会返回 `ambiguous: true` + 候选，让你自己指定 `namespace`。
> 5. **静态索引只覆盖游戏自己的程序集**。Mono 的索引默认只收
>    `Assembly-CSharp.dll`（实测 1,387 类型），所以 `class_detail("UnityEngine.Component")`
>    会返回「未找到类」并说明原因 —— 这不是工具坏了，是那份 dll 没被索引。
>    `UnityEngine.*` 这类框架类型要用 `runtime_class_info` 在**运行时**查（需先注入）。

下面第二节讲的「元数据优先」**只适用于 IL2CPP 与 Mono**。
如果你确认是 Unreal，直接跳到「引擎差异 → Unreal」与「配方 0」，
不要尝试 `search_classes`（会失败，因为没有类型元数据可查）。

---

## 核心理念：元数据优先，而不是内存扫描

> 适用于 **IL2CPP / Mono**。Unreal 请见上方说明。

这是本工具集与"传统内存修改器（Cheat Engine 式）"最根本的区别，**先理解这一点再动手**：

| | 传统 CE 式做法 | 本工具集做法 |
|---|---|---|
| 怎么找 "金币" | 反复搜索内存值、改值、再筛 | 查元数据拿到 `UserData.gold` 的**字段名与类型**，注入后取它的**真实偏移** |
| 怎么找函数 | AOB 特征码猜机器码 | 查元数据拿方法 **token**，注入后用 token 对账换算出**真实地址** |
| 稳定性 | 游戏一更新就全废 | 元数据变了才失效，且能自动重新解析 |
| 需要猜测吗 | 需要（试错） | 基本不需要（可枚举、可比对） |

**所以：优先用 `search_classes` / `class_detail` 定位，`scan_pattern` 只是符号被剥离时的兜底。**

### 元数据能给出什么、不能给出什么

⚠️ **最容易记反的一点**：静态元数据给出的是**类型系统**，不是内存落点。

| 信息 | 静态元数据 | 注入后的运行时 |
|---|---|---|
| 类 / 字段 / 方法的**存在与签名** | ✅ 有 | ✅ 有 |
| 方法的 **token** | ✅ 有 | ✅ 有 |
| 字段的**字节偏移** | ❌ **没有** | ✅ `runtime_class_info` |
| 方法的 **RVA / 绝对地址** | ❌ **没有** | ✅ `find_hook_target` |

所以路径永远是：**静态拿名字与 token → 注入 → 运行时拿偏移与地址 → 用 token 对账**。

游戏（IL2CPP/Mono）的元数据里保存了**完整的类型系统**：
类型名、字段名、字段类型、方法名、方法签名、方法 token。
运行时再把它们**实体化**成一个真实的内存布局：
- 每个类有真实的 `Il2CppClass` 结构
- 每个字段有真实的字节偏移
- 每个方法有真实的机器码地址

工具做的事就是：**离线解析元数据 → 在线问运行时真实布局 → 两边用 token 对上号**。
token 对上号这一点非常重要，它是"我挂的确实是我想挂的那个方法"的硬证据。

---

## 标准工作流

### 第 0 步：确认环境能做什么

```
get_diagnostics()
```

务必先看这个。它会告诉你：
- Frida 装没装（没有的话**所有运行时工具都不能用**，只能做静态分析）
- Mono / UE 解析器是否就绪
- **`decompiler.available`** —— 反编译能力（可选的外部 `ilspycmd`）是否可用
- **`screen.capture_usable` / `screen.ocr_usable`** —— 弱视觉能力
  （可选：`dxcam`|`mss` 截图、`rapidocr`|`tesseract` OCR）是否可用，
  以及实际会用的引擎名。为 false 时 `capture_screen` / `ocr` 会返回
  安装指引而不是抛异常
- 缓存目录在哪、当前会话状态

> 它**默认不扫本机进程**（那一步实测要 2.7 秒，且与 `find_game_processes` 重复）。
> 要列正在运行的游戏，直接调 `find_game_processes`。

如果 `frida.installed = false`，就**不要**尝试 `inject_process`，直接告诉用户需要装 Frida。

### 第 1 步：找到游戏

```
inspect_game(game_dir="D:\\...")   # 探测安装目录：引擎、主程序、关键文件
find_game_processes()              # 游戏已经在跑时，用它拿 pid
```

`inspect_game` 返回的 `engine` 字段决定后续路线：`il2cpp` / `mono` / `ue` / `unity`。

### 第 2 步：建立索引（静态查询的前提）

> **Unreal 游戏跳过这一步**——UE 没有可索引的类型元数据，
> `open_game` 会报「没有元数据文件」。UE 直接走 `detect_ue` / `ue_list_entries`。

```
open_game(game_dir="D:\\Games\\MyGame")
```

这一步会解析元数据并建 SQLite 缓存。**首次约 2 秒，之后 0.01 秒**（缓存键是元数据文件的 sha256）。
返回里 `index.type_count` 等数字可以用来 sanity check——比如 IL2CPP 游戏通常有一两万个类型。

⚠️ 不要反复调 `open_game`。重复选同一个游戏会复用会话（返回 `reused: true`），不会重建。

### 第 3 步：查询（此步不需要游戏在运行）

```python
search_classes(pattern="UserData")             # 找类
search_classes(subtype_of="UnityEngine.MonoBehaviour")  # 找所有组件
search_classes(only_enums=True, pattern="Currency")     # 找枚举常量表
search_fields(pattern="gold")                  # 直接搜字段名
search_methods(pattern="AddCurrency")          # 直接搜方法名
```

然后看细节：
```python
class_detail("TeamTapas.ProjectR.UserDatas.Service.UserData_Service")
# → fields: [{name, type, type_confidence}], methods: [{name, token, ...}], parent
# 注意 fields 里**没有 offset** —— 偏移要注入后从 runtime_class_info 取
```

**关键**：静态元数据里的字段**没有偏移**（偏移是运行时才定的），方法**只有 token 没有地址**。
要拿到能用的落点，必须进入运行时。

**如果元数据不够用，就去读源码（仅 Mono）**：元数据只能告诉你「有哪些类/字段/方法、签名如何」，
给不出**方法体逻辑**。想知道「这个方法到底怎么算伤害的」，对 Mono 游戏可以直接反编译：

```python
decompile(assembly=r"D:\...\_Data\Managed\Assembly-CSharp.dll", type_name="SpellEffect")
# → source: 67 行真正的 C# 源码；declared_types 是产物里声明的类型

decompile(type_name="FlyingWormConsole3.LiteNetLib.NetManager")   # 留空 assembly 用当前会话的程序集
# → 嵌套类型可传 "Outer.Inner"，会自动退让到**最外层**并在 nested_note 里说明
#   （ilspycmd 无法单独定位嵌套类型，产物始终是最外层那一份；
#    `decompiled_type` 如实报的是**实际产出的**类型名，不是请求名）

decompile(output_dir=r"D:\out\full")     # 只给 output_dir = 反编译整个程序集
# → 落盘 800+ 个文件，只返回 file_count / sample_files，**不内联源码**
```

- **只支持 Mono/.NET**。IL2CPP 的 `GameAssembly.dll` 没有托管元数据，传进去会明确报错。
- 依赖是**可选**的（外部 `ilspycmd`）。先看 `get_diagnostics` 的 `decompiler.available`；
  为 false 时会返回安装指引而不是抛异常。
- 产物大时一定用 `output_dir` 落盘，别指望内联返回。

### 第 4 步：注入运行时

```python
inject_process()                              # 游戏已在运行
launch_game(exe="D:\\...\\Game.exe")          # 还没启动（能抢在初始化前挂钩）
```

`launch_game` 更强：能在对象被创建**之前**装好 Hook，适合拦截只执行一次的初始化逻辑。
但它需要等游戏加载 DLL，返回里的 `init.waited` 会告诉你等了多久。

**会话上下文会自动跟随目标**（`launch_game(exe=...)` 与 `inject_process(pid=...)`
都一样）：目标属于**另一个游戏**时，会话身份（`game_dir` / `engine` /
`metadata_path`）与静态索引会一起切过去，返回里给 `context_switched` 说明换了哪个。
这一步是必须的 —— 否则 `search_classes` 查的是 A 游戏、`runtime_class_info`
拿的是 B 游戏的地址，而 **token 对账这把"尺子"本身就是错的**。
实测过：会话停在 Pass The Fear（索引 140 个 image），`launch_game` 起 ved 之后
`runtime_info` 报 `image_count=143 / static_image_count=140 /
image_count_matches=false`，而全程每个工具都是 `ok: true`，一个错都不报。

所以看到 `context_switched` 时**别当成噪音**：它意味着你之前的静态查询结果
已经不能用了，要重新用 `search_classes` / `class_detail` 定位。
反推不出 exe 属于哪个游戏时（比如它不在游戏目录里）工具**不猜**，
改用 `context_warning` 提示 —— 这时 `runtime_info` 的一致性自检结果**不可信**，
要么显式 `open_game` 换上下文，要么别把静态与运行时的结果对着看。

**同名进程已经在跑时它会直接拒绝**（`ok:false` + `running_pid` + `hint`）：
Unity 游戏普遍带单实例锁，第二个进程**不会**加载 `GameAssembly.dll`，
继续启动只会白等 60 秒再报「IL2CPP 运行时未就绪」—— 那个报错会把你带向
「探针坏了」的错误方向。出路在 `hint` 里：`inject_process(pid=...)` 注入已在跑的
那个，或先 `taskkill` 掉它。**注意**：这种情况下也说明游戏其实**已经开着**了，
先想想是不是该直接注入它。

附带的 `args=` 按 **Windows** 习惯拆：空白分隔、双引号内保留空白、反斜杠**不是**
转义符（所以 `-path "C:\\Games\\My Game\\a.txt"` 里的反斜杠照原样传进去，
含空格的路径**必须**加引号）。

**`init.attempts` 大于 2 是正常的**，不是故障：`GameAssembly.dll` 加载完、
导出能解析，**并不等于** IL2CPP 运行时已经初始化好。域还没建好时探针会明确
返回「IL2CPP 域尚未初始化」并**继续重试**（最多 60 秒），直到域真的可用才附着线程。
早期版本在这里「静默降级」——把附着失败记一笔就继续跑，结果后续每次 il2cpp 调用
都在未附着的线程上执行，**直接把游戏搞崩**（实测 9 个 IL2CPP 游戏里 6 个中招，
Windows 事件日志里是 `GameAssembly.dll` 的 `0x80000003`）。
所以：如果注入返回里带这类错误、且长时间拿不到就绪，**别硬上**——
去 `runtime_info` 看 `image_count`，先确认探针是不是真的活着。

### 第 5 步：拿真实落点

```python
runtime_class_info(name="PresetLoaderV2")
# → fields: [{name, type, offset: 32, is_static}]       ← 真实字段偏移
# → methods: [{name, token, pointer, rva_hex, in_module}] ← 真实方法地址
```

**拿对象实例：用 `find_instances`，别靠 Hook 碰运气。**

`read_field_value` / `set_field_value` 都要一个 `instance=` 指针。
以前唯一的办法是「Hook 一个恰好会收到该对象的函数、从 `args[0]` 捡」——
那是碰运气。现在直接枚举该类的活实例：

```python
find_instances(name="UserData", max_count=20)
# → {ok, class, engine, count: 3,
#    pointers: ["0x1dd...", ...],
#    instances: [{pointer, class_name}, ...],
#    exact_class_count: 3, derived_count: 0}
# 然后拿 pointers[0] 去 read_field_value / set_field_value
```

| 引擎 | 情况 |
|---|---|
| **IL2CPP** | ✅ 可用，实测毫秒级（GameObject / Transform / String 各约 10ms） |
| **Mono** | ⚠️ 官方头文件写明堆遍历「只在 pre-stop-world 回调内有效」，从外部线程调用**不保证**可用。拿不到时它返回 `ok:false` + `unsupported:true` —— 那是「本时机不可用」，**不是**「没有实例」，别据此下结论 |

四条注意：
- **值类型（struct）没有堆实例**，会直接报错说明，不是返回空列表。
- **⚠️ 返回的实例包含派生类**：实测 `find_instances("Transform",
  namespace="UnityEngine")` 拿回的**全是 `RectTransform`**（Unity 里 UI 节点
  都是它，而它是 Transform 的派生类）。**用基类的字段偏移去读派生类实例
  可能出错** —— 按返回里的 `instances[].class_name` 甄别。
  `exact_class_count` / `derived_count` 能一眼看出混没混。
- `overflow:true` 表示实例数超过 `max_count`，**还有更多**。
- 拿到的指针**只在游戏继续运行时有效**，对象可能被 GC 回收或随场景切换失效 ——
  **立即使用**，不要跨步骤长期保存。

**类名怎么传**（`runtime_class_info` / `find_hook_target` / `read_field_value` 通用）：

| 你要找的 | 怎么传 | 结果 |
|---|---|---|
| 带命名空间的顶层类 | `name="Transform"`（namespace 可省） | 按**顶层**类型短名解析，唯一就采用，多个则 `ambiguous:true` + `candidates` |
| 同上，想精确 | `name="Transform", namespace="UnityEngine"`，或 `name="UnityEngine.Transform"` | 同一个类，三条路返回的 `full_name` 与字段偏移完全一致 |
| **嵌套类型**（状态机 `State`、协程体 `<Xxx>d__4`、私有内部类） | 必须给**完整嵌套名**（`search_classes` 的 `nested_name`，如 `ChapterE3_FinalBossCorridor.<ProcessSequence>d__4`） | 只用短名会被拒绝：嵌套类型在静态索引里 namespace 为空，拿短名兜会撞上一堆同名类 |

**这是整个流程的核心产出**。到这里你就有：
- 字段的字节偏移 → 可以用 `read_memory` / `read_field_value` 读写数值
- 方法的绝对地址 → 可以用 `hook_address` 挂钩

**读与写是对称的两个入口**（`read_field_value` / `set_field_value`）：
两者都要「哪个对象的哪个字段」，都按**字段类型**决定宽度，都不需要你自己算偏移。
区别只在 `write_memory` 是**原始字节**入口（要地址 + 十六进制串），
上面两个是**字段**入口。改数值优先用 `set_field_value`：

```python
set_field_value(instance="0x...", name="UserData", field="gold", value=9999)
# → {ok, offset, field_address, write_width, write_kind,
#    value_before{as_int32}, read_back{as_int32}, verified: true}
```

它替你挡掉三类常见错误：

| 情况 | 行为 |
|---|---|
| 值超出字段宽度（如 300 写进 1 字节） | **报错，不静默截断**（截成 44 会让你以为成功了） |
| 字段类型认不出、宽度 8（多半是引用类型） | **拒绝**：换对象引用属于改逻辑，该用 `eval_script` / Hook |
| 静态字段 | **拒绝**：那是 `il2cpp_field_static_set_value` 的事 |

`verify=True`（默认）会在写完后**回读一次**并把实测值放进 `read_back`；
回读与写入不一致时**报 `ok:false`**，不会给你一个「看着成功」的结果。

### 第 6 步：Hook 并观察

```python
hook_method(name="Time", method="get_frameCount", namespace="UnityEngine",
            args_type="", label="frame", max_calls=500)
hook_status()          # 看触发次数
drain_events()         # 取走捕获到的调用（取走即消费）
unhook()               # 收尾务必卸载
```

### 第 7 步：收尾

```python
close_session()        # 卸载 Hook + 断开注入 + 关索引
```

**务必收尾**。卸载 Hook 后游戏恢复正常行为；注入状态下游戏会一直带着探针运行。

> `close_session` **只有真的恢复原状才返回 `ok=true`**。卸载 Hook / 卸载脚本 /
> detach 任一步失败都会 `ok=false` + `error`，并保留会话让你能重试 ——
> 这时游戏可能仍带着探针，**不要**直接宣称收尾完成，先按 `error` 排查再重试。

---

## 各引擎的差异

### IL2CPP（Unity 的 AOT 后端，最常见）

- 元数据在 `<游戏>_Data/il2cpp_data/Metadata/global-metadata.dat`
- 逻辑在 `GameAssembly.dll`
- **工具支持最完整**：类型/字段/方法/token 全部可查，运行时偏移可读
- 游戏逻辑通常在 `Assembly-CSharp.dll` 对应的映像里
- 搜索时按 `assembly` 参数过滤能大幅缩小范围

### Mono（Unity 的 JIT 后端，老游戏或开发版）

- 程序集在 `<游戏>_Data/Managed/*.dll`，是标准 .NET DLL
- 主逻辑在 `Assembly-CSharp.dll`；**静态索引默认只收这一份**，
  `UnityEngine.*` 等框架类型静态查不到（要用 `runtime_class_info` 运行时查）
- 运行时里 `mono-2.0-*.dll` 是标志
- 可以直接用 `decompile` 工具读源码（薄封装 ILSpy）——**比 IL2CPP 好搞得多**
  ```python
  decompile(type_name="SpellEffect")   # 单个类型，源码内联在 source 里
  ```
  反编译是 Mono 独有的能力：IL2CPP 没有托管元数据，必须先造 DummyDll（本工具集暂未实现）。
- 本工具集在 Mono 上**静态与运行时都能用**（运行时走 `mono_*` 那套探针）。
  配方与 IL2CPP 一样：注入 → `runtime_class_info` → `find_hook_target` →
  `hook_method` + `drain_events`，静态 token 与运行时 token 照样对账。
  五条差异（无 RVA / `gc_control` 不支持 / 程序集懒加载**且注入后会自动等它稳定** /
  短名只认顶层 / 静态索引不含框架类型）
  见本文开头「先判断引擎」一节，都是如实声明，不是「没实现」。

### Unreal

- 特征是 `*-Win64-Shipping.exe`、`Engine/` 目录、`Content/Paks/*.pak`
- **没有类型系统可查**：UE 没有 IL2CPP 那样的集中元数据文件。
  FName 池、UObject 属性偏移、`.usmap` 都拿不到（需运行时）。
  所以**不要**在 UE 游戏上期待 `search_classes` / `search_fields` 能用。
- UE 的强项是**资源容器**，用这几个工具：
  - `detect_ue(exe=...)` —— 确认是不是 UE，看证据与置信度
  - `ue_profile(exe=...)` —— 概况：几个 pak/utoc、各多少条目、脚本模块
  - `ue_list_entries(container="....pak")` —— **列出打包了哪些资产**（最常用）
  - `ue_script_objects(exe=...)` —— exe 里的 `/Script/` 模块（字符串证据）
  - `ue_manifest(game_dir=...)` —— 打包清单（校验容器解析的真值）
- 典型用途：找本地化文本（`.locres`）、找配置、确认某功能是否被裁剪进包。
- **版本号拿不到是正常的**：工具会返回空串 + 原因，不要当成 bug。

---

## 常用配方

### 配方 0：Unreal 游戏——看它打包了什么

```python
detect_ue()                                     # 1. 确认是 UE（看 confidence 与 evidence）
ue_profile()                                    # 2. 有几个容器、各多少条目
# 3. 从 profile 的 detection.layout.utoc_files 里挑一个容器
ue_list_entries(container="....utoc", pattern=".locres")   # 4. 找本地化文本
ue_list_entries(container="....utoc", limit=50, offset=0)  # 5. 翻页看全貌
# 想知道某个资源在不在包里：
ue_list_entries(container="....utoc", pattern="AbilitySystem")
```

**要点**：
- 条目总数可能上万（实测某样本 21,439 条），**必须用 `pattern` 过滤或翻页**，
  不要一次拉全量。
- 过滤没命中时返回 `matched=0` **并附带说明**——那是"没搜到"，
  不是"包里没有"，换个关键词再试。
- 拿到的路径是**大小写敏感的原样**；和打包清单比对时要不敏感地比
  （实测清单会规范化大小写，敏感比对会少几十条）。

### 配方 1：改一个数值（比如金币）

```python
open_game(game_dir=...)
search_fields(pattern="gold")                   # 1. 找字段，看它属于哪个类
# 假设结果指向 UserData.gold (System.Int32)
class_detail("UserData")                        # 2. 确认字段与类型
inject_process()                                # 3. 注入
runtime_class_info(name="UserData")             # 4. 拿 gold 的真实偏移
# 5. 要改值得先拿到实例指针 —— 通过 Hook 一个会经过该对象的函数，
#    从 args[0] 拿到 this 指针
hook_method(name="UserData", method="AddGold", args_type="ptr,int")
drain_events()                                  # → events[0].args[0] 就是实例指针
read_field_value(instance="0x...", name="UserData", field="gold")
# 6. 确认读出来的值符合预期后再写（写错地址会立刻崩游戏）
```

**注意**：改数值是"当下的"修改，游戏重新加载存档可能覆盖。
要做持久修改，应该 Hook 住读取/写入该值的方法，在 Hook 里改逻辑。

### 配方 2：拦截一个函数、修改它的返回值

```python
find_hook_target(name="UserData_Service", method="get_storeStatSync")
# 记下返回的 token，去静态那边比对，确认是同一个方法
hook_method(name="UserData_Service", method="get_storeStatSync",
            namespace="TeamTapas.ProjectR.UserDatas.Service",
            args_type="")
drain_events()          # 看 retval
# 要改返回值需要在下发 JS 里操作 —— 用 eval_script 装一个自定义 Hook
eval_script("""
(function(){
  var target = ptr('0x...');            // 上一步拿到的 pointer
  Interceptor.attach(target, {
    onLeave: function(retval) { retval.replace(ptr(9999)); }
  });
  return 'hooked';
})()
""")
```

### 配方 3：找所有每帧执行的更新逻辑

```python
# 找游戏自己写的 MonoBehaviour 脚本（assembly 过滤掉 UnityEngine 等框架程序集）
find_derived_classes(base_full_name="UnityEngine.MonoBehaviour",
                     assembly="Assembly-CSharp", limit=500)
# 再看具体某个类有没有 Update
class_detail(full_name="PlayerController", method_name="Update")
search_methods(pattern="Update", owner="PlayerController")
```

> **类名有歧义时工具会拒绝，不会替你猜。**
> 同一个短名可能对应多个类（不同程序集各有同名类，实测 IL2CPP 样本
> 20,072 个类型里有 585 个短名是这种情况）。这时返回
> `ok: false` + `candidates`（含各自程序集），补 `assembly=` 消歧即可：
> `class_detail(full_name="Player", assembly="Assembly-CSharp")`。
> 返回里的 `hint` 是**验证过能用**的写法（通常是一条候选的限定名），
> 照抄即可；若候选全在同一个程序集里，错误信息会明说 `assembly=` 帮不上忙。

> **想逐层看继承树用 `class_hierarchy(direction="down")`。**
> 它给的是**直接**继承关系：每条的 `parent` 是它真实的直接父类，
> `depth` 是它所在的那层。`find_derived_classes` 则是**传递式**的
> （含间接继承，适合一次捞全所有脚本），两者用途不同，别混用。
> 某一层超过 500 条时 `down_has_more=true`，这时改用 `find_derived_classes` 拿全量。
> 这样做是因为「拿错类」比「报错」危险得多 —— 拿到的是**另一个类的整份字段表**，
> 据此算出的偏移必然错，而且要到运行时才暴露。

> **嵌套类型要用完整名查，短名查不到或会报歧义。**
> IL2CPP 的 `namespace` 只属于**最外层**类型，嵌套类型（状态机 `State`、
> 协程体 `<Xxx>d__0`、私有内部类）的命名空间是空的、只有短名。
> 实测 IL2CPP 样本 20,072 个类型里 **7,056 个（35%）是嵌套类型**，
> 名为 `State` 的类型有 131 个而**只有 1 个是顶层**。
> 所以：查嵌套类型请把 `name` 传成完整嵌套名
> （如 `name="TeamTapas.ProjectR.UI2.RUI_FloatingUI.State"`），
> 这个写法从 `search_classes` 返回的 `nested_name` 直接抄即可。
> 只传短名而命中多个时，`runtime_class_info` / `find_hook_target` 会返回
> `ok: false` + `ambiguous: true` + `candidates`，**不会**替你取第一个。

### 配方 4：确认 Hook 落点是对的（重要）

**永远做这一步**，否则你可能挂到了同名的另一个重载上：

```python
static = class_detail("PresetLoaderV2")          # 静态：token = 0x06000036
runtime = find_hook_target(name="PresetLoaderV2", method="Update")
# 比对：runtime.candidates[0].token 必须等于 static.methods 里那条的 token
```

工具的 `hook_method` 也会在返回里带 `chosen.token`，同样要核对。

### 配方 5：处理重载方法

同名不同参数的方法有不同 token。`find_hook_target` 返回的 `candidates` 里
每项都有 `param_count`，**先按参数个数认准你要的那个**：

```python
tgt = find_hook_target(name="BodyPart", method="Visualize")
for c in tgt["candidates"]:
    print(c["token"], c["param_count"], c["rva_hex"])
```

`hook_method` **支持指定重载**，两种方式：

```python
# 首选：按 token 精确指定（静态元数据里就有，最可靠）
static = class_detail(full_name="BodyPart", method_name="Visualize")  # 拿到 token
hook_method(name="BodyPart", method="Visualize", token="0x06000484")

# 次选：按参数个数（仅当各重载参数个数互不相同时可用）
hook_method(name="BodyPart", method="Visualize", param_count=1)
```

> ⚠️ 若目标方法有**多个重载而你没给 `token` / `param_count`**，
> `hook_method` 会**拒绝挂载**并返回候选清单（`ok: false`）——
> 这是故意的：盲选第一个候选会让 Hook 成功却挂错方法，
> 错误要到很久以后才以「数据不对」的形式暴露，极难排查。
> 只有候选唯一时才允许省略。
>
> 挂完**务必**核对返回里的 `chosen.token` 与静态查到的是否一致。

### 配方 6：语义化点击 UI（不靠屏幕坐标）

与配方 7 的 OCR 坐标点击相比，这条路**更稳**（不受分辨率/缩放/遮挡影响），
但要求你能在元数据里定位到那个 UI 组件。

```python
# 思路：拿 Button 组件的 onClick 委托并 Invoke。
# 先按类找实例（find_instances），再调它的方法。
find_instances(name="Button", namespace="UnityEngine.UI", max_count=20)
# → 每个实例带 class_name；挑出你真正想要的那个（注意派生类混入）

# 然后用 eval_script 调它。IL2CPP 的泛型方法需要先 inflate：
eval_script("""
(function(){
  var k = ptr('0x...');            // 上一步拿到的 Button 实例
  // Button.onClick 是个属性，取 get_onClick 再 Invoke
  var mi = funcs.il2cpp_class_get_method_from_name(
      funcs.il2cpp_object_get_class(k), 'get_onClick', 0);
  if (!mi || mi.isNull()) { return 'no get_onClick'; }
  var ev = funcs.il2cpp_runtime_invoke(mi, k, NULL, NULL);
  if (!ev || ev.isNull()) { return 'invoke returned null'; }
  // ev 是 ButtonClickedEvent（UnityEvent），再 Invoke 它
  var im = funcs.il2cpp_class_get_method_from_name(
      funcs.il2cpp_object_get_class(ev), 'Invoke', 0);
  if (!im || im.isNull()) { return 'no Invoke'; }
  funcs.il2cpp_runtime_invoke(im, ev, NULL, NULL);
  return 'clicked';
})()
""")
```

**三条注意**：

| 注意 | 说明 |
|---|---|
| **先核对实例的 `class_name`** | `find_instances` 会返回**派生类**实例（实测 `Transform` 拿回的全是 `RectTransform`）。用错类的字段偏移会出错 |
| **`onClick.Invoke()` 会绕过事件管线** | 它跳过 `Selectable` 状态机、`interactable` 校验与同级监听者。最省事，但**验收会失真** —— 想更接近真实点击，用 `ExecuteEvents.Execute` 派发 `PointerClick` |
| **泛型方法要先 inflate** | `ExecuteEvents.Execute<T>` 在 IL2CPP 下是泛型方法，需要先拿到具体实例化版本，直接按名字取会拿到未实例化的 MethodInfo |

> 更符合直觉但更复杂的做法是 `ExecuteEvents.Execute` + `PointerEventData`：
> 需要目标 GameObject、一个用 `EventSystem.current` 构造的 `PointerEventData`，
> 且场景里要有 `EventSystem`。用它之前先确认这三样都在。

### 配方 7：验收 Mod —— 等到可以判、再判、留证据

Mod 改完之后要**自动验收**。验收的关键**不是**「看一眼截图」，而是三步：

```
等到可以判  →  判  →  留下可证伪的证据
```

**第一步：等 —— 游戏是异步的，不等就会得到大量假失败。**

```python
wait_for(kind="ocr", target="开始游戏", region="0,600,1280,200",
         timeout_ms=20000, interval_ms=500)
# → {ok, verdict: "pass"|"fail"|"unverifiable",
#    polls: 12, elapsed_ms: 6100,
#    evidence: [{poll, matched_count, ...}, ...],   ← 每次轮询的实测值
#    last: {...}}
```

五种条件，**优先用引擎信号，少用像素**：

| `kind` | 判据 | 参数 |
|---|---|---|
| `field` | 某字段值 == `equals` | `name`/`field`/`equals` |
| `instances` | 某类活实例数 ≥ `min_count` | `name` |
| `log` | 日志里**新增**匹配 `pattern` 的行 | `target`=日志路径，`pattern` |
| `ocr` | 屏幕 OCR 到 `target` | `target`（可选 `region`/`window`） |
| `window` | 出现标题含 `target` 的窗口 | `target` |

> **`kind="log"` 等的是「新增」，不是「存在」**（真机实战踩出来的）：
> 第一次轮询记下基线匹配数，之后**超过**基线才算成立。
> 否则日志里早就有的旧行会让它**秒过** —— 实测某日志已有 5 条
> 「已修改 精核」时，`wait_for(kind="log", pattern="已修改 精核")`
> 在 **1ms** 内返回 `pass`，而本次操作**根本没发生**（假阳性）。
> 要判「当前是否已有」请直接用 `probe_log`。

**`verdict` 是三态，别当成布尔**：

| 值 | 含义 | 该怎么办 |
|---|---|---|
| `pass` | 条件成立 | 继续 |
| `fail` | **判了**，条件没成立（含超时） | 看 `evidence` 里每次的实测值；**不要**直接断定 Mod 无效 |
| `unverifiable` | **没法判**（例如引擎侧不支持枚举实例） | 这是「尺子坏了」而不是「量出来是坏的」。换一条判据或换个时机，**别去修一个不存在的缺陷** |

**第二步：判 —— 证据要能证伪。用 `verify_state` 要求「≥2 条独立证据」。**

> ⚠️ **单条证据不得判通过。** 这是从同类项目（`reverse-skill`）的证据链
> 契约学来的硬规矩，而且我们自己的实战立刻印证了它：
> 在 CatapultHero 验收 Mod 时，**日志**（`已修改 精核 = 108`）与
> **屏幕 OCR**（`精核：108`）两个**互相独立**的来源都指向同一结论，
> 才敢说「Mod 生效了」。
> 只看日志是不够的 —— **日志是 Mod 自己打的，Mod 写错了照样打日志**。

```python
# 双证据验收：日志 + 屏幕，两条独立来源
verify_state(checks=r"log=D:\Game\BepInEx\LogOutput.log#已修改 精核,"
                    r"ocr=精核")
# → {verdict: "pass"|"fail"|"unverifiable",
#    verdict_reason: "2/2 条成立，达到要求的 2 条独立证据",
#    evidence: [{check, hit, detail}, ...],     ← 每条各自的依据
#    independent_count: 2, require_independent: 2}
```

`checks` 的写法（与 `wait_for` **共用同一套条件语义**，所以两处结论必然一致）：

| 写法 | 含义 |
|---|---|
| `log=<路径>#<正则>` | 日志里有匹配行 |
| `ocr=<期望文本>` | 屏幕上 OCR 到该文本 |
| `window=<标题子串>` | 存在该窗口 |
| `field=<类>.<字段>=<期望值>` | 该字段当前等于期望值 |
| `instances=<类>>=<最小个数>` | 该类活实例数达到下限 |

**三态与「证据不足」**（这是它最有价值的地方）：

| `verdict` | 含义 | 该怎么办 |
|---|---|---|
| `pass` | 成立条数 ≥ `require_independent`（默认 2） | 继续 |
| `fail` | 判了、没达到（含**只有 1 条成立**） | 看 `evidence`，补一条**独立**来源 |
| `unverifiable` | **没法判**（如引擎不支持枚举实例） | 换判据，**别去改 Mod** |

**单条证据**时它会明确报 `证据不足，不判通过` —— 这不是缺陷，是设计。

用 `wait_for` 做**等待**（条件型、会重试）：

```python
# 等日志**新增**一行（注意：等的是新增，不是「当前存在」）
wait_for(kind="log", target=r"D:\Game\BepInEx\LogOutput.log",
         pattern="ModPanel.*ready", timeout_ms=15000)

# 或用引擎信号确认状态（比 OCR 稳，不受分辨率影响）
wait_for(kind="field", name="UserData", field="gold", equals="9999")
wait_for(kind="instances", name="ModPanel", min_count=1)
```

> **`wait_for` 与 `verify_state` 的分工**：前者是「等到条件成立」（会轮询、
> 等的是**变化**）；后者是「当前状态到底成不成立」（单次求值、要求**多条
> 独立证据**）。两者共用同一套条件语义，结论不会互相矛盾。

**第三步：留证据 —— 失败时必须能复盘。**

```python
# OCR 没匹配到就自动把当时的截图存下来（等价 MaaFramework 的 save_on_error）
ocr(region="0,600,1280,200", expected="开始游戏",
    save_on_failure=r"D:\evidence\start_button_missing.png")
```

**三条铁律**（都是同类项目踩过的坑）：

| 规矩 | 为什么 |
|---|---|
| **截图不是判据** | `capture_screen` 只用来**留证据**。判断「在哪个界面」用 `ocr` 的文本。`podium-mcp` 源码原话：*Screenshots are NEVER an oracle.* |
| **`matched_count == 0` ≠ 不存在** | 那只是**本次没找到**：界面可能还没加载、或文字被 OCR 拆成多个框。别据此断定按钮没有 |
| **坐标是屏幕绝对坐标** | 返回里 `coordinate_space: "screen_absolute"`。拿裁剪图坐标去点会**点偏**（MaaMCP 专门写进坑） |

**用日志确认结果**（最可靠的验收通道之一）：

```python
probe_log(path=r"D:\Game\BepInEx\LogOutput.log", pattern="ModPanel|ERROR")
# → {ok, matched_count, lines: [{line_no, text}], encoding, has_more}
```

`probe_log` **只读、不消费** —— 同一次读取可以反复做，所以适合轮询等某个
日志行出现（不像 `drain_events` 那样取走即没）。
编码自动探测（utf-8 → gbk），中文游戏日志常见 GBK。

**判断「加载完了没有」不要只看单一信号**：

| 信号 | 注意 |
|---|---|
| `SceneManager.sceneLoaded` | 触发在 `Start` **之前**，单独用它当「可验收」会**过早** |
| `Scene.isLoaded` | 官方语义是「加载完成且对象已 enable」，可轮询 |
| `Application.isLoadingLevel` | **已废弃**，不要用 |
| 帧计数连续 N 帧不变 | 「画面稳定」的近似，不需要截图 |

**点击怎么做**：本工具集**不提供点击工具**（那样会与 `ocr` 的定位职责重叠）。
所有输入都走配方 8 的 `eval_script` 路线 —— 下面是**本机实测跑通**的完整配方。

### 配方 8：模拟按键 / 点击（实测跑通的完整链路）

用户场景里最关键的一步：「按热键唤起我们自己的 Mod 面板」。
本工具集没有 `send_key` 这类工具（有意的，见下），但 `eval_script`
能在**游戏进程内**调 `user32!SendInput` —— 这条路**本机实测跑通了**。

**⚠️ 四个实测踩到的坑，缺一个都发不出键：**

| 坑 | 现象 | 正确做法 |
|---|---|---|
| **前台锁** | `SetForegroundWindow` 返回 **0**，键发到了别的窗口（实测发给了浏览器） | 先 `AttachThreadInput` 附加到当前前台线程，再 `SetForegroundWindow`（见下表实测结果） |
| Frida 17 的 API 变了 | `Module.getExportByName` 报 **"not a function"** | 先 `Process.getModuleByName('user32.dll')` 再 `findExportByName` |
| `eval_script` 的 `//` 注释 | 报 `unexpected token`（它先按**表达式**求值，包一层 `return (...)`，换行注释会吃掉后面的语句） | JS 里只用 `/* */` 注释 |
| `SendInput` 只看**前台**窗口 | 「投递成功」（返回 1）**不等于**游戏收到了 —— 前台窗口不对时，键进了别的程序 | 发键前**必须核对前台窗口**，发完再核对一次 |

**实测数据（PathOfIdle，本机）**：

```
直接 SetForegroundWindow        → returned=0, fg_is_game=false   ← 失败，键发去了浏览器
SetForegroundWindow + AttachThreadInput → returned=1, fg_is_game=true  ← 成功
SendInput（游戏在前台时）        → sent=1
```

**完整配方（先抢前台，再发键）**：

```python
# 第一步：把游戏窗口抢到前台。必须走 AttachThreadInput，
# 否则 SetForegroundWindow 会被 Windows 前台锁挡掉（返回 0 且不生效）。
eval_script("""
(function(){
  var u = Process.getModuleByName('user32.dll');
  var k = Process.getModuleByName('kernel32.dll');
  var E = function(m, n) { return m.findExportByName(n); };
  var EnumWindows = new NativeFunction(E(u,'EnumWindows'), 'int', ['pointer','pointer']);
  var GetWindowTextW = new NativeFunction(E(u,'GetWindowTextW'), 'int', ['pointer','pointer','int']);
  var IsWindowVisible = new NativeFunction(E(u,'IsWindowVisible'), 'int', ['pointer']);
  var GetWindowThreadProcessId = new NativeFunction(E(u,'GetWindowThreadProcessId'), 'uint', ['pointer','pointer']);
  var GetForegroundWindow = new NativeFunction(E(u,'GetForegroundWindow'), 'pointer', []);
  var SetForegroundWindow = new NativeFunction(E(u,'SetForegroundWindow'), 'int', ['pointer']);
  var ShowWindow = new NativeFunction(E(u,'ShowWindow'), 'int', ['pointer','int']);
  var BringWindowToTop = new NativeFunction(E(u,'BringWindowToTop'), 'int', ['pointer']);
  var AttachThreadInput = new NativeFunction(E(u,'AttachThreadInput'), 'int', ['uint','uint','int']);
  var GetCurrentThreadId = new NativeFunction(E(k,'GetCurrentThreadId'), 'uint', []);

  var mypid = Process.id, found = null;
  var cb = new NativeCallback(function(h){
    if (!IsWindowVisible(h)) { return 1; }
    var pb = Memory.alloc(8);
    GetWindowThreadProcessId(h, pb);
    if (pb.readU32() !== mypid) { return 1; }
    var b = Memory.alloc(512); GetWindowTextW(h, b, 240);
    var t = b.readUtf16String() || '';
    if (t.length > 0) { found = {hwnd:h, title:t}; return 0; }
    return 1;
  }, 'int', ['pointer','pointer']);
  EnumWindows(cb, NULL);
  if (!found) { return {ok:false, why:'no window'}; }

  var fgThread = GetWindowThreadProcessId(GetForegroundWindow(), NULL);
  var myThread = GetCurrentThreadId();
  AttachThreadInput(myThread, fgThread, 1);   /* 附加 */
  ShowWindow(found.hwnd, 9);                  /* SW_RESTORE */
  var okSet = SetForegroundWindow(found.hwnd);
  BringWindowToTop(found.hwnd);
  AttachThreadInput(myThread, fgThread, 0);   /* 摘掉，别留副作用 */

  var fg = GetForegroundWindow();
  return { ok:true, set_ok:okSet, fg_is_game:fg.equals(found.hwnd), title:found.title };
})()
""")
# 返回 fg_is_game=false 就**不要**继续发键 —— 键会进别的程序。

# 第二步：发键。VK 码见下。
eval_script("""
(function(){
  var u = Process.getModuleByName('user32.dll');
  var sendInput = new NativeFunction(u.findExportByName('SendInput'),
      'uint', ['uint','pointer','int']);
  var inp = Memory.alloc(64);
  inp.writeU32(1);              /* INPUT_KEYBOARD */
  inp.add(8).writeU16(0x70);    /* VK_F1 */
  var sent = sendInput(1, inp, 40);
  /* 松键也要发，否则游戏可能认为一直按着 */
  inp.add(12).writeU32(2);      /* KEYEVENTF_KEYUP */
  sendInput(1, inp, 40);
  return { sent: sent };
})()
""")
```

**为什么把它写成配方而不是工具**：这是你有意选择的路线。
理由也成立 —— 输入模拟与「定位」是两个不同的关注点，
`ocr` 负责「点哪里」，`eval_script` 负责「怎么点」，
硬造一个 `click(x,y)` 工具会让职责重叠，还要处理缩放的换算。

> ⚠️ **反作弊提醒**：`SendInput` 是**系统级合成输入**，
> 带 EAC / BattlEye 的网游会检测并可能封号。本配方只适用于
> **单机 / 离线**场景。工具集本身不做任何隐藏（探针注入同样会被检测）。

**常用 VK 码**：`F1`=0x70 … `F12`=0x7B、`Enter`=0x0D、`Esc`=0x1B、
`Space`=0x20、`Tab`=0x09、字母/数字用其 ASCII 大写值（`A`=0x41、`0`=0x30）。

**要点击屏幕坐标**（而不是发键）？把上面的 `INPUT_KEYBOARD` 换成
`MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_LEFTDOWN/UP`，坐标要归一到
0~65535（`x*65535/屏幕宽`）。⚠️ 这一步**没有**在本机实测过
（只实测了键盘路径），用之前先自己验一次。


### 配方 9：大结果集翻页

```python
r = search_classes(pattern="Player", limit=50)
r["total"]        # 总共 697 个（**匹配总数**，不受 limit 影响）
r["handle"]       # "il2cpp-g3-classes-1"（引擎-g代次-种类-序号）
read_query_result(r["handle"], offset=500, limit=50)      # 翻页
read_query_result(r["handle"], pattern="Controller")      # 在结果内再过滤
```

> 翻页与「结果内再过滤」是**同一个工具**的两种用法：`read_query_result`
> 的 `pattern` 留空就是纯翻页（两者原先各是一个工具，按契约第 3 条已合并）。

> **句柄只属于它被创建时的那一个会话**（前缀里的 `g3` 就是会话代次）。
> 切游戏或重建索引后旧句柄一律失效 —— 这是刻意的：句柄表是每个索引
> 实例各自的、序号会从 0 重来，若不带代次，旧句柄会**静默读到另一个游戏
> 的结果集**（实测过）。失效时返回里会给出 `available_handles`，从里面取。

**`total` 与 `returned` 不是一回事，`has_more` 才是截断标志**：

| 字段 | 含义 |
|---|---|
| `total` | 匹配**总数**（不受 limit 影响） |
| `returned` | 本次实际返回条数 |
| `has_more` | `True` = 被 limit 截断，还有没返回的 |

所以看到 `total=644 / returned=200 / has_more=true` 时，
意思是「一共 644 个，这次只给了 200 个」，**不要**把 200 当成总数。
`find_derived_classes` 也遵守同一约定。

> **还有一个你没法用 `limit` 控制的截断：256 KB 的响应体积上限。**
> 任何一次返回都不超过 256 KB（外层统一兜底，不是各工具自觉）。
> 超限时返回里会多出 `capped: true`，并且：
>
> - `items` 只给前 N 条，**`has_more` 一定是 `true`**、`returned` 是**实际**条数；
> - `note` 直接告诉你续读命令，照抄即可：
>   `read_query_result(handle='il2cpp-g3-classes-1', offset=612)`
>   —— 注意 `offset` 是「原 offset + 已回条数」，**照 note 抄，别自己算**；
> - **一条数据都不会丢**：完整列表就在那个句柄里。
>
> 所以「我要 2000 条」可能只回 600 条，这不是工具坏了，也不是没有那么多 ——
> 接着翻页就行。自己主动用 `pattern` / `limit` 收窄，比事后翻页更省事。

---

## 坑与注意事项

### 假成功是最危险的

工具返回**必定**带 `ok` 字段：
- `ok: true` = 真的成功了
- `ok: false` + `error` = 失败了，看 `error` 说明

**不要**把 `ok: false` 当成"没数据"就往下走。失败时工具不会返回半真半假的数据。

### 卡住了怎么办 —— 量化的停止条件（别死磕）

本工具集有几处**容易让人反复重试**的场景。它们的共同点是：
**重试不会让情况变好，只会浪费时间并让你误判**。
所以下面是硬性停止条件，不是建议：

| 情况 | 停止条件 | 该改做什么 |
|---|---|---|
| `wedged: true`（探针卡死） | **一次都不重试** | 后续调用会立刻失败。`taskkill` 结束游戏 → 重新 `inject_process`。**不要**反复调同一工具等它恢复 |
| `drain_events(kind=...)` 返回空 | 连续 **2 次**为空就换判据 | 先看不带 `kind` 的 `drain_events()` 看实际事件类型（`pending_approx > 0` 说明队列里有东西）。空**不等于** Hook 没触发 |
| `wait_for` 返回 `fail`（超时） | **1 次**超时后先看 `evidence` | `evidence` 里每次轮询的实测值能区分「界面没加载完」与「条件本身没满足」。**不要**直接再调一次同一个 `wait_for` |
| 同一工具 + **同样参数** | 调用 **≥2 次**必须换方法 | 同参数重试两次以上，第三次结果不会不同。换 `kind`、换引擎路线（静态↔运行时）、换工具 |
| 同一方法失败 **2–3 次** | 必须换路径 | 例如 `search_classes` 搜不到就换 `search_fields` / `list_images` 缩小范围 |
| `hook_method` 报「多候选」 | **不要**靠猜重试 | 用 `token=`（从 `class_detail` 拿）或 `param_count=` 精确指定 |
| `open_game` 报「识别不出引擎」 | **1 次**即停 | 看返回里的 `engine_family.label`；不支持的引擎（Godot/GameMaker 等）换不了，如实告诉用户 |

**每 5 次工具调用，或感觉"卡住了"时，停下来自问三句**：

1. 我**真的**在向目标前进吗？**举出具体证据**（不是"应该快了"）。
2. 我有没有用**同样的参数**调同一个工具 ≥2 次？有 → 必须换方法。
3. 我能**说清**上一条报错的意思吗？说不清 → 先搞清楚它，再动手。

> **同一方法失败 2–3 次就换路径**；**同一命令重复 ≥3 次必须停下来评估**。
> 这三条来自同类项目（`reverse-skill`）实战总结的「防漂移」纪律，
> 对我们的场景同样成立 —— 探针卡死、事件空、条件超时都属于
> 「再试一次也不会变」的类型。

### 事件是"取走即消费"

`drain_events()` 取走事件后就清空了。想持续统计就循环调用并累加：
```python
total = 0
for _ in range(10):
    time.sleep(1)
    ev = drain_events(limit=500)
    total += len([e for e in ev["events"] if e["phase"] == "enter"])
```

**`kind` 过滤的是整条队列**，不是只看队首。这条值得记住，因为它曾经坏过：
注入后队列里恒有一条 `notice`（`probe_ready`），旧实现一见队首不是要的类型就
`break`，于是 `drain_events(kind="hook")` 返回 **0 条**（而实际待取 201 条），
调用方会误判成"Hook 没触发"、跑去改 token 换方法 —— 而落点完全正确。

所以：**`drain_events(kind=...)` 返回空，不等于没有该类型事件。**
返回里的 `pending_approx` 若明显大于 0，就说明队列里还有东西；
不放心时用不带 `kind` 的 `drain_events()` 看一眼实际类型（`hook` /
`notice` / `log` / `script_error`），再决定过滤条件。

### 高频方法会刷爆事件队列

Hook `Update`、`get_frameCount` 这类每帧调用的方法时，务必设 `max_calls`。
事件队列满了会丢最旧的（返回里的 `dropped` 会告诉你丢了多少）。

### 参数类型要写对，否则可能崩游戏

`args_type` 里的类型必须与实际签名一致。不确定时：
- 先不传 `args_type`（只观察调用次数和来源）
- 再用 `class_detail` 看方法的参数类型
- 类型表：`int`/`uint`/`long`/`ulong`/`bool`/`float`/`double`/`ptr`/`string`/`cstring`
- 写错的规格名会**直接报错**（不会静默按指针读出一个错值）

**实例方法的第一个整型位置就是 `this`**：想拿对象指针（例如喂给
`read_field_value`）就写 `args_type="ptr"`，0 参数的方法也一样 ——
`args[0]` 即该实例。这是拿到活对象指针的正路。

```python
hook_method(name="Object", method="get_name", namespace="UnityEngine",
            args_type="ptr")          # 事件里 args[0] = this
# 然后：read_field_value(instance=该 args[0], name="Object", field="m_CachedPtr")
```

**浮点参数/返回值走 XMM，不在整型寄存器里**：`float`/`double` 的规格由探针
从 `context.xmmN` 取，不要用 `ptr` 去凑（那会读出一个看着像地址、其实毫无
意义的整数）。返回值**不用你填** —— `hook_method` 会按静态返回类型自动推断
（`System.Single` → `float`），返回里带 `retval_type` 可以核对；
`hook_address` 指向原生函数时才需要手填 `retval_type`。

### 权限问题

**不需要管理员权限**（实测订正 —— 这条以前写错了）。

只要 **MCP 服务器进程与游戏进程的完整性级别相同**就能注入：

| 游戏以什么权限运行 | Agent 该用什么权限 | 能注入吗 |
|---|---|---|
| 普通用户（`Medium`） | 普通用户 | ✅ **实测成功**（默认就是这种情形） |
| 普通用户（`Medium`） | 管理员（`High`） | ✅ 通常可以（高往低） |
| 管理员（`High`） | 普通用户 | ❌ 失败 —— **这时才需要提权** |

实测证据：普通权限的 python（完整性级别 `Medium`）注入同样普通权限的
IL2CPP 游戏，`attach` → `runtime_info` → `find_instances` **全部成功**。
所以**默认按普通权限跑，不要一上来就要求管理员**。

真正会让注入失败的是这两类，**与权限无关**：

- **反调试 / 反注入保护**（EAC / BattlEye 等，常见于网游）——
  这是预期的，**不要反复重试**；
- 目标进程已退出、或运行时还没加载好（换 `launch_game`、或等就绪）。

### 静态查询不需要游戏在运行

第 3 步的查询全部离线可用。**这让"先做功课再动手"成为可能**：
可以先把类结构摸清楚，再启动游戏。

### 写入内存是危险操作

`write_memory` / `set_field_value` 写错地址或写错值会**立刻崩溃游戏**。
规矩：**先读、确认值符合预期、再写**。写之前把原值记下来。

**改「某个对象的某个字段」请优先用 `set_field_value`**，别自己算地址：
它按字段类型定宽度、按类型选整数/浮点/布尔编码、溢出直接报错、写完回读核对。
只有要写**裸字节**（补丁、结构体数组、认不出的类型）时才用 `write_memory`。

**绝不要写已装 Hook 的函数入口**：Frida 的 Interceptor 拥有那几字节
（它在入口写了跳转指令），绕过它直接写同一块内存 —— **哪怕写回刚读到的
同样字节** —— 实测会让游戏在数秒内崩在 `0xc0000005`（Windows 事件日志里
正是该方法 RVA 处的访问违例；同样操作写在模块头 `base` 上则完全无害）。
工具默认会**拒绝**这种写入，要强写只能 `force=True`，正确做法是先 `unhook`。
改行为请改**数据字段**，不要改代码字节。

---

## 自查清单

动手前：
- [ ] `get_diagnostics()` 看 Frida 是否可用
- [ ] 确认游戏引擎类型（IL2CPP/Mono/UE）——**UE 走完全不同的路线**
- [ ] 若是 IL2CPP：`open_game` 成功且类型数量合理
- [ ] 若是 Mono：静态查询与 `decompile` 可用，运行时探针也可用
      （注入后探针会等程序集加载稳定，看 `init.assemblies.settled`；
      `settled=false` 时先别 Hook —— 实测过早 Hook 会把游戏打崩）
- [ ] 若是 UE：`detect_ue()` 确认，再用 `ue_list_entries` 看容器

定位落点时（**IL2CPP 与 Mono 同样适用**）：
- [ ] 用 `class_detail` 拿到静态 token
- [ ] 用 `find_hook_target` / `runtime_class_info` 拿到运行时地址
- [ ] **比对两边的 token 是否一致**（Mono 上同样对账，只是没有 RVA）

改动前：
- [ ] 字段偏移是用 `runtime_class_info` 拿的真实偏移（不是猜的）
- [ ] 参数类型与实际签名一致
- [ ] 想清楚这是"临时改值"还是"改逻辑"

收尾时：
- [ ] `unhook()` 或 `close_session()`
- [ ] 确认游戏恢复正常（没有残留 Hook）

---

## 诊断：查不到东西怎么办

| 症状 | 可能原因 | 怎么办 |
|---|---|---|
| `search_classes` 返回空 | 名字拼错 / 需求完整命名空间 | 放宽 pattern（只搜一部分词）；试 `assembly` 过滤 |
| `class_detail` 说找不到 | 用了短名但库里有同名嵌套类型 | 用 `search_classes` 拿到完整 `nested_name` 再查 |
| `runtime_class_info` 报「类名有歧义」 | 短名对应多个嵌套类型（如 `State` 有 131 个） | 从 `candidates[].full_name` 里挑一个当 `name` 回灌；或补 `image=` |
| `runtime_class_info` 报「未找到类」而静态明明有 | 传的是短名，而嵌套类型必须用完整名 | 用 `search_classes` 的 `nested_name` 作为 `name`（见上方嵌套类型说明） |
| 字段类型是 `None` | 该字段类型在元数据里无法解析 | 正常现象（约 10% 字段）；用偏移 + 内存读确认实际用途 |
| `inject_process` 失败 | **权限级不匹配**（游戏是管理员、而本进程不是）/ 反调试 / 进程刚启动 | 先看游戏是不是以管理员身份运行的；是则用管理员跑 Agent，否则**不用提权**。或改用 `launch_game` |
| `runtime_class_info` 报错 | 类在游戏里还没被加载 | 先让游戏运行到相关场景（进游戏、打开对应界面） |
| Hook 装上但 0 次触发 | 目标方法在当前场景/状态不会被调 | 换个确实会执行的方法；或 `launch_game` 早期挂钩 |
| token 两边对不上 | 挂到了同名的重载 | 用 `token=` 精确指定；或 `param_count=`（见配方 5） |
| AOB 扫描 0 命中 | 特征码写错 / 段不可读 | 优先改用元数据定位；确认特征码格式（`48 8B ?? ??`） |
| `open_game` 报「这个目录下有 N 个可识别的游戏」 | 传进去的是**父目录**（如 `D:\Projects`），不是某一款游戏的目录 | 把 `game_dir` 改成返回里 `container.candidates` 里的某一个；不要指望它替你挑 |
| `open_game` 报「Unreal 游戏没有集中元数据」 | 正常行为（不是错误） | UE 不走索引：直接用 `detect_ue` / `ue_profile` / `ue_list_entries`（会话已按这个游戏选好） |
| `open_game` 报「没能识别出可支持的引擎」 | 引擎不在支持范围（Godot / GameMaker / Electron 等） | 看返回里 `engine_family.label`；本 MCP 不解析这类容器 |
| `open_game` 报「这个目录本身不是游戏，它下面的 X 是」 | 游戏外面套了一层目录 | 把 `game_dir` 指到返回里 `game_root` 的路径 |
| `close_session` 报 `ok:true` 后**游戏在一两分钟内自己崩了** | 探针线程没从 IL2CPP 域摘除（**已在 1.0.0+ 修复**，见下方） | 见下方「收尾后游戏崩溃」 |
| 工具调用**长时间不返回**，且期间**任何**工具都不回话 | 探针卡在 IL2CPP/Mono 内部（旧版本会永久挂住；**1.0.0+ 有 30 秒超时**） | 等它超时后看 `wedged`；游戏通常已 `Fatal error in GC`，`taskkill` 后重新注入。见下方「工具调用卡住」 |
| `close_session` 返回 `restored:false` + `wedged:true` | 探针已卡死，卸载/摘域这些同步收尾步骤**同样会挂住**，所以被跳过 | 这不是「收尾失败要重试」：先结束游戏进程，再重新注入。注意 `session_released:true` 只表示可以重新注入，**不**表示游戏已恢复原状 |
| 某个慢查询在跑时，另一个工具也超时 | 旧版本会把事件循环占死（**1.0.0+ 已修**：同步工具 offload 到线程） | 升级到 1.0.0+；仍出现请报告 |

### 收尾后游戏崩溃（`frida-agent.dll_unloaded`）——已修复

**症状**（旧版本）：`close_session` 如实返回 `ok:true / restored:true / detached:true`，
`unhook` 也确实卸掉了 Hook —— 但游戏在 **80~160 秒后**崩掉，
Windows 事件日志里出错模块是 `frida-agent.dll_unloaded`、异常码 `0xc0000005`、
偏移恒为 `0x9c7e0`。

**根因**：探针用 `il2cpp_thread_attach(dom)` 把 Frida 的线程挂进 IL2CPP 域，
IL2CPP 为此在线程本地存了一份该线程的 `Il2CppThread` 记录。
而卸载路径**从来没有调用 `il2cpp_thread_detach`** —— 于是 `script.unload()`
释放 agent 内存之后，IL2CPP 域里仍留着指向失效内存的线程记录，
**下一次 GC 走到它时就访问违例**。这解释了几个看似矛盾的现象：

- 为什么崩溃延迟 80~160 秒：在等一次 GC（Ved 的 `ved.gc.log` 里
  那行 `Collecting from unknown thread` 就是现场）。
- 为什么**非 Unity 进程**（python）detach 后完好无损：它们没有 IL2CPP 域。
- 为什么三款不同游戏的崩溃偏移**完全一致**（`0x9c7e0`）：同一处代码路径。
- 为什么与 Hook 无关：`attach → detach` 不加载任何 Hook 也照样崩。

**修复**：卸载前先摘域。`agent.js` 新增 `dispose()` RPC 调用
`il2cpp_thread_detach`，宿主在 `script.unload()` **之前**调用它
（见 `RuntimeSession.close()`）。`close_session` 的 `steps` 里因此多了
一项 `domain_released`，可据此确认这一步真的执行了。

**验证**：Ved 与 Pass The Fear 两款原本必崩的游戏，
修复后 detach 并观察 **360 秒**（超过原崩溃窗口 2 倍以上）均存活。
另有一款非 Unity 进程作阴性对照。

> **一条方法论教训**（踩过两次）：
>
> 1. 「某款游戏存活」这种结论**必须先确认观察窗口足够长**。最初测
>    `PathOfIdle` 只看 200 秒，得出「它不受影响」，于是误判成
>    「个别游戏特有」；实际崩溃发生在 80~160s，属于窗口边缘，
>    换个时机重测就翻了。
> 2. 进程名匹配要小心：Frida 报的是 `PassTheFear.exe`（带后缀），
>    按 `PassTheFear` 匹配会全部落空，看起来像「进程不可见」。
>    用 **PID** 判定最可靠。

### 工具调用「卡住不返回」——已修复（1.0.0+）

**症状**（旧版本）：`launch_game` 调用**十几分钟不返回**，而且期间
**任何**工具都不回话（连不碰 Frida 的 `get_session` 也超时）；
被启动的游戏窗口标题停在 `Fatal error in GC`。

**两个根因**（同一次现场的两半）：

1. **探针的 Frida 同步 RPC 没有超时**。`script.exports_sync.x()` 没有超时参数，
   而 `wait_ready` 的 deadline **只在每次调用返回之后**才检查 ——
   一次不返回，60 秒预算整体失效，`launch_game` 永不返回。
2. **同步工具占住事件循环**。本 MCP 的工具都是 `def`（不是 `async def`），
   而 mcp 1.26.0 对同步函数是**直接调用、没有 `to_thread`** ——
   于是任何慢工具都会让服务器停止响应所有请求。

**修复后的行为**（你可以依赖这些）：

- 单次探针 RPC 超过 **30 秒**没返回 → 如实报错，并把会话标记为**卡死**；
  后续调用**立刻**失败（不再各等 30 秒）。`wait_ready` 也会提前退出，
  不吃满预算。
- 卡死时 `close_session` **有界返回**：它跳过「卸载 Hook / 摘域 / 卸载脚本」
  这些**同样会挂住**的同步收尾步骤，并如实返回
  `ok:false` + `restored:false` + `wedged:true` + `session_released:true` +
  `warning`。两个字段含义**不同**，别混：
  `session_released` = 会话对象已作废、可以重新注入；
  `restored` = 游戏是否已恢复原状 —— 卡死时是 `false`（Hook 可能还留在进程里）。
  **不要**把 `restored:false` 当成「收尾失败要重试」——
  卡死时重试同样会挂住，先 `taskkill` 掉游戏。
- 慢工具不再阻塞其它工具：一个工具在跑时，`get_session` 之类的调用照常秒回。

**看到 `wedged: true` 时怎么办**：结束游戏进程 → 重新 `inject_process` /
`launch_game`。实测这一步能恢复（重启后索引与注入都正常）。

> **根因尚未定位，别急着下结论。** 现在能确定的只有这些：
> · 触发条件是**探针在游戏早期初始化阶段就调 `il2cpp_thread_attach`**
>   —— 隔离实验里「先 resume 再加载探针并立刻轮询 init」会卡住，
>   而「先让游戏自己跑 25 秒再加载探针」正常、以及**不加载探针**只
>   spawn+attach 也正常（`devtools/archive/probes/frida_matrix.py` 的 A~G 形态）。
> · 它是**间歇性**的：同样的顺序实测既有卡住也有正常
>   （真实 MCP 路径曾连续 6/6 正常，也在别的时点连续 2 次卡住），
>   所以**单次观测不能当结论**。
> · 卡住时游戏窗口标题是 `Fatal error in GC`。
>
> 也就是说：**不能**说成「游戏自己的问题、与探针无关」——现有证据反而指向
> 探针的调用时机。已修的是「不再无限挂住」（30 秒超时 + 如实报错），
> 根因修复仍待做，见 `docs/重启验证与运行时理解报告.md` §4.2。
