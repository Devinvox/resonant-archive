# Compaction, and how chronicle makes it bearable

This page explains what compaction is, what goes wrong with it, and what the chronicle mod does about it. The mod's own
README ([`claude-code/chronicle/README.md`](../claude-code/chronicle/README.md)) has the install steps and every setting.

## What compaction is

An AI conversation lives in a context window of limited size. As you work, the window fills with your messages, the
replies, file contents and tool output. When it is nearly full, Claude Code compacts it. It asks a model to summarise
the whole conversation so far, throws the original away, and carries on from the summary.

Without compaction a long session would simply stop. With it, you keep going. The problem is what the summary costs.

## Why it hurts

- **The summary is written by a stranger.** It is a generic pass over a pile of text. It doesn't know which of forty
  file paths you'll need again.
- **Details go first.** Exact paths, ids, version numbers, the reason a decision was made, the thing you were about to
  do. These are the cheapest things to drop and the most expensive to lose.
- **The last stretch goes too.** The most recent exchanges are the ones you are in the middle of. A plain compaction
  summarises them along with everything else.
- **It happens when the window says so,** not when you choose. Often mid-thought.
- **It can't be undone.** Once the original is gone, the summary is all the AI has.

## What chronicle changes

### 1. Your AI writes the summary, in its own voice

The older part of the conversation goes to the summariser with a steer. By default the steer asks for a first-person
summary, written as the assistant about "the person I'm working with", that keeps:

- every exact identifier, verbatim (paths, ids, hashes, URLs, version numbers),
- every decision, and who made it,
- promises and open threads,
- your stated preferences, and your state where it matters,
- the tone between you,
- what was about to happen next.

It is told never to copy credentials. You can add a short paragraph in front of the steer with `compaction.voice` (for
example who the assistant is to you), or replace the steer completely with `compaction.steer`.

### 2. The recent stretch stays word for word (split compaction)

Chronicle splits the conversation at a point that leaves at least `compaction.tailTokens` (default 60,000) of the most
recent exchanges untouched, and at most `compaction.maxTailTokens` (default 120,000, also capped at 30% of the window).
Only the older part is summarised. The recent part is handed back exactly as it was.

If there are fewer than `compaction.minOlderMessages` (default 6) older messages, there is nothing worth splitting, and
the native compaction runs (still steered). If anything fails before the summariser runs, it falls back to ordinary
native compaction. A broken mod never leaves you without a compaction.

### 3. You choose when

- **`/squish [focus]`** runs compaction when you decide. The optional focus tells the summary what to care about.
  Under the hood it runs `/compact`, so everything above applies exactly as for a typed `/compact`.
- **`/squish status`** prints how full the window is.
- **`/squish auto on|off`** flips Claude Code's own `autoCompact` setting.
- **The context meter.** Every prompt you type (not tool results, not other agents' messages) gets a short line
  appended as context for the model: `[context · 142k of 1M (14%)]`. From 75% it adds
  ` · squish soon`. From 90%: ` · SQUISH NOW`. Change the thresholds with `contextLine.warnAt` and `urgentAt`, or turn
  it off with `contextLine.enabled`.

### 4. Nothing is truly lost

As a conversation happens, chronicle writes every row to a mirror on your disk:
`<archiveRoot>/<day>/<session>.jsonl` (default `~/.chronicle/archive`). Secrets are redacted before they are written.
Compaction summaries are recorded there too. So if the summary dropped something, the original is still there.

The `recall` tool searches it. Without the archive, `recall` searches the mirror with ripgrep, newest first. With the
archive configured (`"archive": { "command": ["resonant-archive", "recall"] }`) it also searches every past
conversation you have imported, by words, exact strings and meaning. See [RECALL-CONTRACT.md](RECALL-CONTRACT.md).

## Memories (optional, off by default)

With `memories.enabled: true`, every 15-25k tokens, at the end of a turn, the assistant writes a short first-person
memory of that stretch, in the background. At a manual compaction, if those memories provably cover the older part (an
unbroken chain from the first row, each memory signed with a key kept outside the mirror), the older part is replaced
by them with no summariser call at all. Anything short of that proof falls back to split compaction.

This is the biggest saving at compaction time. It also **spends model calls in the background.** The prefix is cached,
so it is cheap, but it is your usage. That is why it is off until you turn it on.

## An example (illustrative)

Say you have been debugging an uploader for three hours. Plain compaction produces: "The user and assistant debugged an
upload issue and made several changes." Chronicle produces something closer to: "We found the retry loop in
`src/upload/queue.ts:88` swallowing 429s. You chose exponential backoff capped at 30s over a token bucket, because the
API's limit is per key. I changed `queue.ts` and `retry.ts`; the test `retry.spec.ts` still fails on the jitter case.
Next: fix jitter, then run the soak test." And the last hour of back-and-forth is still there as you wrote it.

## What it can't do

- It can't make a summary perfect. It makes the summary better and the loss recoverable.
- It depends on Claude Code hooks, which are early access and may change.
- The mirror is a record of what was said, not a guarantee. Redaction is pattern-based and removes too much rather than
  too little.
- `/squish` starts `/compact` just after answering, because Claude Code refuses to run a command from inside a command
  hook.
