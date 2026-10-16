import { test, expect } from 'claude-code/testing'
import { archiveArgv, archiveReply, recallSpec, toItem, parseQuery, parseBound, scoreItem, jsonEscaped } from '../hooks/register'
import { rig, settle } from './harness'

const ROOT = '/home/tester/.chronicle/archive'
const TOOL = 'mcp__chronicle__recall'
const call = ($: any, input: Record<string, unknown>): Promise<{ text?: string; result?: unknown }> => $.tool.call({ tool: TOOL, ...input })
const textOf = (o: any): string => String(o?.text ?? o?.result ?? '')
const start = ($: any) => $.session.start({ source: 'startup', cwd: '/work' } as any)

const MIRROR_LINE = JSON.stringify({ t: Date.UTC(2026, 0, 5, 12), sid: 'sess-1', uuid: 'aaaaaaaa-0000-4000-8000-000000000001', door: 'prompt', origin: 'composer', role: 'user', text: 'we chose the postgres schema for the archive' })

// A fake ripgrep over one mirror file.
const fakeRg = (argv: readonly string[]) => {
  if (argv[0] !== 'rg') return undefined
  if (argv.includes('--version')) return { exitCode: 0, stdout: 'ripgrep 14' }
  if (argv.includes('--files')) return { exitCode: 0, stdout: `${ROOT}/2026-01-05/sess-1.jsonl\n` }
  return { exitCode: 0, stdout: MIRROR_LINE + '\n' }
}

test('without an archive command the tool offers the session source only, and says so', async ($, on) => {
  const r = rig(on)
  await start($)
  const t = r.registered.find(x => x.name === 'recall')
  expect(t).toBeDefined()
  const props = Object.keys(t.inputSchema.properties)
  expect(props).toEqual(['query', 'from', 'to', 'session', 'kind', 'limit', 'around'])
  expect(t.description).toContain('No archive of older conversations is set up')
})

test('with an archive command the schema adds the archive fields', async ($, on) => {
  const r = rig(on, { config: { archive: { command: ['resonant-archive', 'recall'] } } })
  await start($)
  const t = r.registered.find(x => x.name === 'recall')
  expect(Object.keys(t.inputSchema.properties)).toContain('action')
  expect(Object.keys(t.inputSchema.properties)).toContain('source')
  expect(t.description).toContain('source archive')
})

test('no archive: source omitted searches the session mirror, and only rg is run', async ($, on) => {
  const r = rig(on, { config: { timezone: 'UTC' }, proc: fakeRg })
  await start($)
  const out = textOf(await call($, { query: 'postgres schema' }))
  expect(out).toContain('we chose the postgres schema')
  expect(out).toContain('uuid aaaaaaaa-0000-4000-8000-000000000001')
  const other = r.runs.filter(x => x.argv[0] !== 'rg' && x.argv[0] !== 'sh')
  expect(other).toEqual([])
})

test('no archive: asking for the archive explains how to set one up', async ($, on) => {
  const r = rig(on, { proc: fakeRg })
  await start($)
  const out = textOf(await call($, { source: 'archive', query: 'anything' }))
  expect(out).toContain('no archive is set up')
  expect(out).toContain('archive.command')
  expect(out).toContain(ROOT)
  expect(r.runs.some(x => x.argv[0] === 'resonant-archive')).toBe(false)
})

test('a mirror folder that does not exist yet is said plainly', async ($, on) => {
  rig(on, { proc: argv => (argv[0] === 'rg' && argv.includes('--files') ? { exitCode: 2, stderr: 'rg: /x: No such file or directory (os error 2)' } : undefined) })
  await start($)
  expect(textOf(await call($, { query: 'x' }))).toContain('does not exist yet')
})

test('ripgrep missing gives a clear message instead of a crash', async ($, on) => {
  rig(on, { proc: argv => (argv[0] === 'rg' ? { exitCode: 127, stderr: 'not found' } : undefined) })
  await start($)
  const out = textOf(await call($, { query: 'x' }))
  expect(out).toContain('ripgrep ("rg") was not found')
  expect(out).toContain('tools.rgPath')
})

