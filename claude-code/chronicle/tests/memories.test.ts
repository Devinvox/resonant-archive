import { test, expect } from 'claude-code/testing'
import {
  noteRow, memoryPrompt, answerForm, hash16, hmacHex, macMessage, memoryChain, planAssembly, assemblyAllowed, memoryBlock,
  synthKind, msgAnchor, MEMORY_BLOCK_HEADER, MEMORY_ACK, LEGACY_MEMORY_ACKS, MEMORY_MARKER, DEFAULT_MEMORY_VOICE, memoryVoiceOf,
} from '../hooks/register'
import { rig, settle } from './harness'

type Msg = { role: 'user' | 'assistant'; text: string; toolUses: any[] }
const user = (text: string): Msg => ({ role: 'user', text, toolUses: [] })
const asst = (text: string): Msg => ({ role: 'assistant', text, toolUses: [] })
const LONG = (s: string) => `${s} ${'and some more words to pass the forty character floor '.repeat(2)}`

// Four turns. Memory 0 covers turns 1-2 (rows u0..u3) and ends with reply 2.
const msgs: Msg[] = [
  user(LONG('request one')), asst(LONG('reply one')),
  user(LONG('request two')), asst(LONG('reply two')),
  user(LONG('request three')), asst(LONG('reply three')),
  user(LONG('request four')), asst(LONG('reply four')),
]
const uuids = ['u0', 'u1', 'u2', 'u3', 'u4', 'u5', 'u6', 'u7']
const seq = uuids.map(uuid => ({ uuid, door: 'prompt' }))
const mem = (over: Record<string, unknown> = {}) => {
  const reply = answerForm(msgs[3]!.text)
  return {
    t: 1, text: 'MEMORY ZERO', from_uuid: 'u0', to_uuid: 'u3', t_from: 1, t_to: 2, ordinal: 0,
    first_text_snippet: msgAnchor(msgs[0]!.text), last_text_snippet: msgAnchor(msgs[3]!.text),
    last_assistant_text_hash: hash16(reply), answer_chars: reply.length, ...over,
  } as any
}
const facts = (over: Record<string, unknown> = {}) => ({ freshMarked: true, compacted: false, anchors: new Map([['u4', msgAnchor(msgs[4]!.text)], ['u1', msgAnchor(msgs[1]!.text)]]), ...over }) as any
const opts = (over: Record<string, unknown> = {}) => ({ gapRawMax: 12_000, blockMax: 50_000, facts: facts(), ...over })

test('planAssembly: a fresh session with a covering memory plans an assembly with a raw gap', () => {
  const plan = planAssembly(msgs as any, 6, [mem()], seq, opts()) as any
  expect(typeof plan).toBe('object')
  expect(plan.mode).toBe('fresh')
  expect(plan.k).toBe(3)
  expect(plan.memories.length).toBe(1)
  expect(plan.gap.length).toBe(2)
  expect(plan.gapRaw).toBe(true)
  expect(plan.blockText).toStartWith(MEMORY_BLOCK_HEADER)
  expect(plan.blockText).toContain('MEMORY ZERO')
})

test('planAssembly: no fresh-start mark, an earlier compaction, or a mismatched forward edge all refuse', () => {
  expect(typeof planAssembly(msgs as any, 6, [mem()], seq, opts({ facts: facts({ freshMarked: false }) }))).toBe('string')
  expect(typeof planAssembly(msgs as any, 6, [mem()], seq, opts({ facts: facts({ compacted: true }) }))).toBe('string')
  const wrong = facts({ anchors: new Map([['u4', 'something else entirely']]) })
  expect(planAssembly(msgs as any, 6, [mem()], seq, opts({ facts: wrong }))).toContain('could not find where my latest memory ends')
})

test('planAssembly: a final reply that appears twice is never trusted', () => {
  const dup = msgs.slice()
  dup[5] = asst(msgs[3]!.text)
  expect(typeof planAssembly(dup as any, 6, [mem()], seq, opts())).toBe('string')
})

test('planAssembly: an over-large memories block, a bad cut and a missing chain refuse', () => {
  expect(planAssembly(msgs as any, 6, [mem()], seq, opts({ blockMax: 1 }))).toContain('over 1')
  expect(planAssembly(msgs as any, 0, [mem()], seq, opts())).toBe('no safe cut')
  expect(planAssembly(msgs as any, 6, [], seq, opts())).toBe('no memories for this session yet')
})

