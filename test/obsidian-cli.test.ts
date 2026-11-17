/**
 * Obsidian CLI 封装层的回归测试。
 *
 * 这一层要挡住的是**静默给错答案**的那几种失败，而不是崩溃：
 *
 * 1. **Obsidian 的错误打在 stdout，退出码是 0。** 只看退出码的话，一次
 *    "文件不存在"会被当成成功，把 `Error: File "X" not found.` 当成笔记正文返回。
 *    这条钉的是「错误文本绝不能被当成内容」。
 * 2. **`vault=` 放错位置会被静默忽略**，然后返回**当前活动库**的数据——
 *    用户以为查的是 A 库，拿到的是 B 库的。这条钉的是参数位置。
 * 3. **Obsidian 没在运行时**，CLI 客户端退化成"启动一个新实例"，stdout 里只剩
 *    Electron 启动日志，命令结果根本收不到。这条钉的是那个冷启动特征串。
 * 4. **`counts`/`verbose` 返回的是字符串**（`"2"`、`"a.md, b.md"`），不是数字和数组。
 *    当成数字用得到 NaN，当成数组用得到长度 1 的假数组。
 *
 * 测试全部用**注入的假执行器**，不真的调 Obsidian —— 所以它们在 CI 上也跑得动。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import {
  isCliError,
  stripErrorPrefix,
  looksLikeColdStart,
  parseCliJson,
  stripEvalPrefix,
  isEmptyResult,
  parseTsv,
  runObsidianCli,
  BROKEN_CLI_COMMANDS,
} from '../src/services/obsidianCli.js'
import {
  getTags,
  getUnresolvedLinks,
  searchVault,
  listVaults,
} from '../src/services/obsidianService.js'

/** 假执行器：按调用次序返回预设输出。 */
function fakeExec(outputs: Array<{ stdout?: string; stderr?: string; code?: number | null }>) {
  const calls: string[][] = []
  let index = 0
  const exec = (_exe: string, args: string[], _timeout: number) => {
    calls.push(args)
    const spec = outputs[Math.min(index++, outputs.length - 1)] ?? {}
    return {
      stdout: spec.stdout ?? '',
      stderr: spec.stderr ?? '',
      code: spec.code === undefined ? 0 : spec.code,
    }
  }
  return { exec, calls }
}

test('Obsidian 的错误以 Error: 前缀出现在 stdout，退出码仍是 0 —— 必须被认出来', () => {
  // 这是实测到的真实输出（1.15.1）：读一个不存在的文件。
  const stdout = 'Error: File "Wiki/Nope.md" not found.'
  assert.equal(isCliError(stdout), true, '这条 stdout 是错误，不能被当成正文')
  assert.equal(stripErrorPrefix(stdout), 'File "Wiki/Nope.md" not found.')
})

test('正文里恰好出现 "Error:" 不算错误', () => {
  // 笔记正文完全可能以 "Error:" 开头。判据要靠前缀而不是包含。
  assert.equal(isCliError('Error handling in the plugin was fine'), false)
  assert.equal(isCliError('# Error handling guide\n\nsome text'), false)
  assert.equal(isCliError('  Error: real one'), true, '前导空白不该影响判定')
})

test('Obsidian 没在运行时的冷启动特征会被认出来', () => {
  assert.equal(looksLikeColdStart('Loaded main app package\nsomething'), true)
  assert.equal(looksLikeColdStart('Wiki/00-Overview.md'), false)
})

test('`vault=` 必须放在子命令前面 —— 放后面会被静默忽略', () => {
  // 实测：`vault info=name vault=nonexistent-vault` 返回的是当前库的名字，
  // 而 `vault=nonexistent-vault vault info=name` 才报 "Vault not found."。
  // 前一种错法不报错、只给错数据 —— 比报错更危险，所以这里钉住参数顺序。
  const { exec, calls } = fakeExec([{ stdout: 'wechat-vault' }])
  runObsidianCli(['vault', 'info=name'], { vault: 'wechat-vault', exec })
  assert.deepEqual(calls[0], ['vault=wechat-vault', 'vault', 'info=name'],
    'vault= 必须在子命令前面，否则 Obsidian 会忽略它并返回当前活动库')
})

test('CLI 报错时 exit code 仍是 0，但结果必须是失败的', () => {
  const { exec } = fakeExec([{ stdout: 'Error: File "X" not found.', code: 0 }])
  const run = runObsidianCli(['read', 'path=X'], { exec })
  assert.equal(run.ok, false, '退出码 0 不代表成功——Obsidian 把错误打在 stdout 上')
  assert.match(run.error ?? '', /not found/)
})

test('退出码非 0 时报退出码，并带上 stdout 里的线索', () => {
  const { exec } = fakeExec([{ stdout: 'crash detail', code: -1 }])
  const run = runObsidianCli(['search:context', 'query=x'], { exec })
  assert.equal(run.ok, false)
  assert.match(run.error ?? '', /退出码 -1/)
})

test('冷启动时给出「请先打开 Obsidian」而不是一句空失败', () => {
  const { exec } = fakeExec([{ stdout: 'Loaded main app package\n[main] something' }])
  const run = runObsidianCli(['search', 'query=x'], { exec })
  assert.equal(run.ok, false)
  assert.equal(run.needsRunning, true, '这条失败有可行动的下一步，必须单独标出来')
})

test('search:context 被列进已知坏命令 —— 转发它只会拿到空结果', () => {
  assert.ok(BROKEN_CLI_COMMANDS.has('search:context'),
    '1.15.1 上 search:context 以退出码 -1 无输出地崩掉')
})

