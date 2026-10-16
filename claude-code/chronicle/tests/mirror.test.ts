import { test, expect } from 'claude-code/testing'
import { rig, settle } from './harness'

let n = 0
const uuid = () => `00000000-0000-4000-8000-${String(++n).padStart(12, '0')}`
const row = (text: string, role: 'user' | 'assistant' = 'user', door?: string) => ({
  message: { type: role, role, content: [{ type: 'text', text }] },
  door: door ?? (role === 'user' ? 'prompt' : 'response'),
  origin: role === 'user' ? { kind: 'composer' } : { kind: 'model', model: 'm' },
  uuid: uuid(),
}) as any

const ROOT = '/home/tester/.chronicle/archive'
// The appends the mirror made: { file, rows } in order.
const writes = (r: ReturnType<typeof rig>) => r.runs
  .filter(x => x.argv[0] === 'sh' && String(x.argv[2]).includes('cat >> '))
  .map(x => ({ file: String(x.argv[5]), rows: String(x.stdin ?? '').split('\n').filter(Boolean).map(l => JSON.parse(l)) }))
const start = ($: any) => $.session.start({ source: 'startup', cwd: '/work' } as any)

test('a row lands in <root>/<day>/<sid>.jsonl, written 0700/0600', async ($, on) => {
  const r = rig(on, { config: { timezone: 'UTC' }, now: Date.UTC(2026, 0, 5, 12, 0, 0) })
  await start($)
  await $.session.append(row('hello there'))
  await r.clock.advance(31_000); await settle()
  const w = writes(r)
  expect(w.length).toBe(1)
  expect(w[0]!.file).toBe(`${ROOT}/2026-01-05/sess-1.jsonl`)
  expect(w[0]!.rows[0]).toMatchObject({ sid: 'sess-1', door: 'prompt', role: 'user', text: 'hello there' })
  const script = String(r.runs.find(x => String(x.argv[2]).includes('cat >> '))!.argv[2])
  expect(script).toContain('umask 077')
  expect(script).toContain('mkdir -p -m 700')
})

test('the day folder follows the configured zone', async ($, on) => {
  const at = Date.UTC(2026, 0, 5, 23, 30, 0)
  const auckland = rig(on, { config: { timezone: 'Pacific/Auckland' }, now: at })
  await start($)
  await $.session.append(row('late'))
  await auckland.clock.advance(31_000); await settle()
  expect(writes(auckland)[0]!.file).toBe(`${ROOT}/2026-01-06/sess-1.jsonl`)
})

test('the day folder is stable across the end of daylight time', async ($, on) => {
  // 23:30 UTC on 24 Oct is 00:30 BST on the 25th; 23:30 UTC on the 25th is 23:30 GMT on the 25th: one day.
  const r = rig(on, { config: { timezone: 'Europe/London' }, now: Date.UTC(2026, 9, 24, 23, 30, 0) })
  await start($)
  await $.session.append(row('before the change'))
  await r.clock.advance(24 * 60 * 60 * 1000)
  await $.session.append(row('after the change'))
  await r.clock.advance(31_000); await settle()
  const days = new Set(writes(r).map(x => x.file.split('/').slice(-2)[0]))
  expect([...days]).toEqual(['2026-10-25'])
})

test('the zone is written once into the archive and wins over a later system zone', async ($, on) => {
  const r = rig(on, { config: { timezone: 'Asia/Tokyo' }, now: Date.UTC(2026, 0, 5, 20, 0, 0) })
  await start($)
  await $.session.append(row('one'))
  await $.session.append(row('two'))
  await r.clock.advance(31_000); await settle()
  const marker = r.runs.filter(x => String(x.argv[2]).includes('cat > '))
  expect(marker.length).toBe(1)
  expect(JSON.parse(marker[0]!.stdin!)).toEqual({ timezone: 'Asia/Tokyo', format: 1 })
  expect(writes(r)[0]!.file).toBe(`${ROOT}/2026-01-06/sess-1.jsonl`)
})

test('a zone already recorded in the archive is used, and not written again', async ($, on) => {
  const r = rig(on, { files: { [`${ROOT}/.chronicle.json`]: '{"timezone":"Asia/Tokyo","format":1}' }, now: Date.UTC(2026, 0, 5, 20, 0, 0) })
  await start($)
  await $.session.append(row('one'))
  await r.clock.advance(31_000); await settle()
  expect(r.runs.some(x => String(x.argv[2]).includes('cat > '))).toBe(false)
  expect(writes(r)[0]!.file).toBe(`${ROOT}/2026-01-06/sess-1.jsonl`)
})