test('planAssembly: an older memories block written with the (London) heading is still accepted', () => {
  const old = `${MEMORY_BLOCK_HEADER}\n\n— 2026-01-05 10:00 to 11:00 (London) —\nMEMORY ZERO\n\n[end of my memories.]`
  const m2: Msg[] = [user(old), asst(MEMORY_ACK), user(LONG('request three')), asst(LONG('reply three')), user(LONG('request four')), asst(LONG('reply four'))]
  const plan = planAssembly(m2 as any, 4, [mem()], seq, opts()) as any
  expect(plan.mode).toBe('reassembled')
  expect(plan.memories.length).toBe(1)
})

test('memoryChain: a clean chain, a hole, an overlap and a wrong first chunk', () => {
  const m0 = mem(), m1 = mem({ from_uuid: 'u4', to_uuid: 'u5', ordinal: 1, t: 2 }), m2 = mem({ from_uuid: 'u6', to_uuid: 'u7', ordinal: 2, t: 3 })
  expect((memoryChain([m0, m1, m2], seq) as any[]).length).toBe(3)
  expect((memoryChain([m0, m2], seq) as any[]).length).toBe(1) // ordinal 1 missing: the chain stops
  expect((memoryChain([m0, mem({ from_uuid: 'u3', to_uuid: 'u5', ordinal: 1 })], seq) as any[]).length).toBe(1) // overlaps u3
  expect(typeof memoryChain([mem({ ordinal: 1 })], seq)).toBe('string')
  expect(typeof memoryChain([m1], seq)).toBe('string') // does not start at the first row
})

test('assemblyAllowed: manual, auto and off', () => {
  expect(assemblyAllowed('manual', 'manual')).toBeUndefined()
  expect(assemblyAllowed('manual', 'auto')).toContain('only for a manual')
  expect(assemblyAllowed('auto', 'auto')).toBeUndefined()
  expect(assemblyAllowed(true, 'auto')).toBeUndefined()
  expect(assemblyAllowed(false, 'manual')).toContain('off')
})

test('synthKind and memoryBlock: the block, its acknowledgement and old acknowledgements are recognised', () => {
  expect(synthKind('user', memoryBlock([mem()]))).toBe('memories')
  expect(synthKind('assistant', MEMORY_ACK)).toBe('ack')
  for (const old of LEGACY_MEMORY_ACKS) expect(synthKind('assistant', old)).toBe('ack')
  expect(synthKind('user', `${MEMORY_MARKER} more`)).toBe('fork')
  expect(synthKind('user', 'hello')).toBeUndefined()
  expect(memoryBlock([mem()])).toContain('mcp__chronicle__recall')
})

test('hmacHex is a real HMAC-SHA-256 (RFC 4231 case 2) and macMessage covers the text', async () => {
  const key = new TextEncoder().encode('Jefe')
  expect(await hmacHex(key, 'what do ya want for nothing?')).toBe('5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843')
  expect(macMessage({ text: 'a' })).not.toBe(macMessage({ text: 'b' }))
})

test('memoryPrompt: names the slice, carries the final reply, and the default voice is neutral', () => {
  const chunk = { sid: 's', fromUuid: 'a', toUuid: 'b', tFrom: 0, tTo: 0, firstLabel: 'You', firstSnippet: 'first words', lastLabel: 'Assistant', lastSnippet: 'last words', tokens: 20_000, rows: 12, answer: 'FINAL', compacted: true }
  const p = memoryPrompt(chunk as any, memoryVoiceOf({ memoryVoice: '', memoryPrompt: '' }))
  expect(p).toStartWith(MEMORY_MARKER)
  expect(p).toContain('first words')
  expect(p).toContain('FINAL')
  expect(p).toContain('A compaction happened')
  expect(p).toContain('Never copy credentials')
  expect(DEFAULT_MEMORY_VOICE).toContain("the person I'm working with")
})

test('noteRow: a memory fork row and compaction rows do not count', () => {
  noteRow({ t: 1, sid: 's', uuid: 'a', door: 'prompt', text: 'hi' }, 100)
  noteRow({ t: 1, sid: 's', uuid: 'b', door: 'compaction', text: 'x' }, 100)
  noteRow({ t: 1, sid: 's', uuid: 'c', door: 'prompt', text: `${MEMORY_MARKER} more` }, 100)
})