test('parseCliJson 认得整份 JSON 与最后一行 JSON', () => {
  assert.deepEqual(parseCliJson('["a","b"]'), ['a', 'b'])
  assert.deepEqual(parseCliJson('loading...\n{"ok":true}'), { ok: true })
  assert.deepEqual(parseCliJson('[1,2,3]'), [1, 2, 3])
  assert.equal(parseCliJson('not json'), null)
  assert.equal(parseCliJson(''), null)
})

test('eval 的输出带 `=> ` 前缀，剥掉它', () => {
  assert.equal(stripEvalPrefix('=> 21'), '21')
  assert.equal(stripEvalPrefix('=> ["a"]'), '["a"]')
  assert.equal(stripEvalPrefix('21'), '21')
})

test('「没有结果」要与「有结果」分开 —— 它不是错误', () => {
  assert.equal(isEmptyResult('No matches found.'), true)
  assert.equal(isEmptyResult('No tasks found.'), true)
  assert.equal(isEmptyResult(''), true)
  assert.equal(isEmptyResult('Wiki/00-Overview.md'), false)
})

test('TSV 解析：空行不算行', () => {
  assert.deepEqual(parseTsv('a\tb\nc\td\n'), [['a', 'b'], ['c', 'd']])
  assert.deepEqual(parseTsv('single'), [['single']])
})

test('search 的 JSON 输出被解析成路径列表', () => {
  const { exec } = fakeExec([{ stdout: '["Wiki/00-Overview.md","Wiki/Concepts/陈陈.md"]' }])
  const result = searchVault('插件', { exec })
  assert.equal(result.success, true)
  assert.deepEqual(result.data?.map(hit => hit.path),
    ['Wiki/00-Overview.md', 'Wiki/Concepts/陈陈.md'])
})

test('search 无匹配时返回空数组，不是错误', () => {
  const { exec } = fakeExec([{ stdout: 'No matches found.' }])
  const result = searchVault('zzzz', { exec })
  assert.equal(result.success, true, '「没搜到」是正常结果')
  assert.deepEqual(result.data, [])
})

test('unresolved 的 count/sources 是字符串 —— 必须归一化成数字和数组', () => {
  // Obsidian 实测返回：{"link":"白名单","count":"2","sources":"a.md, b.md"}
  // 当成数字用得到 NaN；当成数组用得到长度 1 的假数组。
  const { exec } = fakeExec([{
    stdout: JSON.stringify([
      { link: '白名单', count: '2', sources: 'Wiki/Concepts/农商惠支付小徐.md, Wiki/Concepts/岁月欢喜.md' },
      { link: '报错排查', count: '1', sources: 'Wiki/Concepts/罗小小.md' },
    ]),
  }])
  const result = getUnresolvedLinks({ exec })
  assert.equal(result.success, true)
  assert.equal(result.data?.[0].count, 2, 'count 是字符串 "2"，要变成数字 2')
  assert.equal(result.data?.[0].sources?.length, 2, 'sources 是逗号分隔的字符串，要拆成数组')
  assert.deepEqual(result.data?.[0].sources,
    ['Wiki/Concepts/农商惠支付小徐.md', 'Wiki/Concepts/岁月欢喜.md'])
})

test('tags 的 count 同样是字符串', () => {
  const { exec } = fakeExec([{ stdout: JSON.stringify([{ tag: '#知识/概念', count: '20' }]) }])
  const result = getTags({ exec })
  assert.equal(result.data?.[0].count, 20)
})

test('Obsidian 问不到库时退回读 obsidian.json 的注册表', () => {
  // listVaults 优先问 Obsidian；问不到（没运行）才读本地注册表。
  const { exec } = fakeExec([{ stdout: '' }])
  const result = listVaults({ exec })
  assert.equal(result.success, true)
  assert.ok(Array.isArray(result.data))
})

test('命令找不到 Obsidian 时给出可行动的提示', () => {
  // 显式把可执行文件指向一个不存在的位置，强制"找不到"这条分支。
  const previous = process.env.WEFLOW_OBSIDIAN_EXE
  process.env.WEFLOW_OBSIDIAN_EXE = 'E:/definitely/not/here/Obsidian.exe'
  try {
    const run = runObsidianCli(['version'], {})
    if (!run.ok && run.error?.includes('找不到 Obsidian')) {
      assert.match(run.error, /WEFLOW_OBSIDIAN_EXE/, '要说清还有环境变量这条出路')
    }
  } finally {
    if (previous === undefined) delete process.env.WEFLOW_OBSIDIAN_EXE
    else process.env.WEFLOW_OBSIDIAN_EXE = previous
  }
})

test('执行器抛错时不能被读成「库里什么都没有」', () => {
  // 这条钉的是一个**静默的假成功**：执行失败时 stdout 是空的，
  // 而"空输出"在 Obsidian 的语义里是「没有匹配」—— 于是一次失败会被读成
  // "库里没有未解析链接"。真实场景是 Obsidian 没装 / 可执行文件找不到，
  // 评测场景是桩说"这条用例不该走到它"。两种都必须是**失败**，不是空结果。
  const exec = () => { throw new Error('评测没有给 Obsidian 的 unresolved 准备回放') }
  const run = runObsidianCli(['unresolved', 'format=json'], { exec })
  assert.equal(run.ok, false, '执行失败必须报失败')
  assert.match(run.error ?? '', /准备回放/, '错误文本要说清是什么失败')
})

test('命令行不存在时的错误不会混进 stdout 当成正文', () => {
  const { exec } = fakeExec([{ stdout: 'Error: Command "nonexistentcmd" not found. It may require a plugin to be enabled.' }])
  const run = runObsidianCli(['nonexistentcmd'], { exec })
  assert.equal(run.ok, false)
  assert.match(run.error ?? '', /not found/)
  assert.equal(isCliError(run.stdout), true, '原始 stdout 仍然带着 Error: 前缀，可供上层再判一次')
})