test('tools.rgPath is the ripgrep that runs', async ($, on) => {
  const r = rig(on, { config: { tools: { rgPath: '/opt/bin/rg' } }, proc: argv => (argv[0] === '/opt/bin/rg' ? { exitCode: 0, stdout: '' } : undefined) })
  await start($)
  await call($, { query: 'x' })
  expect(r.runs.some(x => x.argv[0] === '/opt/bin/rg')).toBe(true)
  expect(r.runs.some(x => x.argv[0] === 'rg')).toBe(false)
})

test('mirror.enabled false: session recall says the mirror is off', async ($, on) => {
  rig(on, { config: { mirror: { enabled: false } } })
  await start($)
  expect(textOf(await call($, { query: 'x' }))).toContain('mirror is switched off')
})

test('with an archive command: the command, --json, the action and its options are run, and its text is returned', async ($, on) => {
  const r = rig(on, {
    config: { archive: { command: ['resonant-archive', 'recall'], timeoutMs: 5000 } },
    proc: argv => (argv[0] === 'resonant-archive' ? { exitCode: 0, stdout: JSON.stringify({ ok: true, text: 'recall hit one' }) } : undefined),
  })
  await start($)
  const out = textOf(await call($, { query: 'schema', collection: 'chatgpt,notes', limit: 5, hidden: true }))
  expect(out).toBe('recall hit one')
  const run = r.runs.find(x => x.argv[0] === 'resonant-archive')!
  expect(run.argv).toEqual(['resonant-archive', 'recall', '--json', 'search', '--source', 'chatgpt,notes', '--limit', '5', '--hidden', '--', 'schema'])
})

test('with an archive command: a non-zero exit with a message is reported with the stderr', async ($, on) => {
  rig(on, { config: { archive: { command: ['resonant-archive', 'recall'] } }, proc: argv => (argv[0] === 'resonant-archive' ? { exitCode: 2, stderr: 'database unreachable' } : undefined) })
  await start($)
  const out = textOf(await call($, { query: 'x' }))
  expect(out).toContain('recall failed: exit 2')
  expect(out).toContain('database unreachable')
})

test('with an archive command: a command that cannot run (or times out) is a message, not a throw', async ($, on) => {
  rig(on, { config: { archive: { command: ['resonant-archive', 'recall'], timeoutMs: 1000 } }, proc: argv => { if (argv[0] === 'resonant-archive') throw new Error('timed out'); return undefined } })
  await start($)
  expect(textOf(await call($, { query: 'x' }))).toContain('could not run or timed out after 1 s')
})

test('archive source is the default when a command is set; source session still reaches the mirror', async ($, on) => {
  const r = rig(on, { config: { timezone: 'UTC', archive: { command: ['ra'] } }, proc: argv => (argv[0] === 'ra' ? { exitCode: 0, stdout: 'from archive' } : fakeRg(argv)) })
  await start($)
  expect(textOf(await call($, { query: 'x' }))).toBe('from archive')
  expect(textOf(await call($, { query: 'postgres', source: 'session' }))).toContain('we chose the postgres schema')
  expect(textOf(await call($, { query: 'x', source: 'bogus' }))).toContain('is not archive or session')
  await settle()
  expect(r.runs.filter(x => x.argv[0] === 'ra').length).toBe(1)
})

// ---- pure ----

test('archiveArgv: every action', () => {
  expect(archiveArgv({ query: 'a b', collection: 'x', from: '2026-01-01', to: '2026-01-31', speaker: 'user', context: 'c', limit: 7, mode: 'fts', recent: true }))
    .toEqual(['search', '--source', 'x', '--from', '2026-01-01', '--to', '2026-01-31', '--speaker', 'user', '--context', 'c', '--limit', '7', '--mode', 'fts', '--recent', '--', 'a b'])
  expect(archiveArgv({ action: 'around', id: 'abc', before: 2, after: 3, hidden: 'true' })).toEqual(['around', '--before', '2', '--after', '3', '--hidden', '--', 'abc'])
  expect(archiveArgv({ around: 'abc' })).toEqual(['around', '--', 'abc'])
  expect(archiveArgv({ action: 'associate', id: 'abc', k: 5, recent: 'true' })).toEqual(['associate', '--k', '5', '--recent', '--', 'abc'])
  expect(archiveArgv({ action: 'overview', from: '2026-01-01', to: '2026-01-02', memories: 'only' })).toEqual(['overview', '--from', '2026-01-01', '--to', '2026-01-02', '--memories-only'])
  expect(archiveArgv({ action: 'overview', from: '2026-01-01', to: '2026-01-02', memories: 'none', collection: 's' })).toEqual(['overview', '--from', '2026-01-01', '--to', '2026-01-02', '--source', 's', '--no-memories'])
  expect(archiveArgv({ action: 'drill', memory: '#3' })).toEqual(['drill', '--', '#3'])
})

