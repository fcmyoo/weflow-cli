/**
 * Obsidian 知识库的**语义层** —— 把 Obsidian CLI 的原始命令变成有业务含义的操作。
 *
 * 与 `obsidianCli.ts` 的分工：
 * - `obsidianCli.ts` 只管「把命令跑通、错误说清楚」（传输层）
 * - 这里管「跑哪个命令、结果怎么组织成能直接给人看的东西」（语义层）
 *
 * 为什么要有语义层，而不是让 CLI 直接透传 100 个 Obsidian 命令：
 * 1. **Obsidian 的命令是给 GUI 用的**，输出形状很杂（TSV / JSON / 散文 / 表格）。
 *    直接透传，调用方每次都要自己判形状。
 * 2. **weflow-cli 有自己的两条线**（文章线 / 聊天线），而 Obsidian CLI 只知道"库"。
 *    跨线合并、去重、排序这些活必须在这一层做。
 * 3. **有几条命令在 Obsidian 侧坏掉了**（`search:context` 在 1.15.1 直接崩），
 *    语义层要给它们一条能走的替代路径。
 */
import {
  runObsidianCli,
  parseCliJson,
  isEmptyResult,
  stripEvalPrefix,
  listRegisteredVaults,
  findObsidianExecutable,
  type ObsidianCliRun,
} from './obsidianCli.js'
import { join } from 'node:path'
import { resolveOutputRoot } from '../utils/outputRoot.js'

export interface ObsidianQueryOptions {
  /** 目标库名。不传则用 Obsidian 当前活动库。 */
  vault?: string
  /** 超时毫秒 */
  timeoutMs?: number
  /** 注入假执行器（测试用） */
  exec?: (exe: string, args: string[], timeoutMs: number) => { stdout: string; stderr: string; code: number | null }
}

export interface ObsidianResult<T> {
  success: boolean
  data?: T
  error?: string
  /** 需要用户先打开 Obsidian */
  needsRunning?: boolean
}

function fail<T>(run: ObsidianCliRun): ObsidianResult<T> {
  return {
    success: false,
    error: run.error ?? 'Obsidian CLI 调用失败',
    needsRunning: run.needsRunning,
  }
}

/** weflow-cli 自己的 Obsidian 库（`wiki compile` 等命令写进去的那一个）。 */
export function defaultWeFlowVaultName(): string {
  return 'wechat-vault'
}

/** weflow-cli 自己的 Obsidian 库在磁盘上的路径。 */
export function defaultWeFlowVaultPath(): string {
  return join(resolveOutputRoot(), 'wechat-vault')
}

// ---------------------------------------------------------------- 检索

export interface ObsidianSearchHit {
  path: string
  /** 命中所在的行（带上下文搜索才有） */
  line?: string
}

/**
 * 全文搜索。
 *
 * 走 Obsidian 的 `search` 命令而不是本地 grep：Obsidian 的索引已经建好了，
 * 一次搜索是毫秒级；本地 grep 要遍历整库（这里有上万条材料）。
 *
 * `search:context`（带上下文的搜索）在 1.15.1 上会崩（退出码 -1，无输出），
 * 所以**不转发**它 —— 转发只会得到空结果，用户会误以为"库里没有"。
 */
export function searchVault(query: string, options: ObsidianQueryOptions & { limit?: number; path?: string } = {}): ObsidianResult<ObsidianSearchHit[]> {
  if (!query.trim()) return { success: false, error: '搜索词不能为空' }
  const args = ['search', `query=${query}`, 'format=json']
  if (options.limit && options.limit > 0) args.push(`limit=${options.limit}`)
  if (options.path) args.push(`path=${options.path}`)

  const run = runObsidianCli(args, options)
  if (!run.ok) return fail(run)

  const parsed = parseCliJson<string[]>(run.stdout)
  if (parsed === null) {
    // 没有 JSON：要么是"没匹配"（散文措辞），要么是别的形状。
    if (isEmptyResult(run.stdout)) return { success: true, data: [] }
    return { success: true, data: run.stdout.split(/\r?\n/).filter(Boolean).map(p => ({ path: p })) }
  }
  return { success: true, data: parsed.map(p => ({ path: p })) }
}

// ---------------------------------------------------------------- 读取

export interface ObsidianNote {
  path: string
  content: string
}

