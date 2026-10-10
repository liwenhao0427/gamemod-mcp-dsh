/**
 * GameMod MCP 组合包的 Host 半：准备 Python 侧工具链，并把结果发布成服务。
 *
 * 职责边界
 * ---------------------------------------------------------------
 * 这个模块**只做一件事**：保证「一个装好 mcp + frida 的 Python 解释器」
 * 存在，然后把它（连同包根目录、server 参数、环境变量）发布为
 * `gamemodToolchain` 服务。
 *
 * 它**不**负责启动 MCP 服务器 —— 那是同一层里 `mcp-gamemod` 行的事，
 * 那一行 `inject: [gamemodToolchain]`，所以 Loader 会等本服务就绪
 * 才启动它。这样「先装依赖、再拉服务器」的顺序由依赖关系保证，
 * 而不是靠 YAML 里的书写顺序（Loader 并发挂载各行，行序不保证顺序）。
 *
 * 为什么是托管 venv 而不是系统 Python
 * ---------------------------------------------------------------
 * 系统解释器里可能没有 mcp / frida，也可能版本不对；往用户的全局
 * site-packages 里装东西会污染其它项目。所以在 `$DSH_HOME/gamemod-mcp/venv`
 * 下建一个**本项目专用**的 venv，装什么都不影响别处，也不需要管理员权限。
 *
 * 幂等性
 * ---------------------------------------------------------------
 * 每次 DSH 启动都会走一遍本模块。已就绪时只做一次「导入探测」
 * （约几百毫秒），不做任何安装。探测失败才修复。
 *
 * 探测为什么查 `mcp.server.fastmcp`
 * ---------------------------------------------------------------
 * 本项目的服务器用的是 mcp **1.x** 的 `mcp.server.fastmcp.FastMCP`；
 * mcp 2.x 把它改名成 `MCPServer`，导入路径变了。所以探测直接导入这个
 * 确切路径 —— 它是「服务器真能起来」的最小充分检查，也把历史上踩过的
 * 「装到 mcp 2.x 然后起不来」变成一个**安装期**就能发现的失败，
 * 而不是等用户调用工具时才报 ModuleNotFoundError。
 */

import { spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, rmSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

/** Cordis 插件身份（诊断信息里显示的名字）。 */
export const name = 'gamemod-toolchain'

/** 包根目录：本模块所在目录。 */
const ROOT = dirname(fileURLToPath(import.meta.url))

/**
 * 必需依赖。
 *
 * `mcp` 必须 <2：本代码用 1.x 的 `mcp.server.fastmcp.FastMCP`，2.x 改名成
 * MCPServer。只写 `>=1.0.0` 会让干净环境装到 2.x 然后起不来 —— 打包分享包
 * 时实测到过（干净 venv → 2.3.0 → 服务器起不来）。
 */
const REQUIRED = ['mcp>=1.0.0,<2', 'frida>=16.0.0']

/**
 * 可选弱视觉依赖（截图 + OCR）。
 *
 * 不默认安装：onnxruntime / opencv 加起来几百 MB，而「最后一公里」的
 * 验收工作里只有一部分需要 OCR（元数据查询、建索引、Hook、读内存都不需要）。
 * 想用就在组合包 config 里写 `vision: true`，或用 install.py --vision。
 */
const VISION = [
  'dxcam', 'mss', 'rapidocr-onnxruntime', 'onnxruntime',
  'numpy', 'opencv-python', 'pillow', 'pywin32',
]

/** 探测脚本：直接导入本项目真正需要的入口，而不是只看包在不在。 */
const PROBE = 'from mcp.server.fastmcp import FastMCP; import frida; print("ok")'

/** 安装子进程的上限。首次装 frida 需要下载原生扩展，给足时间。 */
const INSTALL_TIMEOUT_MS = 900_000

/** 解析 DSH home：环境变量优先，否则退回 ~/.dsh。 */
function resolveDshHome() {
  const configured = process.env.DSH_HOME
  return configured && configured.length > 0 ? configured : join(homedir(), '.dsh')
}

/** 读组合包 config 并做最小校验（不引入 schemastery，保持零依赖）。 */
function readConfig(raw) {
  const config = raw && typeof raw === 'object' ? raw : {}
  for (const key of Object.keys(config)) {
    if (!['vision', 'python'].includes(key)) {
      throw new Error(`gamemod-mcp: 未知的配置项 "${key}"（可用：vision、python）`)
    }
  }
  if (config.vision !== undefined && typeof config.vision !== 'boolean') {
    throw new Error('gamemod-mcp: config.vision 必须是布尔值')
  }
  if (config.python !== undefined && typeof config.python !== 'string') {
    throw new Error('gamemod-mcp: config.python 必须是字符串')
  }
  return { vision: config.vision === true, python: config.python }
}

/** 跑一个子进程并把输出留在内存里（不往 Host 的 stdout 写东西）。 */
function run(executable, args, options = {}) {
  return spawnSync(executable, args, {
    encoding: 'utf8',
    windowsHide: true,
    maxBuffer: 64 * 1024 * 1024,
    timeout: options.timeout ?? 60_000,
    env: { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
  })
}

/** 末尾若干行，用于把子进程的真实报错带进诊断。 */
function tail(text, lines = 8) {
  const all = (text ?? '').trimEnd().split(/\r?\n/).filter(line => line.trim().length > 0)
  return all.slice(-lines).join('\n')
}

/**
 * 找一个能建 venv 的基础解释器（≥3.10）。
 *
 * 候选顺序：config.python → GAMEMOD_PYTHON → PATH 上的 python。
 * 找到的必须是 3.10+，否则 `match` 之类的新语法会直接崩。
 * @returns 可用解释器的路径。
 * @throws 找不到时抛出带安装指引的错误。
 */
function findBasePython(configured) {
  const candidates = [configured, process.env.GAMEMOD_PYTHON, 'python', 'python3']
    .filter(value => typeof value === 'string' && value.length > 0)

  const rejected = []
  for (const candidate of candidates) {
    const probe = run(candidate, ['-c', 'import sys; print("%d.%d" % sys.version_info[:2])'])
    if (probe.error || probe.status !== 0) {
      rejected.push(`${candidate}（无法执行）`)
      continue
    }
    const [major, minor] = String(probe.stdout).trim().split('.').map(Number)
    if (!(major > 3 || (major === 3 && minor >= 10))) {
      rejected.push(`${candidate}（版本 ${major}.${minor}，需要 3.10+）`)
      continue
    }
    return candidate
  }

  throw new Error(
    'gamemod-mcp: 找不到 Python 3.10+ 解释器，无法准备运行环境。\n'
    + `  已尝试：${rejected.join('、') || '（无候选）'}\n`
    + '  请从 https://www.python.org/downloads/ 安装 Python 3.10+，'
    + '安装时勾选 "Add python.exe to PATH"，然后重启 DSH。\n'
    + '  已经装了但不在 PATH 上时，可在组合包 config 里指定绝对路径：\n'
    + '    config:\n'
    + '      python: C:/Python311/python.exe',
  )
}

/** venv 里的解释器路径（Windows 与 POSIX 的目录名不同）。 */
function venvPython(venvDir) {
  return process.platform === 'win32'
    ? join(venvDir, 'Scripts', 'python.exe')
    : join(venvDir, 'bin', 'python')
}

/**
 * venv 是否**真的能用**（不是只看 `python.exe` 在不在）。
 *
 * 为什么不能只用 existsSync
 * ---------------------------------------------------------------
 * 冷启动实测踩到过：venv 目录在、`python.exe` 也在，但 `pyvenv.cfg`
 * 是**空的**（venv 建到一半被打断），于是这个解释器**一跑就报
 * `No pyvenv.cfg file`** —— 连 `--version` 都执行不了。
 *
 * 而旧代码只查 `existsSync(python)`，于是**跳过创建**、直接往里装依赖，
 * 一路失败。更糟的是这个状态**会一直保留**：之后每次启动都看到
 * 「python.exe 在」，于是永远不再尝试重建 —— 一次被打断的首装
 * 就让这个包**永久装不上**，除非用户手工删目录。
 *
 * 所以判据必须是「这个解释器能不能跑起来」，而不是「文件在不在」。
 * @returns 可用时为 true。
 */
function venvUsable(python) {
  if (!existsSync(python)) return false
  const r = run(python, ['-c', 'print(1)'], { timeout: 60_000 })
  return !r.error && r.status === 0
}

/**
 * 探测解释器是否**真的能跑本项目的服务器**。
 *
 * 只查「包装上了没」不够：包在但版本不对（mcp 2.x）时服务器照样起不来，
 * 所以这里导入的是确切的入口 `mcp.server.fastmcp`。
 * @returns 探测通过时为 true。
 */
function probeServer(python) {
  const result = run(python, ['-c', PROBE], { timeout: 120_000 })
  return !result.error && result.status === 0
}

/**
 * 确认 venv 里的 pip 可用，缺了就补上。
 *
 * 为什么不能假定 `python -m venv` 建完就有 pip
 * ---------------------------------------------------------------
 * 冷启动实测踩到过：`python -m venv` **返回 0**、`python.exe` 也在，
 * 但紧接着 `python -m pip` 报 `No module named pip`，于是整个安装失败
 * —— 而这是**对方第一次安装**必然要走的路径，一次偶发就等于装不上。
 * 单独连测 10 轮不复现，说明它与并发（ensurepip 解压到临时目录）有关，
 * 是**偶发**而非必然 —— 偶发的基础设施问题更应该由代码兜住，
 * 而不是赌它不发生。
 *
 * 所以这里显式补一次 `ensurepip`（标准库自带的引导器，不依赖网络），
 * 补完再复验。
 * @returns pip 可用时为 true；补不上则抛错（不静默继续）。
 */
function ensurePip(python) {
  const ok = (r) => !r.error && r.status === 0
  if (ok(run(python, ['-m', 'pip', '--version'], { timeout: 120_000 }))) return true

  const boot = run(python, ['-m', 'ensurepip', '--upgrade', '--default-pip'],
                   { timeout: 300_000 })
  if (!ok(run(python, ['-m', 'pip', '--version'], { timeout: 120_000 }))) {
    throw new Error(
      'gamemod-mcp: venv 里没有可用的 pip，且用 ensurepip 引导失败。\n'
      + tail(`${boot.stdout ?? ''}\n${boot.stderr ?? ''}`)
      + `\n  可手动修复：\n    "${python}" -m ensurepip --upgrade --default-pip`,
    )
  }
  return true
}

/** pip 安装（--no-input 防止它在无人值守的启动流程里卡住等输入）。 */
function pipInstall(python, specs) {
  return run(python, [
    '-m', 'pip', 'install', '--no-input', '--disable-pip-version-check',
    '--upgrade', ...specs,
  ], { timeout: INSTALL_TIMEOUT_MS })
}

/** 组合包内 server 的运行参数与环境（不含绝对路径，全部相对包根）。 */
function serverInvocation(root) {
  return {
    python: undefined,
    serverArgs: ['-m', 'gamemod_mcp.server'],
    env: {
      PYTHONPATH: join(root, 'src'),
      PYTHONUTF8: '1',
      PYTHONUNBUFFERED: '1',
      PYTHONDONTWRITEBYTECODE: '1',
    },
  }
}

/** Cordis 插件入口：准备工具链 → 发布服务。 */
export async function apply(ctx, rawConfig) {
  const config = readConfig(rawConfig)
  const root = ROOT
  const stateDir = join(resolveDshHome(), 'gamemod-mcp')
  const venvDir = join(stateDir, 'venv')
  const python = venvPython(venvDir)

  const specs = config.vision ? [...REQUIRED, ...VISION] : [...REQUIRED]
  const label = config.vision ? '必需依赖 + 弱视觉' : '必需依赖'

  if (probeServer(python)) {
    // 已就绪：这是**常态路径**（每次 DSH 启动都会走到这里）。
    ctx.logger.info(`gamemod-mcp: Python 环境就绪（${python}）`)
  } else {
    ctx.logger.info(`gamemod-mcp: 正在准备 Python 环境（${label}），首次安装可能需要一两分钟…`)

    if (!venvUsable(python)) {
      mkdirSync(stateDir, { recursive: true })
      // 目录在但解释器跑不起来 = 上次建到一半被打断（实测过：pyvenv.cfg
      // 为空，连 `--version` 都报 No pyvenv.cfg file）。这种残留必须**清掉
      // 重建**，否则 `python -m venv` 会因为目录非空而拒绝/留下混合状态，
      // 而下次启动又会看到「python.exe 在」从而跳过创建 —— 永久卡死。
      if (existsSync(venvDir)) {
        ctx.logger.warn('gamemod-mcp: 发现不可用的 venv 残留，正在重建…')
        try {
          rmSync(venvDir, { recursive: true, force: true })
        } catch (error) {
          throw new Error(
            `gamemod-mcp: 无法删除损坏的 venv（${venvDir}）：${String(error)}\n`
            + '  请手工删掉该目录后重启 DSH。',
          )
        }
      }
      const base = findBasePython(config.python)
      ctx.logger.info(`gamemod-mcp: 用 ${base} 创建专用 venv：${venvDir}`)
      const created = run(base, ['-m', 'venv', venvDir], { timeout: 300_000 })
      // 判据同样是「能不能跑」，不是「文件在不在」—— venv 可能返回 0
      // 却留下一个不可用的解释器（冷启动实测）。
      if (created.error || created.status !== 0 || !venvUsable(python)) {
        throw new Error(
          'gamemod-mcp: 创建 venv 失败。\n'
          + tail(`${created.stdout ?? ''}\n${created.stderr ?? ''}`)
          + `\n  可手工重试：\n    "${base}" -m venv "${venvDir}"`,
        )
      }
    }

    // 不能假定 venv 建完就有 pip（冷启动实测踩到过，见 ensurePip 注释）。
    ensurePip(python)

    const installed = pipInstall(python, specs)
    if (installed.error || installed.status !== 0) {
      throw new Error(
        `gamemod-mcp: 安装 ${label} 失败（网络或镜像问题）。\n`
        + tail(`${installed.stdout ?? ''}\n${installed.stderr ?? ''}`)
        + '\n  可手动重试：\n'
        + `    "${python}" -m pip install ${specs.join(' ')}\n`
        + '  公司网络下可加国内镜像：\n'
        + `    "${python}" -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple ${specs.join(' ')}`,
      )
    }

    // 装完必须复验：pip 退出码 0 不等于「服务器真能起来」。
    if (!probeServer(python)) {
      const check = run(python, ['-m', 'pip', 'show', 'mcp', 'frida'])
      throw new Error(
        'gamemod-mcp: 依赖装完了，但服务器入口仍无法导入 —— 环境不可用。\n'
        + `  探测：${PROBE}\n`
        + tail(`${check.stdout ?? ''}`)
        + '\n  若 mcp 装成了 2.x，本项目需要 1.x（FastMCP 在 2.x 已改名 MCPServer）。',
      )
    }
    ctx.logger.info(`gamemod-mcp: Python 环境已就绪（${python}）`)
  }

  const invocation = serverInvocation(root)
  const toolchain = {
    /** 运行 MCP 服务器的解释器（已确认能导入 mcp.server.fastmcp + frida）。 */
    python,
    /** server 的启动参数；配合 cwd + PYTHONPATH 使用，不含绝对路径。 */
    serverArgs: invocation.serverArgs,
    /** 包根目录，作为 MCP 服务器的工作目录。 */
    root,
    /** 传给 MCP 子进程的额外环境变量。 */
    env: invocation.env,
    /** 供诊断使用的环境信息。 */
    venv: venvDir,
    vision: config.vision,
    packages: specs,
  }

  // 发布服务。这一行下面的 mcp-gamemod 行 inject 了本服务，
  // 所以它会在本行激活之后才启动 —— 依赖关系即挂载顺序。
  ctx.provide('gamemodToolchain', toolchain)
  ctx.logger.info('gamemod-mcp: 已发布 gamemodToolchain 服务，MCP 客户端即将启动')

  registerSkill(ctx)
}

/**
 * 把随包分发的 `gamemod-mod` skill 注册进 skill 注册表。
 *
 * 为什么用 `ctx.inject` 而不是直接 `inject = ['skills']`
 * ---------------------------------------------------------------
 * `skills` 不是本组合包的必需依赖 —— 没挂 skill 服务时（例如精简 profile）
 * 工具链与 MCP 客户端**仍然应该正常工作**，只是少了这份说明。所以这里用
 * `ctx.inject` 做可选注入：服务出现时注册，消失时自动撤销。
 *
 * 为什么 skill 要随包走
 * ---------------------------------------------------------------
 * 以前这份 SKILL.md 靠手工复制到 `$DSH_HOME/skills/`，而那份副本不在任何
 * 门禁范围内 —— 实测曾落后 139 行，开头还让 Agent 去调一个已删除的工具。
 * 随包注册后，Agent 看到的就是**这个版本包里的那一份**，不存在副本漂移。
 *
 * 失败不影响 MCP：读不到文件只记警告。
 */
function registerSkill(ctx) {
  const skillDir = join(ROOT, 'skills', 'gamemod-mod')
  const skillFile = join(skillDir, 'SKILL.md')

  ctx.inject(['skills'], (skillCtx) => {
    let raw
    try {
      raw = readFileSync(skillFile, 'utf8')
    } catch (error) {
      ctx.logger.warn(`gamemod-mcp: 读不到 skill 文件 ${skillFile}（${String(error)}），跳过注册`)
      return
    }

    // 复用 skill-filesystem 的前置元数据格式：YAML frontmatter 的 name/description。
    // 这里只做最小解析 —— 文件是我们自己随包发的，不是用户输入。
    const match = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/.exec(raw)
    if (match === null) {
      ctx.logger.warn(`gamemod-mcp: skill 文件缺少 frontmatter，跳过注册：${skillFile}`)
      return
    }
    const frontmatter = match[1]
    const readField = (key) => {
      const line = new RegExp(`^${key}:\\s*(.+)$`, 'm').exec(frontmatter)
      return line === null ? undefined : line[1].trim().replace(/^["']|["']$/g, '')
    }
    const skillName = readField('name')
    const description = readField('description')
    if (skillName === undefined || description === undefined) {
      ctx.logger.warn(`gamemod-mcp: skill frontmatter 缺 name 或 description，跳过注册：${skillFile}`)
      return
    }

    skillCtx.skills.register({
      name: skillName,
      description,
      source: 'bundled',
      provider: 'gamemod-mcp',
      // 相对资源基准指向 skill 目录，Agent 需要读同目录下其它文件时能解析。
      resourceBase: { kind: 'directory', path: skillDir },
      path: skillFile,
      content: raw.slice(match[0].length).trim(),
    })
    ctx.logger.info(`gamemod-mcp: 已注册 skill "${skillName}"`)
  })
}