test('archiveArgv: bad input comes back as a message', () => {
  expect(archiveArgv({ action: 'search' })).toContain('search needs a query')
  expect(archiveArgv({ action: 'around' })).toContain('around needs id')
  expect(archiveArgv({ action: 'associate' })).toContain('associate needs id')
  expect(archiveArgv({ action: 'overview', from: '2026-01-01' })).toContain('overview needs from and to')
  expect(archiveArgv({ action: 'overview', from: 'a', to: 'b', memories: 'sometimes' })).toContain('is not include, only or none')
  expect(archiveArgv({ action: 'drill' })).toContain('drill needs memory')
  expect(archiveArgv({ action: 'delete' })).toContain('is not one of search')
})

test('archiveArgv: values pass as separate arguments after --, so a query cannot become a flag', () => {
  const a = archiveArgv({ query: '--hidden; rm -rf /' }) as string[]
  expect(a.slice(-2)).toEqual(['--', '--hidden; rm -rf /'])
})

test('archiveReply: JSON text, JSON error, plain text, cut text', () => {
  expect(archiveReply('{"ok":true,"text":" hi "}', false)).toBe('hi')
  expect(archiveReply('{"ok":false,"error":"no db"}', false)).toBe('recall failed: no db')
  expect(archiveReply('plain words', false)).toBe('plain words')
  expect(archiveReply('x'.repeat(13_000), false)).toContain('[output cut at 12000 characters]')
  expect(archiveReply('abc', true)).toContain('cut by the process limit')
})

test('recallSpec: neutral text, no private era names', () => {
  for (const has of [false, true]) {
    const s = recallSpec(has)
    expect(s.description.length).toBeGreaterThan(100)
    expect(JSON.stringify(s)).not.toMatch(/London/)
  }
})

test('parseQuery, parseBound, jsonEscaped', () => {
  expect(parseQuery('  Foo   bar foo ')).toEqual({ words: ['foo', 'bar'], phrase: undefined })
  expect(parseQuery('"exact phrase"')).toEqual({ words: ['exact', 'phrase'], phrase: 'exact phrase' })
  expect(parseBound('2026-01-05')).toEqual({ day: '2026-01-05' })
  expect(parseBound('')).toBeUndefined()
  expect(typeof parseBound('garbage')).toBe('string')
  const iso = parseBound('2026-01-05T10:00:00Z') as { day: string; ms: number }
  expect(iso.ms).toBe(Date.UTC(2026, 0, 5, 10))
  expect(jsonEscaped('a"b\\c\n')).toBe('a\\"b\\\\c\\n')
})

test('toItem and scoreItem', () => {
  const it = toItem(MIRROR_LINE)!
  expect(it.kind).toBe('row')
  expect(it.label).toBe('You')
  expect(scoreItem(it, ['postgres', 'schema'], undefined).score).toBe(3)
  expect(scoreItem(it, ['nothing'], undefined).score).toBe(0)
  expect(scoreItem(it, ['postgres'], 'we chose').score).toBe(2)
  expect(toItem('not json')).toBeUndefined()
  expect(toItem(JSON.stringify({ event: 'mirror_dropped', t: 1 }))).toBeUndefined()
  expect(toItem(JSON.stringify({ event: 'compaction', t: 1, summary: 's', trigger: 'manual' }))!.kind).toBe('summary')
  expect(toItem(JSON.stringify({ event: 'memory', t: 1, text: 'm', level: 1 }))!.kind).toBe('memory')
})

test('toItem: the assistant\'s own recall calls, current and legacy named, are not shown', () => {
  const mk = (tool: string) => JSON.stringify({ t: 1, sid: 's', uuid: 'u1', door: 'response', role: 'assistant', tools: [`${tool}({"query":"x"})`, 'Bash({})'] })
  expect(toItem(mk(TOOL))!.parts.map(p => p.s)).toEqual(['Bash({})'])
})