/** 读一篇笔记的正文。 */
export function readNote(path: string, options: ObsidianQueryOptions = {}): ObsidianResult<ObsidianNote> {
  const run = runObsidianCli(['read', `path=${path}`], options)
  if (!run.ok) return fail(run)
  return { success: true, data: { path, content: run.stdout } }
}

/** 读一篇笔记的 frontmatter 属性。 */
export function readProperties(path: string, options: ObsidianQueryOptions = {}): ObsidianResult<Record<string, unknown>> {
  const run = runObsidianCli(['properties', `path=${path}`, 'format=json'], options)
  if (!run.ok) return fail(run)
  const parsed = parseCliJson<Record<string, unknown>>(run.stdout)
  if (!parsed) return { success: true, data: {} }
  return { success: true, data: parsed }
}

export interface ObsidianHeading {
  level: number
  heading: string
  line: number
}

/** 读一篇笔记的大纲（标题层级）。 */
export function readOutline(path: string, options: ObsidianQueryOptions = {}): ObsidianResult<ObsidianHeading[]> {
  const run = runObsidianCli(['outline', `path=${path}`, 'format=json'], options)
  if (!run.ok) return fail(run)
  const parsed = parseCliJson<ObsidianHeading[]>(run.stdout)
  return { success: true, data: parsed ?? [] }
}

// ---------------------------------------------------------------- 图谱

export interface ObsidianLink {
  /** 出链指向的文件（`links`）或指向本文件的文件（`backlinks`） */
  path: string
}

/** 一篇笔记的出链（它引用了谁）。 */
export function getOutgoingLinks(path: string, options: ObsidianQueryOptions = {}): ObsidianResult<ObsidianLink[]> {
  const run = runObsidianCli(['links', `path=${path}`], options)
  if (!run.ok) return fail(run)
  if (isEmptyResult(run.stdout)) return { success: true, data: [] }
  return {
    success: true,
    data: run.stdout.split(/\r?\n/).filter(Boolean).map(p => ({ path: p })),
  }
}

/** 一篇笔记的反链（谁引用了它）。 */
export function getBacklinks(path: string, options: ObsidianQueryOptions = {}): ObsidianResult<ObsidianLink[]> {
  const run = runObsidianCli(['backlinks', `path=${path}`, 'format=json'], options)
  if (!run.ok) return fail(run)
  const parsed = parseCliJson<{ file?: string }[]>(run.stdout)
  if (!parsed) return { success: true, data: [] }
  return {
    success: true,
    data: parsed
      .map(item => item?.file)
      .filter((f): f is string => typeof f === 'string')
      .map(p => ({ path: p })),
  }
}

export interface ObsidianUnresolvedLink {
  link: string
  count?: number
  sources?: string[]
}

/**
 * 库里的未解析链接（[[某概念]] 但目标页不存在）。这是知识库最常见的缺口。
 *
 * **Obsidian 的 `counts`/`verbose` 返回的是字符串，不是数字和数组**：
 * `{"link":"白名单","count":"2","sources":"a.md, b.md"}`。
 * 所以这里要归一化——`count` 转数字，`sources` 按逗号拆开。
 * 直接把字符串当数字用会得到 `NaN`，把整串当数组用会得到一个长度 1 的"数组"。
 */
export function getUnresolvedLinks(options: ObsidianQueryOptions = {}): ObsidianResult<ObsidianUnresolvedLink[]> {
  const run = runObsidianCli(['unresolved', 'format=json', 'counts', 'verbose'], options)
  if (!run.ok) return fail(run)
  const parsed = parseCliJson<{ link?: string; count?: unknown; sources?: unknown }[]>(run.stdout)
  if (!parsed) return { success: true, data: [] }
  return {
    success: true,
    data: parsed
      .filter(item => item?.link)
      .map(item => ({
        link: String(item.link),
        count: toCount(item.count),
        sources: toSourceList(item.sources),
      })),
  }
}

/** Obsidian 的计数可能是数字也可能是字符串（`"2"`）；不是数字就返回 undefined，不假装是 0。 */
function toCount(value: unknown): number | undefined {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string') {
    const n = Number(value.trim())
    if (Number.isFinite(n)) return n
  }
  return undefined
}

