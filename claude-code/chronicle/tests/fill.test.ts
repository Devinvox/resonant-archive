import { test, expect } from 'claude-code/testing'
import { fillLine } from '../hooks/register'
import { rig, usage } from './harness'

// fillLine reads $.session.usage(); a stand-in $ is enough for the pure cases.
const at = (context: Record<string, number | undefined>) => ({ session: { usage: async () => ({ context }) } }) as any

test('fillLine: nothing measured, no window', async () => {
  expect(await fillLine(at({ tokens: 0, window: 0 }))).toBe('not measured yet')
})

test('fillLine: window known but nothing measured yet', async () => {
  expect(await fillLine(at({ tokens: 0, window: 200_000 }))).toBe('200k window, not measured yet')
})

test('fillLine: flags start at 75 and 90, not before', async () => {
  const line = (p: number) => fillLine(at({ tokens: p * 1000, window: 100_000, percent: p }))
  expect(await line(74)).toBe('74k of 100k (74%)')
  expect(await line(75)).toBe('75k of 100k (75%) · squish soon')
  expect(await line(89)).toBe('89k of 100k (89%) · squish soon')
  expect(await line(90)).toBe('90k of 100k (90%) · SQUISH NOW')
  expect(await line(100)).toBe('100k of 100k (100%) · SQUISH NOW')
  expect(await line(120)).toBe('120k of 100k (120%) · SQUISH NOW')
})

test('fillLine: a 1M window is written 1M, and a missing percent is computed', async () => {
  expect(await fillLine(at({ tokens: 142_000, window: 1_000_000 }))).toBe('142k of 1M (14%)')
  expect(await fillLine(at({ tokens: 800_000, window: 1_000_000 }))).toBe('800k of 1M (80%) · squish soon')
})

test('fillLine: custom thresholds are honoured', async () => {
  expect(await fillLine(at({ tokens: 50_000, window: 100_000, percent: 50 }), 40, 60)).toBe('50k of 100k (50%) · squish soon')
  expect(await fillLine(at({ tokens: 60_000, window: 100_000, percent: 60 }), 40, 60)).toBe('60k of 100k (60%) · SQUISH NOW')
})

const submit = async ($: any, origin?: unknown) => $.prompt.submit({ text: 'hello', ...(origin ? { origin } : {}) })

function capture(on: any) {
  const seen: { context?: readonly string[] }[] = []
  on('prompt.submit', async (_$: any, e: any) => { seen.push({ context: e.context }); return { text: e.text } })
  return seen
}

test('prompt.submit: the line is appended to existing context, for the composer', async ($, on) => {
  rig(on, { usage: usage(760_000, 1_000_000, 76) })
  const seen = capture(on)
  await $.prompt.submit({ text: 'hello', origin: { kind: 'composer' }, context: ['earlier note'] } as any)
  expect(seen[0]?.context).toEqual(['earlier note', '[context · 760k of 1M (76%) · squish soon]'])
})

test('prompt.submit: bridge and origin-less prompts get the line', async ($, on) => {
  rig(on, { usage: usage(100_000, 1_000_000, 10) })
  const seen = capture(on)
  await submit($, { kind: 'bridge' })
  await submit($)
  expect(seen.map(s => s.context)).toEqual([['[context · 100k of 1M (10%)]'], ['[context · 100k of 1M (10%)]']])
})

test('prompt.submit: other origins are left alone', async ($, on) => {
  rig(on, { usage: usage(100_000, 1_000_000, 10) })
  const seen = capture(on)
  await submit($, { kind: 'sdk' })
  await submit($, { kind: 'scheduled-trigger' })
  expect(seen.map(s => s.context)).toEqual([undefined, undefined])
})

test('prompt.submit: an unreadable fill passes the prompt through untouched', async ($, on) => {
  const r = rig(on)
  r.state.failUsage = true
  const seen = capture(on)
  const out = await submit($, { kind: 'composer' })
  expect(out.text).toBe('hello')
  expect(seen[0]?.context).toBeUndefined()
})

test('contextLine.enabled false: nothing is added', async ($, on) => {
  rig(on, { usage: usage(900_000, 1_000_000, 90), config: { contextLine: { enabled: false } } })
  const seen = capture(on)
  await submit($, { kind: 'composer' })
  expect(seen[0]?.context).toBeUndefined()
})

test('contextLine warnAt and urgentAt from chronicle.json are used', async ($, on) => {
  rig(on, { usage: usage(50_000, 100_000, 50), config: { contextLine: { warnAt: 40, urgentAt: 50 } } })
  const seen = capture(on)
  await submit($, { kind: 'composer' })
  expect(seen[0]?.context).toEqual(['[context · 50k of 100k (50%) · SQUISH NOW]'])
})
