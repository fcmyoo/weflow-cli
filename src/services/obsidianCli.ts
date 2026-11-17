/**
 * Obsidian 官方 CLI 的封装层 —— 让 weflow-cli 能直接读写用户自己的 Obsidian 知识库。
 *
 * 为什么值得单独一层，而不是各处直接 `execFile`：
 *
 * 1. **Obsidian CLI 的错误不走 stderr，也不走退出码**。实测（1.15.1）：`read` 一个不存在的文件
 *    会打印 `Error: File "X" not found.` 到 **stdout**，退出码仍是 **0**。所以"退出码非 0 = 失败"
 *    这条通用约定在这里是错的，必须显式识别 `Error:` 前缀 —— 否则一次"文件不存在"会被当成
 *    成功返回，把错误文本当成笔记正文继续往下传。
 * 2. **`vault=` 必须放在子命令前面**。实测 `vault=nonexistent-vault vault info=name` 报
 *    `Vault not found.`，而 `vault info=name vault=nonexistent-vault` 静默返回当前库的名字。
 *    参数位置错了不会报错，只会给你**错的库** —— 比报错更危险。
 * 3. **CLI 服务端只在 Obsidian 运行时存在**。它通过命名管道（`\\.\pipe\obsidian-cli-<用户>`）
 *    与 GUI 通信；GUI 没开时，CLI 客户端会退化成"启动一个新 Obsidian 实例"，于是 stdout 里
 *    混进启动日志、命令结果根本收不到。判据是 stdout 出现 `Loaded main app package`。
 * 4. **部分命令在当前版本会崩溃**。实测 `search:context` 在 1.15.1 直接以 -1 退出且无输出，
 *    这类命令必须绕开而不是原样转发。
 *
 * 这一层只做"把 Obsidian CLI 调通、结果解析干净、失败说清楚"，不做任何知识加工——
 * 加工（排序、跨线合并、AI 摘要）留在上层，避免同一个概念两处实现各自漂移。
 */
import { execFileSync } from 'node:child_process'
import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { join, delimiter } from 'node:path'
import { homedir } from 'node:os'

/** Obsidian CLI 的单次调用超时。读大库/大文件会慢，但超过 60 秒基本是卡住了。 */
const DEFAULT_TIMEOUT_MS = 60_000

export interface ObsidianCliRun {
  ok: boolean
  /** 去掉首尾空行后的 stdout */
  stdout: string
  /** stderr 原文（Obsidian 基本不用它，但 Electron 启动失败会写这里） */
  stderr: string
  /** 退出码；被信号杀死时为 null */
  code: number | null
  /** 失败原因（已分类：不可用 / 超时 / 退出码 / CLI 报错） */
  error?: string
  /** 是否需要先启动 Obsidian —— 供调用方给出「请打开 Obsidian」的可行动提示 */
  needsRunning?: boolean
}

/**
 * Obsidian CLI 报的错**长这样**：`Error: File "X" not found.` / `Error: Missing required parameter: ...`
 * 后面还可能跟一段 Usage。注意它是打到 **stdout** 的，退出码 0。
 */
const ERROR_PREFIX = /^Error:\s*/

/** stdout 是否是 Obsidian 自己报的错误（而不是笔记正文恰好以 "Error:" 开头）。 */
export function isCliError(stdout: string): boolean {
  return ERROR_PREFIX.test(stdout.trimStart())
}

/** 从 stdout 里剥掉 CLI 的错误前缀，返回纯错误文本。 */
export function stripErrorPrefix(stdout: string): string {
  return stdout.trimStart().replace(ERROR_PREFIX, '').trim()
}

/**
 * Obsidian 没在运行时，CLI 客户端会走「启动一个完整实例」的分支，stdout 里混进 Electron 启动日志。
 * 看到这句就说明命令根本没送到运行中的 Obsidian。
 */
export function looksLikeColdStart(stdout: string): boolean {
  return stdout.includes('Loaded main app package')
}

/**
 * 定位 Obsidian 可执行文件。
 *
 * 顺序：显式环境变量 → 常见安装位置 → PATH 目录里找可执行文件。
 *
 * **刻意不 spawn `obsidian version` 来探测**：那要起一次 Electron，冷启动 2~5 秒，
 * 而这个函数在每条 CLI 命令里都会被调一次。直接看文件是否存在是零成本的。
 *
 * 另一个坑：Windows 上 Obsidian 注册到**用户级** PATH（注册表 `Environment`），
 * 新开的进程能继承，已运行的进程不会 —— 所以在**已经开着**的终端里 `where obsidian`
 * 可能找不到，但安装目录永远在。这也解释了为什么不能只依赖 PATH。
 */
