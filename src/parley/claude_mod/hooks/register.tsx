// Parley's listener state, live, in a band above the Claude Code prompt.
//
// The truth comes from `parley indicator --json <pane>`, the same snapshot the
// tmux badge renders, so the two never disagree. Running Python on every tick
// would cost a process start each time, so the mod watches the handful of
// files Parley writes on every transition (cheap stats through the host) and
// asks Parley again only when one of them changes, plus a slow heartbeat that
// catches a listener or player that died without cleaning up.
//
// While it draws, the mod also keeps bands/<pane key> fresh in Parley's state
// folder; the tmux badge stands aside for a pane with a fresh marker, so a
// Claude session never shows the state twice.
//
// The look: quiet at idle (one dim glyph and the wake phrase), vivid only
// while the microphone is open (a breathing red dot and an elapsed timer, like
// a recorder). Colors are theme keys, so light and dark themes both hold.
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { ParleyFrame, ParleySnapshot } from '../types'

const snapshot = atom({ plugin: 'parley', key: 'snapshot' } as const, null)
const frame = atom({ plugin: 'parley', key: 'frame' } as const, { tick: 0, elapsedMs: 0 })

/** How often the watched files are stat'ed, and the animation's frame. */
export const TICK_MS = 250
/** How long a snapshot may stand unchanged before Parley is asked again. */
export const HEARTBEAT_MS = 4000
/** How often the band's coverage marker is rewritten (Parley trusts 15 s). */
export const MARKER_MS = 4000

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

export type Style = 'quiet' | 'chip' | 'glyph'

type State = Exclude<ParleySnapshot['state'], 'off'>
type Tone = 'inactive' | 'error' | 'warning' | 'permission'

const TONE: Record<State, Tone> = {
  ready: 'inactive',
  listening: 'error',
  sending: 'warning',
  reconnecting: 'warning',
  speaking: 'permission',
}

const WORD: Record<State, string> = {
  ready: 'ready',
  listening: 'listening',
  sending: 'sending',
  reconnecting: 'finding a microphone',
  speaking: 'speaking',
}

const SPINNER = ['◐', '◓', '◑', '◒']

/** States that move: the band redraws on the frame clock only for these. */
export const ANIMATED: ReadonlySet<string> = new Set(['listening', 'sending', 'reconnecting'])

/** The state's mark for this frame: a breath, a spinner, or a still glyph. */
export function glyph(state: State, tick: number): { mark: string; isDim: boolean } {
  if (state === 'listening') return { mark: '●', isDim: tick % 4 >= 2 }
  if (state === 'sending') return { mark: SPINNER[tick % SPINNER.length] ?? '◐', isDim: false }
  if (state === 'reconnecting') return { mark: '↻', isDim: tick % 4 >= 2 }
  if (state === 'speaking') return { mark: '♪', isDim: false }
  return { mark: '○', isDim: false }
}

/** Everything a frame draws, as one string: equal strings, no redraw. */
export function look(state: State, tick: number, elapsedMs: number): string {
  const { mark, isDim } = glyph(state, tick)
  return `${mark}${isDim}${state === 'listening' ? clock(elapsedMs) : ''}`
}

export function clock(ms: number): string {
  const seconds = Math.max(0, Math.floor(ms / 1000))
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
}

/** The phrase to say next, or none: `[before, phrase, after]`. */
export function hint(snap: ParleySnapshot): [string, string, string] | null {
  if (snap.state === 'ready' && snap.wake) return ['say ', snap.wake, '']
  if (snap.state === 'listening' && snap.send) return ['', snap.send, ' sends']
  if (snap.state === 'speaking' && snap.stop) return ['', snap.stop, ' skips']
  return null
}

/** Where dictation goes, when it is not simply here. */
export function route(snap: ParleySnapshot): string | null {
  if (snap.route === 'other' || snap.route === 'target') return `dictation goes to ${snap.label}`
  if (snap.route === 'unavailable') return 'dictation target is gone, run parley on here'
  return null
}

export function parse(stdout: string): ParleySnapshot | null {
  try {
    const value = JSON.parse(stdout)
    return value && typeof value.state === 'string' ? (value as ParleySnapshot) : null
  } catch {
    return null
  }
}

