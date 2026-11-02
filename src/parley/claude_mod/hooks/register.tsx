// Parley's listener state, live, in a band above the Claude Code prompt.
//
// The truth comes from `parley indicator --json <pane>`, the same snapshot the
// tmux badge renders, so the two never disagree. Running Python on every tick
// would cost a process start each time, so the mod watches the handful of
// files Parley writes on every transition (cheap stats through the host) and
// asks Parley again only when one of them changes, plus a slow heartbeat that
// catches a listener or player that died without cleaning up.
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { ParleySnapshot } from '../types'

const snapshot = atom({ plugin: 'parley', key: 'snapshot' } as const, null)

/** How often the watched files are stat'ed. */
export const TICK_MS = 250
/** How long a snapshot may stand unchanged before Parley is asked again. */
export const HEARTBEAT_MS = 4000

/** Files and folders under the state directory whose changes mean "ask again". */
export const WATCHED = [
  'listener.state',
  'listener.pid',
  'target',
  'sessions',
  'queue',
  'playing.pid',
  'speech.pid',
  'drainer.pid',
  'microphone-turn.pid',
]

type Look = {
  glyph: string
  word: string
  color: 'success' | 'error' | 'warning' | 'suggestion' | 'claude'
}

const LOOKS: Record<Exclude<ParleySnapshot['state'], 'off'>, Look> = {
  ready: { glyph: '🎙', word: 'READY', color: 'success' },
  listening: { glyph: '🔴', word: 'LISTENING', color: 'error' },
  sending: { glyph: '⏳', word: 'SENDING', color: 'warning' },
  reconnecting: { glyph: '🎧', word: 'RECONNECTING', color: 'warning' },
  speaking: { glyph: '🔊', word: 'SPEAKING', color: 'claude' },
}

/** The words after the state: where dictation goes, and what to say next. */
export function detail(snap: ParleySnapshot): string {
  let route: string
  if (snap.route === 'this') route = 'this pane'
  else if (snap.route === 'unavailable') route = `target ${snap.pane || '(none)'} unavailable`
  else route = `sends to ${snap.label}`

  let hint = ''
  if (snap.state === 'ready' && snap.wake) hint = `say “${snap.wake}”`
  else if (snap.state === 'listening' && snap.send) hint = `“${snap.send}” to send`
  else if (snap.state === 'speaking') hint = 'mic ready'

  return [route, hint].filter(Boolean).join(' · ')
}

export function parse(stdout: string): ParleySnapshot | null {
  try {
    const value = JSON.parse(stdout)
    return value && typeof value.state === 'string' ? (value as ParleySnapshot) : null
  } catch {
    return null
  }
}

async function signature($: EngineInterface, dir: string): Promise<string> {
  const marks = await Promise.all(
    WATCHED.map(name =>
      $.fs
        .stat(`${dir}/${name}`)
        .then(s => `${s.mtimeMs}:${s.size}`)
        .catch(() => '-'),
    ),
  )
  return marks.join('|')
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)

    const pane = (await $.env.get('TMUX_PANE')) ?? ''
    const home = (await $.env.get('HOME')) ?? ''
    const dir = (await $.env.get('PARLEY_STATE')) || `${home}/.parley`
    const bin = (await $.env.get('PARLEY_BIN')) || 'parley'

    // Parley addresses a session by its tmux pane; outside tmux it cannot
    // type into this session, so there is nothing truthful to show.
    if (!pane) {
      await update($, snapshot, () => null)
      return started
    }

    let last = ''
    let lastText = ''
    let askedAt = 0
    let isAsking = false

    const ask = async () => {
      isAsking = true
      try {
        const run = await $.process.run([bin, 'indicator', '--json', pane], {
          timeoutMs: 5000,
        })
        const snap = run.exitCode === 0 ? parse(run.stdout) : null
        const text = JSON.stringify(snap)
        if (text !== lastText) {
          lastText = text
          await update($, snapshot, () => snap)
        }
      } catch {
        // `parley` missing or hung: show nothing rather than a stale state.
        if (lastText !== 'null') {
          lastText = 'null'
          await update($, snapshot, () => null)
        }
      } finally {
        askedAt = await $.clock.now()
        isAsking = false
      }
    }

    const tick = async () => {
      if (isAsking) return
      const marks = await signature($, dir)
      const now = await $.clock.now()
      if (marks !== last || now - askedAt >= HEARTBEAT_MS) {
        last = marks
        await ask()
      }
    }

    await tick()
    $.clock.every(TICK_MS, tick)
    return started
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const snap = await read($, snapshot)
    if (e.props.hasSurvey || !snap || snap.state === 'off') {
      return next(e)
    }

    const { Box, Text } = $.ui.resolve(e)
    const look = LOOKS[snap.state]
    const below = await next(e)

    return (
      <Box flexDirection="column">
        <Box flexDirection="row" gap={1}>
          {snap.warning ? <Text color="warning">⚠</Text> : null}
          <Text color={look.color} bold>
            {look.glyph} PARLEY {look.word}
          </Text>
          <Text color={snap.warning ? 'warning' : undefined} dimColor={!snap.warning} wrap="truncate-end">
            {detail(snap)}
          </Text>
        </Box>
        {below ?? null}
      </Box>
    )
  })
}