export function findObsidianExecutable(): string | null {
  const explicit = process.env.WEFLOW_OBSIDIAN_EXE
  if (explicit && existsSync(explicit)) return explicit

  const home = homedir()
  const candidates = process.platform === 'win32'
    ? [
        join(home, 'AppData', 'Local', 'Programs', 'Obsidian', 'Obsidian.exe'),
        join(home, 'AppData', 'Local', 'Obsidian', 'Obsidian.exe'),
        join('C:', 'Program Files', 'Obsidian', 'Obsidian.exe'),
      ]
    : [
        join('/Applications', 'Obsidian.app', 'Contents', 'MacOS', 'Obsidian'),
        '/usr/bin/obsidian',
        '/usr/local/bin/obsidian',
      ]
  for (const candidate of candidates) {
    if (existsSync(candidate)) return candidate
  }

  // PATH 上找：只认真实的可执行文件名，不去 spawn（spawn 一次要好几秒）。
  const names = process.platform === 'win32'
    ? ['obsidian.exe', 'obsidian.cmd', 'obsidian.bat']
    : ['obsidian']
  const dirs = (process.env.PATH || '').split(delimiter).filter(Boolean)
  for (const dir of dirs) {
    for (const name of names) {
      const full = join(dir, name)
      if (existsSync(full)) return full
    }
  }
  return null
}

/**
 * 已知在 Obsidian CLI 里会崩的命令。
 *
 * `search:context` 在 1.15.1 上以退出码 -1 无输出地崩掉（无论带不带 format）。
 * 转发它只会拿到一个空结果，用户以为"没有匹配"，实际是命令本身坏了。
 * 这里列出来是为了**在调用前就拦住**，并换一个能工作的替代路径。
 */
export const BROKEN_CLI_COMMANDS = new Set(['search:context'])

export interface ObsidianCliOptions {
  /** 目标库名（`obsidian.json` 里注册的）。不传则用当前活动库。 */
  vault?: string
  /** 超时毫秒 */
  timeoutMs?: number
  /** 允许注入假执行器（测试用） */
  exec?: (exe: string, args: string[], timeoutMs: number) => { stdout: string; stderr: string; code: number | null }
}

/**
 * 全局执行器。形状与 `pythonBridge.setScriptRunner` 一致 —— 都是「注入点」而不是配置项：
 * 测试与评测 harness 需要把真的 Obsidian 换成假的，否则评测会去调**本机的** Obsidian
 *（装了就是真出机，没装就恒为空，两种都不对）。
 *
 * 与 `options.exec` 的区别：那是**单次调用**的覆盖，这个是**进程级**的替换。
 */
export type ObsidianExec = (exe: string, args: string[], timeoutMs: number) =>
  { stdout: string; stderr: string; code: number | null }

let injectedExec: ObsidianExec | null = null

/** 换掉执行器（测试/评测用）。传 null 恢复真实实现。 */
export function setObsidianExec(next: ObsidianExec | null): void {
  injectedExec = next
}

/**
 * 调一次 Obsidian CLI。
 *
 * 刻意用 `execFileSync` 而不是 `spawn` + 异步：CLI 是短命令，同步等待更简单，
 * 而且 CLI 入口（bin/weflow-cli.ts）本身就是命令式同步风格。
 */
export function runObsidianCli(args: string[], options: ObsidianCliOptions = {}): ObsidianCliRun {
  // 单次覆盖优先于全局注入：前者是"这一次我用假的"，后者是"整个进程用假的"。
  const exec = options.exec ?? injectedExec
  // 测试注入的假执行器不需要真实可执行文件；真实路径则必须有。
  const exe = exec ? 'obsidian' : (findObsidianExecutable() ?? '')
  if (!exec && !exe) {
    return {
      ok: false,
      stdout: '',
      stderr: '',
      code: null,
      error: '找不到 Obsidian：请先安装 Obsidian，或用 WEFLOW_OBSIDIAN_EXE 指定可执行文件路径',
    }
  }

  // `vault=` 必须在子命令**前面**——放后面会被静默忽略，然后返回当前库的错数据。
  const fullArgs = options.vault ? [`vault=${options.vault}`, ...args] : args
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS

  let stdout = ''
  let stderr = ''
  let code: number | null = null
  try {
    if (exec) {
      const r = exec(exe, fullArgs, timeoutMs)
      stdout = r.stdout
      stderr = r.stderr
      code = r.code
    } else {
      const r = execFileSync(exe, fullArgs, {
        encoding: 'utf8',
        timeout: timeoutMs,
        windowsHide: true,
        maxBuffer: 64 * 1024 * 1024,
        stdio: ['ignore', 'pipe', 'pipe'],
      })
      stdout = String(r ?? '')
      stderr = ''
      code = 0
    }
  } catch (error: any) {
    stdout = String(error?.stdout ?? '')
    stderr = String(error?.stderr ?? '')
    code = typeof error?.status === 'number' ? error.status : null
    if (error?.killed || error?.signal === 'SIGTERM') {
      return { ok: false, stdout, stderr, code, error: `Obsidian CLI 超时（${timeoutMs}ms）` }
    }
    // **执行器自己抛的错**（不是 Obsidian 报的错）：真实的 `execFileSync` 抛错时
    // 会带上 stdout/stderr，而**没有** stdout 的那种（比如可执行文件找不到、
    // 或者注入的桩说"这条用例不该走到它"）如果被当成"空输出"，
    // 下游会读成「库里什么都没有」—— 一个**静默的假成功**。
    // 所以这里分开报：没有 stdout 的异常 = 执行失败，不是空结果。
    if (!stdout.trim()) {
      return {
        ok: false,
        stdout,
        stderr,
        code,
        error: `Obsidian CLI 调用失败: ${String(error?.message ?? error).slice(0, 300)}`,
      }
    }
  }

  const trimmed = stdout.trim()

  // Obsidian 没在跑：CLI 客户端退化成了「启动一个新实例」，命令结果根本不在 stdout 里。
  if (looksLikeColdStart(trimmed)) {
    return {
      ok: false,
      stdout,
      stderr,
      code,
      needsRunning: true,
      error: 'Obsidian 没有在运行：请先打开 Obsidian（CLI 通过管道与运行中的 Obsidian 通信）',
    }
  }

  if (code !== null && code !== 0) {
    return {
      ok: false,
      stdout,
      stderr,
      code,
      error: `Obsidian CLI 退出码 ${code}${trimmed ? `：${trimmed.slice(0, 200)}` : ''}`,
    }
  }

  // 退出码 0 但 stdout 是 `Error: ...` —— Obsidian 把错误打在 stdout 上，退出码不反映失败。
  if (isCliError(trimmed)) {
    return { ok: false, stdout, stderr, code, error: stripErrorPrefix(trimmed).slice(0, 500) }
  }

  return { ok: true, stdout: trimmed, stderr, code }
}