/** `sources` 可能是数组，也可能是逗号分隔的字符串。两种都要接住。 */
function toSourceList(value: unknown): string[] | undefined {
  if (Array.isArray(value)) return value.map(String).filter(Boolean)
  if (typeof value === 'string') {
    return value.split(',').map(s => s.trim()).filter(Boolean)
  }
  return undefined
}

// ---------------------------------------------------------------- 统计

export interface ObsidianVaultStats {
  name?: string
  path?: string
  files?: number
  folders?: number
  size?: number
  /** markdown 文件数（`files ext=md total`） */
  markdownFiles?: number
  tags?: number
  unresolved?: number
  orphans?: number
  deadends?: number
}

/**
 * 库的统计概览。
 *
 * 每项都是**独立一次 CLI 调用**，因为 Obsidian 的 `vault` 命令只给 name/path/files/folders/size，
 * 标签数、未解析链接数、孤立页数各要各的命令。
 */
export function getVaultStats(options: ObsidianQueryOptions = {}): ObsidianResult<ObsidianVaultStats> {
  const run = runObsidianCli(['vault'], options)
  if (!run.ok) return fail(run)

  const stats: ObsidianVaultStats = {}
  for (const line of run.stdout.split(/\r?\n/)) {
    const [key, ...rest] = line.split('\t')
    const value = rest.join('\t')
    if (key === 'name') stats.name = value
    else if (key === 'path') stats.path = value
    else if (key === 'files') stats.files = Number(value)
    else if (key === 'folders') stats.folders = Number(value)
    else if (key === 'size') stats.size = Number(value)
  }

  // 次要统计：各一次调用，失败不拖垮整体（某项拿不到就留空）。
  const md = runObsidianCli(['files', 'ext=md', 'total'], options)
  if (md.ok && /^\d+$/.test(md.stdout.trim())) stats.markdownFiles = Number(md.stdout.trim())

  const tags = runObsidianCli(['tags', 'total'], options)
  if (tags.ok && /^\d+$/.test(tags.stdout.trim())) stats.tags = Number(tags.stdout.trim())

  const unresolved = runObsidianCli(['unresolved', 'total'], options)
  if (unresolved.ok && /^\d+$/.test(unresolved.stdout.trim())) stats.unresolved = Number(unresolved.stdout.trim())

  const orphans = runObsidianCli(['orphans', 'total'], options)
  if (orphans.ok && /^\d+$/.test(orphans.stdout.trim())) stats.orphans = Number(orphans.stdout.trim())

  const deadends = runObsidianCli(['deadends', 'total'], options)
  if (deadends.ok && /^\d+$/.test(deadends.stdout.trim())) stats.deadends = Number(deadends.stdout.trim())

  return { success: true, data: stats }
}

/** 库里的标签列表（带计数）。 */
export function getTags(options: ObsidianQueryOptions = {}): ObsidianResult<{ tag: string; count?: number }[]> {
  const run = runObsidianCli(['tags', 'format=json', 'counts', 'sort=count'], options)
  if (!run.ok) return fail(run)
  const parsed = parseCliJson<{ tag?: string; count?: unknown }[]>(run.stdout)
  if (!parsed) return { success: true, data: [] }
  return {
    success: true,
    data: parsed
      .filter(item => item?.tag)
      .map(item => ({ tag: String(item.tag), count: toCount(item.count) })),
  }
}

// ---------------------------------------------------------------- 任务

export interface ObsidianTask {
  path?: string
  line?: number
  text?: string
  status?: string
  checked?: boolean
}

/** 库里的待办任务。 */
export function getTasks(options: ObsidianQueryOptions & { todo?: boolean; path?: string } = {}): ObsidianResult<ObsidianTask[]> {
  const args = ['tasks', 'format=json']
  if (options.todo) args.push('todo')
  if (options.path) args.push(`path=${options.path}`)
  const run = runObsidianCli(args, options)
  if (!run.ok) return fail(run)
  if (isEmptyResult(run.stdout)) return { success: true, data: [] }
  const parsed = parseCliJson<ObsidianTask[]>(run.stdout)
  if (!parsed) return { success: true, data: [] }
  return { success: true, data: parsed }
}

// ---------------------------------------------------------------- 库清单

export interface ObsidianVaultInfo {
  name: string
  path: string
}

