import { test, expect } from 'claude-code/testing'
import { splitPoint, keptMessages, coreKept, turnOpen, steerOf, DEFAULT_STEER } from '../hooks/register'
import { rig, settle } from './harness'

type M = { role: 'user' | 'assistant'; text: string; toolUses: any[]; toolResults?: any[]; handle?: string }
const chars = (tokens: number) => 'x'.repeat(Math.round(tokens * 3.6))
const u = (tokens: number, handle?: string): M => ({ role: 'user', text: chars(tokens), toolUses: [], handle })
const a = (tokens: number, handle?: string): M => ({ role: 'assistant', text: chars(tokens), toolUses: [], handle })
const aTool = (id: string): M => ({ role: 'assistant', text: 'calling', toolUses: [{ tool_use_id: id, tool: 'Bash', input: { command: 'ls' }, text: '' }] })
const uResult = (id: string): M => ({ role: 'user', text: '', toolUses: [], toolResults: [{ tool_use_id: id, text: 'out' }] })

// Ten turns of 2000 tokens each (a 1000-token prompt, a 1000-token reply).
const turns = (n = 10): M[] => Array.from({ length: n }, () => [u(1000), a(1000)]).flat()

test('splitPoint: picks the smallest valid tail that reaches the target', () => {
  expect(splitPoint(turns(), 3000, 5000)).toBe(16)
})

test('splitPoint: the cap wins over the target when the target would pass it', () => {
  expect(splitPoint(turns(), 3000, 3500)).toBe(18)
})

test('splitPoint: no valid cut gives -1', () => {
  expect(splitPoint([u(10), aTool('t1'), uResult('t1'), aTool('t2'), uResult('t2'), a(10)], 1, 1_000_000)).toBe(-1)
  expect(splitPoint([], 1, 100)).toBe(-1)
})

test('splitPoint: a tool pair that straddles the cut is never cut through', () => {
  // u0 a1 u2 | a3(use t1) u4(prompt-shaped) a5 u6(result t1) a7 : a cut at 4 would orphan t1
  const msgs: M[] = [u(100), a(100), u(100), aTool('t1'), u(100), a(100), uResult('t1'), a(100)]
  const cut = splitPoint(msgs, 1, 1_000_000)
  expect(cut).not.toBe(4)
  expect(cut === 2 || cut === -1).toBe(true)
})

test('splitPoint: needs a text-only assistant message right before the cut', () => {
  const msgs: M[] = [u(100), aTool('t1'), uResult('t1'), u(100), a(100)]
  expect(splitPoint(msgs, 1, 1_000_000)).toBe(-1) // the prompt at 3 follows a tool result, not an assistant reply
})

test('keptMessages: prompts keep their handle, other rows are rebuilt without one', () => {
  const kept = keptMessages([u(10, 'hp'), a(10, 'ha'), aTool('t1'), uResult('t1')], false)
  expect(kept[0]!.handle).toBe('hp')
  expect(kept[1]!.handle).toBeUndefined()
  expect(kept.length).toBe(4)
})

test('keptMessages: a thinking-only assistant row is left out; an open loop keeps its assistant handles', () => {
  const empty: M = { role: 'assistant', text: '', toolUses: [], handle: 'h' }
  expect(keptMessages([u(10), empty], false).length).toBe(1)
  const open = keptMessages([u(10), { ...aTool('t1'), handle: 'keep' }], true)
  expect(open[1]!.handle).toBe('keep')
})

test('turnOpen and coreKept', () => {
  expect(turnOpen([u(10), a(10)])).toBe(false)
  expect(turnOpen([u(10), aTool('t')])).toBe(true)
  expect(turnOpen([u(10), aTool('t'), uResult('t')])).toBe(true)
  expect(coreKept([u(10, 'h'), a(10, 'h2')])[1]!.handle).toBeUndefined()
  const odd = [null as any, u(1)]
  expect(coreKept(odd).length).toBe(2) // on any doubt the messages go up as core gave them
})

test('steerOf: persona in front, default or own steer, credentials rule always present', () => {
  expect(steerOf({ voice: '', steer: '' })).toBe(DEFAULT_STEER)
  expect(steerOf({ voice: 'I am Sam.', steer: '' }).startsWith('I am Sam.\n\n')).toBe(true)
  const own = steerOf({ voice: '', steer: 'Summarise briefly.' })
  expect(own.startsWith('Summarise briefly.')).toBe(true)
  expect(own).toContain('Never copy credentials')
})