/** 从 stdout 里取 JSON。取不到返回 null（不是抛错——有些命令本来就没有 JSON）。 */
export function parseCliJson<T = unknown>(stdout: string): T | null {
  const trimmed = stdout.trim()
  if (!trimmed) return null
  try { return JSON.parse(trimmed) as T } catch { /* 落下去试最后一行 */ }
  const lines = trimmed.split(/\r?\n/).map(l => l.trim()).filter(Boolean)
  for (let i = lines.length - 1; i >= 0; i--) {
    if (!lines[i].startsWith('{') && !lines[i].startsWith('[')) continue
    try { return JSON.parse(lines[i]) as T } catch { /* 继续往前找 */ }
  }
  return null
}

/** `eval` 的输出带一个 `=> ` 前缀，JSON 在后面。 */
export function stripEvalPrefix(stdout: string): string {
  return stdout.trim().replace(/^=>\s*/, '')
}

/**
 * Obsidian 已知「没有结果」的三种措辞。
 * 它们不是错误，但调用方要能把它们和「有结果」区分开 —— 空列表和一段散文含义完全不同。
 */
const EMPTY_MARKERS = [
  'No matches found.',
  'No tasks found.',
  'No base files found in vault',
  'No files found.',
  'No folders found.',
]

/** stdout 是不是「命令成功但没有结果」。 */
export function isEmptyResult(stdout: string): boolean {
  const trimmed = stdout.trim()
  if (!trimmed) return true
  return EMPTY_MARKERS.some(marker => trimmed === marker || trimmed.startsWith(marker))
}

/** Obsidian 里的一行 TSV（`vaults`、`vault` 等命令的默认输出）。 */
export function parseTsv(stdout: string): string[][] {
  return stdout
    .split(/\r?\n/)
    .map(line => line.trim())
    .filter(Boolean)
    .map(line => line.split('\t'))
}

/** 从默认安装目录里列出 Obsidian 已注册的库（读 `obsidian.json`）。 */
export function listRegisteredVaults(): { name: string; path: string }[] {
  const configDir = process.platform === 'win32'
    ? join(process.env.APPDATA || join(homedir(), 'AppData', 'Roaming'), 'obsidian')
    : join(homedir(), '.config', 'obsidian')
  const configFile = join(configDir, 'obsidian.json')
  if (!existsSync(configFile)) return []
  try {
    const raw = JSON.parse(readFileSync(configFile, 'utf8'))
    const vaults = raw?.vaults ?? {}
    return Object.entries(vaults as Record<string, { path?: string; ts?: number }>)
      .filter(([, info]) => info?.path)
      .map(([name, info]) => ({ name, path: String(info.path) }))
  } catch {
    return []
  }
}

/** 库目录里有没有 markdown 文件（判断「这个库是不是空的」）。 */
export function vaultHasNotes(vaultPath: string): boolean {
  try {
    for (const entry of readdirSync(vaultPath, { withFileTypes: true })) {
      if (entry.isFile() && entry.name.endsWith('.md')) return true
      if (entry.isDirectory() && entry.name !== '.obsidian') return true
    }
    return false
  } catch {
    return false
  }
}
