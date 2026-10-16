import { test, expect } from 'claude-code/testing'
import { rig, usage, settle, cmd } from './harness'

test('/squish runs /compact once, passes the focus through, and answers at once', async ($, on) => {
  const r = rig(on, { usage: usage(500_000, 1_000_000, 50) })
  const out = await cmd($, 'squish', 'keep the schema work')
  expect(out).toEqual({ text: 'Squishing… 500k of 1M (50%)' })
  await r.clock.advance(10); await settle()
  expect(r.compacts).toEqual(['keep the schema work'])
})

test('/squish with no focus runs a plain /compact', async ($, on) => {
  const r = rig(on, { usage: usage(100_000, 1_000_000, 10) })
  await cmd($, 'squish', '')
  await r.clock.advance(10); await settle()
  expect(r.compacts).toEqual([''])
})

test('/squish status and ? print the fill and do not compact', async ($, on) => {
  const r = rig(on, { usage: usage(760_000, 1_000_000, 76) })
  expect(await cmd($, 'squish', 'status')).toEqual({ text: '760k of 1M (76%) · squish soon' })
  expect(await cmd($, 'squish', '?')).toEqual({ text: '760k of 1M (76%) · squish soon' })
  await r.clock.advance(10); await settle()
  expect(r.compacts).toEqual([])
})

test('/squish status says so when the fill cannot be read', async ($, on) => {
  const r = rig(on)
  r.state.failUsage = true
  expect(await cmd($, 'squish', 'status')).toEqual({ text: 'context fill unknown' })
})

test('/squish auto on|off sets autoCompact', async ($, on) => {
  const r = rig(on, { usage: usage(100_000, 1_000_000, 10) })
  const off = await cmd($, 'squish', 'auto off')
  expect(off.text).toStartWith('Auto-compact is off. Squishing is by hand now: /squish.')
  const onOut = await cmd($, 'squish', 'AUTO ON')
  expect(onOut.text).toStartWith('Auto-compact is on.')
  expect(r.configSets).toEqual([{ key: 'autoCompact', value: false }, { key: 'autoCompact', value: true }])
  expect(r.compacts).toEqual([])
})

test('/squish auto: a failing config.set points at /config', async ($, on) => {
  const r = rig(on)
  r.state.failConfigSet = true
  const out = await cmd($, 'squish', 'auto off')
  expect(out.text).toMatch(/^Could not switch auto-compact \(.*\); use \/config\./)
})

test('/squish: a failing compact logs one warning and does not throw', async ($, on) => {
  const r = rig(on)
  r.state.failCompact = true
  const out = await cmd($, 'squish', '')
  expect(out.text).toStartWith('Squishing…')
  await r.clock.advance(10); await settle()
  expect(r.logs.some(l => l.includes('/squish could not run /compact'))).toBe(true)
})

test('/squish is registered at session start, and not when squish.enabled is false', async ($, on) => {
  const a = rig(on)
  await $.session.start({ source: 'startup', cwd: '/work' } as any)
  expect(a.registered.some(x => x.command === 'squish')).toBe(true)
})

test('squish.enabled false: no /squish command is registered', async ($, on) => {
  const a = rig(on, { config: { squish: { enabled: false } } })
  await $.session.start({ source: 'startup', cwd: '/work' } as any)
  expect(a.registered.some(x => x.command === 'squish')).toBe(false)
})