const cfg = { compaction: { tailTokens: 3000, maxTailTokens: 5000, minOlderMessages: 2 } }
const compact = ($: any, messages: unknown[], trigger = 'manual', instructions?: string) => $.session.compact({ trigger, messages, ...(instructions ? { instructions } : {}) })

function summarizer(on: any) {
  const seen: { messages: any[]; instructions?: string }[] = []
  on('session.compact', async (_$: any, e: any) => {
    seen.push({ messages: e.messages, instructions: e.instructions })
    return { messages: [{ role: 'user', text: 'SUMMARY OF OLDER', toolUses: [] }] }
  })
  return seen
}

test('compaction: the older part is summarized with the steer, the recent tail comes through whole', async ($, on) => {
  const r = rig(on, { config: cfg, usage: { tokens: 20_000, window: 1_000_000 } })
  const seen = summarizer(on)
  const out: any = await compact($, turns(), 'manual', 'focus on the schema')
  expect(seen.length).toBe(1)
  expect(seen[0]!.messages.length).toBe(16)
  expect(seen[0]!.instructions).toContain('every exact identifier verbatim')
  expect(seen[0]!.instructions).toContain('focus on the schema')
  expect(seen[0]!.instructions).toContain('kept verbatim right after your summary')
  expect(out.messages[0].text).toBe('SUMMARY OF OLDER')
  expect(out.messages.length).toBe(1 + 4)
  void r
})

test('compaction: too little older material falls back to native compaction, still steered', async ($, on) => {
  rig(on, { config: { compaction: { tailTokens: 3000, maxTailTokens: 5000, minOlderMessages: 50 } }, usage: { tokens: 20_000, window: 1_000_000 } })
  const seen = summarizer(on)
  const msgs = turns()
  const out: any = await compact($, msgs)
  expect(seen[0]!.messages.length).toBe(msgs.length)
  expect(seen[0]!.instructions).toContain('exact identifier')
  expect(out.messages.length).toBe(1)
})

test('compaction: an error while planning falls back to native compaction with the steer', async ($, on) => {
  rig(on, { config: cfg, usage: { tokens: 20_000, window: 1_000_000 } })
  const seen = summarizer(on)
  const broken = { role: 'assistant', text: 'x', toolUses: [{ tool_use_id: 'b1', tool: 'Bash', input: { n: 10n }, text: '' }] } // JSON.stringify throws on a BigInt: the split planner fails
  const out: any = await compact($, [...turns(), broken])
  expect(seen.length).toBe(1)
  expect(seen[0]!.messages.length).toBe(21)
  expect(seen[0]!.instructions).toContain('exact identifier')
  expect(out.messages[0].text).toBe('SUMMARY OF OLDER')
})

test('compaction: precompute is skipped', async ($, on) => {
  rig(on, { config: cfg })
  const seen = summarizer(on)
  const out: any = await compact($, turns(), 'precompute')
  expect(out.skip).toBe('chronicle assembles at compaction time')
  expect(seen.length).toBe(0)
})

test('compaction: the default steer is neutral and a configured steer replaces it', async ($, on) => {
  rig(on, { config: { ...cfg, compaction: { ...cfg.compaction, voice: 'I am the pair-programming partner.', steer: 'Write it short.' } }, usage: { tokens: 20_000, window: 1_000_000 } })
  const seen = summarizer(on)
  await compact($, turns())
  expect(seen[0]!.instructions).toContain('I am the pair-programming partner.')
  expect(seen[0]!.instructions).toContain('Write it short.')
  expect(seen[0]!.instructions).toContain('Never copy credentials')
  expect(seen[0]!.instructions).not.toContain('every exact identifier verbatim')
})

test('compaction: a summary is mirrored as a compaction event, redacted', async ($, on) => {
  const r = rig(on, { config: { ...cfg, timezone: 'UTC' }, usage: { tokens: 20_000, window: 1_000_000 } })
  on('session.compact', async () => ({ messages: [{ role: 'user', text: 'SUMMARY with sk-ant-' + 'Q'.repeat(30), toolUses: [] }] }))
  await $.session.start({ source: 'startup', cwd: '/work' } as any)
  await compact($, turns())
  await r.clock.advance(31_000); await settle()
  const line = r.runs.map(x => x.stdin ?? '').find(s => s.includes('"event":"compaction"'))
  expect(line).toBeDefined()
  expect(line).toContain('"summarized":16')
  expect(line).not.toContain('QQQQQQ')
})