// ---- the engine: memories are off unless asked for ----

const row = (text: string, role: 'user' | 'assistant', id: string) => ({
  message: { type: role, role, content: [{ type: 'text', text }] },
  door: role === 'user' ? 'prompt' : 'response',
  origin: role === 'user' ? { kind: 'composer' } : { kind: 'model', model: 'm' },
  uuid: id,
}) as any
const turnDone = ($: any, answer: string) => $.turn.complete({ reason: 'answer', answer, isAborted: false, turnId: 't' })
const start = ($: any) => $.session.start({ source: 'startup', cwd: '/work' } as any)
const BIG = 'word '.repeat(2000)

function forkHook(on: any) {
  const prompts: string[] = []
  on('model.fork', async (_$: any, e: any) => { prompts.push(e.prompt); return { value: { isAnswered: true, text: 'THE MEMORY with sk-ant-' + 'Y'.repeat(30), usage: { input_tokens: 10, cache_read_input_tokens: 90 } } } })
  return prompts
}

test('memories are off by default: no fork, no memory file', async ($, on) => {
  const r = rig(on, { config: { timezone: 'UTC' } })
  const prompts = forkHook(on)
  await start($)
  await $.session.append(row(BIG, 'user', 'e0000000-0000-4000-8000-000000000001'))
  await $.session.append(row(BIG, 'assistant', 'e0000000-0000-4000-8000-000000000002'))
  await turnDone($, BIG)
  await r.clock.advance(120_000); await settle()
  expect(prompts).toEqual([])
  expect(r.runs.some(x => String(x.argv[5] ?? '').endsWith('.memories.jsonl'))).toBe(false)
})

test('memories.enabled: a stretch is sealed at a turn end, formed by one fork, redacted, signed and written', async ($, on) => {
  const r = rig(on, { config: { timezone: 'UTC', memories: { enabled: true, chunkMinTokens: 100, chunkMaxTokens: 400 } } })
  const prompts = forkHook(on)
  await start($)
  await $.session.append(row(BIG, 'user', 'e0000000-0000-4000-8000-000000000001'))
  await $.session.append(row(BIG, 'assistant', 'e0000000-0000-4000-8000-000000000002'))
  await turnDone($, BIG)
  await r.clock.advance(5_000); await settle()
  await r.clock.advance(31_000); await settle()
  expect(prompts.length).toBe(1)
  expect(prompts[0]).toStartWith(MEMORY_MARKER)
  const file = r.runs.find(x => String(x.argv[5] ?? '').endsWith('.memories.jsonl'))
  expect(file).toBeDefined()
  const memRow = JSON.parse(file!.stdin!.split('\n')[0]!)
  expect(memRow).toMatchObject({ event: 'memory', level: 1, ordinal: 0, from_uuid: 'e0000000-0000-4000-8000-000000000001', to_uuid: 'e0000000-0000-4000-8000-000000000002' })
  expect(memRow.text).not.toContain('YYYYYY')
  expect(memRow.mac).toMatch(/^[0-9a-f]{64}$/)
  expect(typeof r.store.get('memoryMacKey')).toBe('string')
})

test('memories.enabled: /clear gives up what is not formed yet, and says so in the mirror', async ($, on) => {
  const r = rig(on, { config: { timezone: 'UTC', memories: { enabled: true, chunkMinTokens: 100000 } } })
  forkHook(on)
  await start($)
  await $.session.append(row(BIG, 'user', 'e0000000-0000-4000-8000-000000000011'))
  await ($ as any).session.end({ reason: 'clear', sessionId: 'sess-1', resume: { id: 'sess-1' } })
  await r.clock.advance(31_000); await settle()
  const all = r.runs.map(x => x.stdin ?? '').join('')
  expect(all).toContain('"event":"memory_skipped"')
  expect(all).toContain('cleared before the memory formed')
})

test('a configured memory voice is put in front of the instructions; memoryVoice replaces them', () => {
  expect(memoryVoiceOf({ memoryVoice: 'Persona.', memoryPrompt: '' }).startsWith('Persona.\n\n')).toBe(true)
  const own = memoryVoiceOf({ memoryVoice: 'ignored', memoryPrompt: 'Just write it.' })
  expect(own.startsWith('Just write it.')).toBe(true)
  expect(own).toContain('Never copy credentials')
})