/**
 * 已注册的库清单。
 *
 * 优先问 Obsidian 自己（`vaults verbose`），问不到再读 `obsidian.json` ——
 * 后者在 Obsidian 没运行时也能用，是「没开 Obsidian 时也能告诉用户有哪些库」的兜底。
 */
export function listVaults(options: ObsidianQueryOptions = {}): ObsidianResult<ObsidianVaultInfo[]> {
  const run = runObsidianCli(['vaults', 'verbose'], options)
  if (run.ok) {
    const rows = run.stdout.split(/\r?\n/).filter(Boolean).map(line => line.split('\t'))
    const vaults = rows
      .filter(row => row.length >= 2)
      .map(row => ({ name: row[0], path: row[1] }))
    if (vaults.length) return { success: true, data: vaults }
  }
  return { success: true, data: listRegisteredVaults() }
}

/** Obsidian 是不是装着、能不能用。 */
export function checkAvailable(): { installed: boolean; executable: string | null } {
  const executable = findObsidianExecutable()
  return { installed: !!executable, executable }
}

// ---------------------------------------------------------------- 概念页

export interface ObsidianConcept {
  title: string
  path: string
  /** 该页在库内的引用数（`links` 命令不给这个数，来自 frontmatter 或索引表） */
  refs?: number
  verified?: boolean
  summary?: string
}

/**
 * 列出库里的概念页。
 *
 * 走 Obsidian 的 `files`（拿全量 markdown 路径）而不是自己扫目录 ——
 * 这样**用户自己在 Obsidian 里加的页也会被算进来**，不必非得是 `wiki compile` 生成的。
 * 概念页的判定用路径特征（`Wiki/Concepts`、`Chat/Concepts`），其余当普通笔记。
 */
export function listConcepts(options: ObsidianQueryOptions = {}): ObsidianResult<ObsidianConcept[]> {
  const run = runObsidianCli(['files', 'ext=md'], options)
  if (!run.ok) return fail(run)
  if (isEmptyResult(run.stdout)) return { success: true, data: [] }

  const paths = run.stdout.split(/\r?\n/).filter(Boolean)
  const concepts: ObsidianConcept[] = []
  for (const path of paths) {
    const isConcept = /(^|\/)(Wiki|Chat)\/Concepts\//.test(path)
    if (!isConcept) continue
    const title = path.split('/').pop()?.replace(/\.md$/, '') ?? path
    concepts.push({ title, path })
  }
  return { success: true, data: concepts }
}

/**
 * 读概念页的元信息（frontmatter + 正文摘要）。
 *
 * `summary` 取正文首个非空段落并截断 —— 概念页的正文本身就是摘要，
 * 再让模型重写一遍是浪费。
 */
export function readConcept(path: string, options: ObsidianQueryOptions = {}): ObsidianResult<ObsidianConcept & { content: string }> {
  const note = readNote(path, options)
  if (!note.success || !note.data) return { success: false, error: note.error }
  const props = readProperties(path, options)
  const frontmatter = props.success && props.data ? props.data : {}

  const body = note.data.content
    .replace(/^---[\s\S]*?---\s*/, '')
    .trim()
  const summary = body.split(/\n\s*\n/).map(p => p.trim()).find(p => p.length > 0)?.slice(0, 300)

  return {
    success: true,
    data: {
      title: String(frontmatter.title ?? path.split('/').pop()?.replace(/\.md$/, '') ?? path),
      path,
      verified: frontmatter.verified === true || frontmatter.verified === 'true',
      summary,
      content: body,
    },
  }
}

/**
 * 用 `eval` 拿一个聚合数字（Obsidian 没有的统计口径）。
 *
 * 只做只读、纯计算的表达式 —— 这是 CLI 的 `eval` 能安全用在这条路上的前提。
 * 返回值必须能 JSON 序列化，否则 Obsidian 会回一句「无法序列化」。
 */
export function evaluateAggregate(expression: string, options: ObsidianQueryOptions = {}): ObsidianResult<unknown> {
  const run = runObsidianCli(['eval', `code=${expression}`], options)
  if (!run.ok) return fail(run)
  const value = stripEvalPrefix(run.stdout)
  const parsed = parseCliJson<unknown>(value)
  return { success: true, data: parsed !== null ? parsed : value }
}