test('archiveRoot in chronicle.json is used, with ~ expanded', async ($, on) => {
  const r = rig(on, { config: { archiveRoot: '~/notes/mirror', timezone: 'UTC' } })
  await start($)
  await $.session.append(row('x'))
  await r.clock.advance(31_000); await settle()
  expect(writes(r)[0]!.file).toStartWith('/home/tester/notes/mirror/2026-01-05/')
})

test('/clear: the next row is written under the new session id, after a session_fresh mark', async ($, on) => {
  const r = rig(on, { config: { timezone: 'UTC' } })
  await start($)
  await $.session.append(row('first session'))
  await ($ as any).session.end({ reason: 'clear', sessionId: 'sess-1', resume: { id: 'sess-1' } })
  r.state.sid = 'sess-2'
  await $.session.append(row('second session'))
  await r.clock.advance(31_000); await settle()
  const w = writes(r)
  const second = w.filter(x => x.file.endsWith('/sess-2.jsonl')).flatMap(x => x.rows)
  expect(w.some(x => x.file.endsWith('/sess-1.jsonl'))).toBe(true)
  expect(second[0]).toMatchObject({ event: 'session_fresh', sid: 'sess-2', how: 'clear' })
  expect(second[1]).toMatchObject({ text: 'second session', sid: 'sess-2' })
})

test('a failed flush requeues in order, warns after three failures, and recovers', async ($, on) => {
  let failing = true
  const r = rig(on, { config: { timezone: 'UTC' }, proc: argv => (argv[0] === 'sh' && failing && String(argv[2]).includes('cat >> ') ? { exitCode: 1, stderr: 'disk full' } : undefined) })
  await start($)
  await $.session.append(row('one'))
  await $.session.append(row('two'))
  for (let i = 0; i < 3; i++) { await r.clock.advance(31_000); await settle() }
  expect(r.logs.some(l => l.includes('archive mirror has failed 3 times in a row (disk full)'))).toBe(true)
  failing = false
  await $.session.append(row('three'))
  await r.clock.advance(31_000); await settle()
  expect(r.logs.some(l => l.includes('archive mirror recovered'))).toBe(true)
  const texts = writes(r).flatMap(x => x.rows.map(q => q.text)).filter(Boolean)
  expect(texts.slice(-3)).toEqual(['one', 'two', 'three'])
})

test('a full buffer drops rows and says so in the next flush', async ($, on) => {
  let failing = true
  const r = rig(on, { config: { timezone: 'UTC' }, proc: argv => (argv[0] === 'sh' && failing && String(argv[2]).includes('cat >> ') ? { exitCode: 1, stderr: 'no' } : undefined) })
  await start($)
  const big = 'word '.repeat(13_000)
  for (let i = 0; i < 90; i++) await $.session.append(row(`${i} ${big}`))
  failing = false
  await r.clock.advance(31_000); await settle(80)
  const all = writes(r).flatMap(x => x.rows)
  const drop = all.find(q => q.event === 'mirror_dropped')
  expect(drop).toBeDefined()
  expect(drop.count).toBeGreaterThan(0)
})

test('the /exit farewell and anything after it is not mirrored', async ($, on) => {
  const r = rig(on, { config: { timezone: 'UTC' } })
  await start($)
  await $.session.append(row('real work'))
  await $.session.append(row('<command-name>/exit</command-name>', 'user', 'command'))
  await $.session.append(row('Goodbye!', 'assistant'))
  await r.clock.advance(31_000); await settle()
  expect(writes(r).flatMap(x => x.rows.map(q => q.text))).toEqual(['real work'])
})

test('mirror.enabled false: nothing is written', async ($, on) => {
  const r = rig(on, { config: { mirror: { enabled: false } } })
  await start($)
  await $.session.append(row('private'))
  await r.clock.advance(31_000); await settle()
  expect(writes(r)).toEqual([])
})

test('a queued mid-turn prompt is kept as the person\'s words', async ($, on) => {
  const r = rig(on, { config: { timezone: 'UTC' } })
  await start($)
  const q = {
    message: { type: 'attachment', name: 'queued_command', content: [{ type: 'text', text: '<system-reminder>\nThe user sent a new message while you were working:\nplease also check the logs\n\nThis is how Claude Code surfaces messages the user sends mid-turn.\n</system-reminder>' }] },
    door: 'queue', origin: { kind: 'composer' }, uuid: uuid(),
  } as any
  await $.session.append(q)
  await r.clock.advance(31_000); await settle()
  expect(writes(r)[0]!.rows[0]).toMatchObject({ queued: 'prompt', role: 'user', text: 'please also check the logs' })
})

test('the secret a row carries never reaches the file', async ($, on) => {
  const r = rig(on, { config: { timezone: 'UTC' } })
  await start($)
  await $.session.append(row('my key is sk-ant-' + 'Z'.repeat(30)))
  await r.clock.advance(31_000); await settle()
  expect(JSON.stringify(writes(r))).not.toContain('ZZZZZZ')
})
