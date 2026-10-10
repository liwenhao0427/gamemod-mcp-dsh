# archive/ —— 归档的过程产物（**不是验收的一部分**）

这里的脚本是**一次性侦察脚本**：当初为了回答一个具体问题临时写的，
用来反推 IL2CPP v31 的记录布局、找出字段类型解析规则
（「比 `typeIndex` 小的最大 byval 键」）、确认 pak 索引头的字段顺序等等。

**为什么不删**：它们逐字节地打印原始结构。真有一天某个版本的游戏格式变了、
索引解析出错，重新定位的第一步就是把它们翻出来跑。
`verify_*.py` 回答的是「现在的实现对不对」，这里回答的是「这个格式到底长什么样」。

**为什么不放回 `devtools/` 根目录**：`devtools/` 根下的 `verify_*.py` 是**门禁**，
红一条就不许提交；把 31 个「结论已经沉淀进文档、脚本本身不再维护」的文件
和门禁混在一起，会让「`devtools/` 里都是要跑的东西」这个判断失效。
归档目录的语义很清楚：**要跑门禁请跑 `devtools/run_all_verify.py`，
这里的东西不参与。**

## 使用前请注意（它们**不保证**还能跑）

- 脚本里的样本路径是按当时的工作区写的，**可能已经失效**；
  现在样本路径的唯一真值是 `devtools/samples.py`。
- 一些脚本调用的内部 API 可能已经改名或删除。
- 它们**从不**被 `run_all_verify.py` 收集（文件名不以 `verify_` 开头），
  也没有人保证它们仍然输出正确结论 —— 归档 = 不再维护。

要重新用它定位问题时，把结论写回 `docs/` 或转成一个 `verify_*.py` 断言，
不要让它散着留在归档里当第二份真相。

## 内容（42 个：41 个 `.py` + 1 个 `.ps1`，计数是跑出来的）

### `probes/` 下的格式侦察脚本（31 个）

| 前缀 | 个数 | 方向 |
|---|---|---|
| `probe_field*.py` | 9 | IL2CPP 字段类型/偏移的解析规则 |
| `probe_residual*.py` | 4 | 解析残留（未被任何轨道覆盖的记录） |
| `probe_typedef*.py` | 2 | `typedef` 索引语义 |
| `probe_method*.py` | 2 | 方法表与参数计数（IL2CPP 与 Mono 对照） |
| `probe_final_methods.py` | 1 | 方法表最终结论的复核 |
| `probe_il2cpptype.py` / `probe_typeresolve.py` / `probe_indexspace.py` | 3 | 类型索引空间 |
| `probe_crossgame.py` / `probe_mismatch.py` | 2 | 跨游戏对照与差异定位 |
| `probe_nested.py` / `probe_parent_resolve.py` / `probe_hierarchy.py` / `probe_declaring.py` | 4 | 嵌套类型与父类解析 |
| `probe_metadata.py` / `probe_mono_paramcount.py` / `probe_tiling.py` | 3 | 元数据总览与其它 |
| `_recon_pe.ps1` | 1 | PE 结构侦察（PowerShell） |

### `probes/` 下的运行时链路与缺陷复现脚本（11 个，2026-10-03 加）

这 11 个回答的不是「格式长什么样」，而是「**运行时链路究竟走到哪一步、在哪一步断**」。
它们都不以 `verify_` 开头，所以不被 `run_all_verify.py` 收集 —— 原因很实在：
它们需要**真实的正在运行的游戏**，慢（分钟级），而且其中两个**会故意把游戏
卡死/打死**以复现缺陷。结论已写进 `docs/重启验证与运行时理解报告.md`。

| 脚本 | 方向 |
|---|---|
| `runtime_matrix.py` | 对已在运行的游戏跑完整运行时链路（注入→偏移→落点→token 对账→Hook→事件→收尾），逐项记 `ok` 与耗时；**横向扫描的主工具** |
| `failed_call_side_effect.py` | 「失败的 `open_game` 仍改写会话身份」的复现（`docs/重启验证与运行时理解报告.md` §4.3） |
| `blocking_check.py` | 「同步工具占住事件循环」的复现：慢调用在飞时 `get_session` 是否还能回话（§4.1） |
| `wedge_repro.py` | 「探针 Frida 同步 RPC 不返回 → `launch_game` 永不返回」的定向复现（§4.2）。**会故意把游戏卡在 `Fatal error in GC`** |
| `bounded_repeat.py` | 真实 MCP 路径下 `launch_game` 的**有界**成功率统计；按进程树硬杀做收尾，自身不会挂 |
| `repeat_launch.py` | 同上（早期版本，收尾会跟着挂 —— 留作对照，见 §4.2） |
| `frida_matrix.py` | 隔离实验 A~G：把「Frida 的 spawn / stdio 策略 / 探针脚本 / 调用时机」拆开，定位是谁让游戏进不去主场景 |
| `frida_repeat.py` | 重复跑上者的某个形态，用次数说话（竞态不能靠单次观测下结论）。**注意：收尾可能挂死** |
| `probe_check.py` | 最小底座：用独立 stdio 服务器跑 `static` / `attach` / `spawn` 三种场景，用于「先确认工具能不能用」 |
| `wedge_ab.py` | **对照实验**：把「第一次探测前的静默」与「探测间隔」当两个独立因素，各跑 N 轮量卡死率（报告 §7.3 的那张表就是它跑出来的）。用 `INTERVAL` / `DELAY` / `HOOK_OUTPUT` 环境变量控制变量 |
| `verify_settle.py` | 验证 `READY_SETTLE_DELAY` 这条缓解措施：走**产品** `wait_ready`，在**最恶劣条件**（探测间隔 0）下量卡死率 |

> **一条实测教训（写在这里，因为两个脚本都踩过）**：这类实验脚本的**收尾**
> 必须有界。探针卡死时 `script.unload()` / `session.detach()` **同样**是
> 没有超时的同步 Frida 调用，会把驱动脚本一起挂住（`wedge_ab.py` 第一版
> 就是这样挂了 8 分钟，只能靠 `taskkill` 救）。现在的做法是：
> 卡死时**跳过**收尾（下一轮开头会 `taskkill` 掉游戏），非卡死时才调、
> 且用看守线程限时。

> **两条踩出来的教训**（写在这里，免得下次重犯）：
>
> 1. **慢调用必须自证「真的慢」**。`blocking_check.py` 第一版用
>    `open_game(force_rebuild=True)` 当慢调用，实测 0.06 s 就返回 ——
>    那是**空转**，它却给出了「阻塞不成立」的结论。现在两条判据
>    （慢调用 ≥2 s **且** 期间无响应）都要成立才算证据。
> 2. **收尾也要抗挂死**。§4.2 那个缺陷是「Frida 同步调用不返回」，
>    而 `script.unload()` / `session.detach()` **同样是**没有超时的同步调用 ——
>    于是驱动脚本自己也会挂（实测挂了 18 分钟）。所以 `bounded_repeat.py`
>    改为每次子轮次独立进程 + `taskkill /T /F` 杀整棵树 + 结果逐轮落盘。