export function paneKey(pane: string): string {
  return 'pane-' + pane.replace(/[^A-Za-z0-9_-]/g, '_')
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

export const register: Register = (on, options) => {
  const style: Style =
    options.style === 'chip' || options.style === 'glyph' ? options.style : 'quiet'
  let marker = ''

  on('session.start', async ($, e, next) => {
    const started = await next(e)

    // PARLEY_PANE stands in for TMUX_PANE where a session must be kept apart
    // from Parley's own pane detection (Parley's tests do this).
    const pane = (await $.env.get('PARLEY_PANE')) || (await $.env.get('TMUX_PANE')) || ''
    const home = (await $.env.get('HOME')) ?? ''
    const dir = (await $.env.get('PARLEY_STATE')) || `${home}/.parley`
    const bin = (await $.env.get('PARLEY_BIN')) || 'parley'

    // Parley addresses a session by its tmux pane; outside tmux it cannot
    // type into this session, so there is nothing truthful to show.
    if (!pane) {
      await update($, snapshot, () => null)
      return started
    }
    marker = `${dir}/bands/${paneKey(pane)}`

    let last = ''
    let lastText = ''
    let askedAt = 0
    let markedAt = -Infinity
    let isAsking = false
    let shown = ''
    let since = 0
    let tick = 0

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

    const step = async () => {
      if (isAsking) return
      const now = await $.clock.now()

      if (now - markedAt >= MARKER_MS) {
        markedAt = now
        await $.fs.write(marker, String(now)).catch(() => undefined)
      }

      const marks = await signature($, dir)
      if (marks !== last || now - askedAt >= HEARTBEAT_MS) {
        last = marks
        await ask()
      }

      // The frame clock: restart on a new state, and move only while the
      // state itself moves, so an idle band never redraws.
      const state = (await read($, snapshot))?.state ?? 'off'
      if (state !== shown) {
        shown = state
        since = now
        tick = 0
        await update($, frame, () => ({ tick, elapsedMs: 0 }))
      } else if (ANIMATED.has(state)) {
        // Redraw only when what is drawn changes: the breath every other
        // tick, the timer once a second, the spinner every tick.
        const was = look(state as State, tick, now - since - TICK_MS)
        tick += 1
        const moved: ParleyFrame = { tick, elapsedMs: now - since }
        if (look(state as State, tick, moved.elapsedMs) !== was) {
          await update($, frame, () => moved)
        }
      }
    }

    await step()
    $.clock.every(TICK_MS, step)
    return started
  })

  on('session.end', async ($, e, next) => {
    // An emptied marker hands the pane back to the tmux badge at once.
    if (marker) await $.fs.write(marker, '').catch(() => undefined)
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const snap = await read($, snapshot)
    if (e.props.hasSurvey || !snap || snap.state === 'off') {
      return next(e)
    }

    const { Box, Text } = $.ui.resolve(e)
    const state = snap.state
    const tone = TONE[state]
    const { tick, elapsedMs } = ANIMATED.has(state) ? await read($, frame) : { tick: 0, elapsedMs: 0 }
    const { mark, isDim } = glyph(state, tick)
    const isIdle = state === 'ready'
    const timer = state === 'listening' ? clock(elapsedMs) : null
    const said = hint(snap)
    const away = route(snap)
    const below = await next(e)

    // The phrase itself is the one thing at full strength: dim words around
    // it, the words to say in plain italic.
    const phrase = said ? (
      <Text>
        {said[0] ? <Text dimColor>{said[0]}</Text> : null}
        <Text italic>{said[1]}</Text>
        {said[2] ? <Text dimColor>{said[2]}</Text> : null}
      </Text>
    ) : null

    const warning = away ? (
      <Text color="warning" wrap="truncate-end">
        ⚠ {away}
      </Text>
    ) : null

    let line
    if (style === 'chip') {
      // A filled pill in the state's color; at idle, an outline-quiet one.
      line = (
        <Box flexDirection="row" gap={2}>
          <Text
            backgroundColor={isIdle ? undefined : tone}
            color={isIdle ? 'inactive' : 'inverseText'}
            bold={!isIdle}
          >
            {' '}
            {mark} {WORD[state]}
            {timer ? ` ${timer}` : ''}{' '}
          </Text>
          {phrase}
          {warning}
        </Box>
      )
    } else if (style === 'glyph') {
      // The least ink: the mark and the timer, words only for a problem.
      line = (
        <Box flexDirection="row" gap={1}>
          <Text color={tone} dimColor={isDim}>
            {mark}
          </Text>
          {timer ? <Text color={tone}>{timer}</Text> : null}
          {warning}
        </Box>
      )
    } else {
      // quiet: a wordmark, the state, and the phrase to say next.
      line = (
        <Box flexDirection="row" gap={2}>
          <Text>
            <Text color={tone} dimColor={isDim}>
              {mark}
            </Text>{' '}
            <Text dimColor={isIdle} bold={!isIdle}>
              parley
            </Text>
            {isIdle ? null : (
              <Text color={tone}>
                {' '}
                {WORD[state]}
                {timer ? <Text dimColor> {timer}</Text> : null}
              </Text>
            )}
          </Text>
          {phrase}
          {warning}
        </Box>
      )
    }

    return (
      <Box flexDirection="column">
        {line}
        {below ?? null}
      </Box>
    )
  })
}
