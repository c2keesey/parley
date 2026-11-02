import type { On } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'
import type { TestBody } from 'claude-code/testing'

type Engine = Parameters<TestBody>[0]

const BAND = {
  plugin: 'parley',
  component: 'AbovePrompt',
  requestId: 'above-prompt',
  props: {
    hasSurvey: false,
    isWorking: false,
    maxRows: 10,
    bodyColumns: 100,
    scroll: { offset: 0, bodyRows: 10 },
    view: {},
  },
} as const

const SURFACES = ['terminal', 'desktop'] as const

type Snap = Record<string, unknown>

const ready: Snap = {
  state: 'ready',
  route: 'this',
  label: 'windy-falcon',
  pane: '%42',
  warning: false,
  text: ' 🎙 PARLEY READY · THIS PANE ',
  wake: 'okay computer',
  send: 'send it',
  stop: 'stop talking',
}

/**
 * Stands in for the host: a fake Parley state directory whose files the test
 * touches, and a `parley indicator --json` whose answer the test sets.
 */
function world(on: On, start: Snap, env: Record<string, string> = { TMUX_PANE: '%42' }) {
  const clock = mock.clock(on, { now: 1_000 })
  mock.env(on, { HOME: '/home/u', ...env })
  const files = new Map<string, number>()
  const writes: [string, string][] = []
  const calls: string[][] = []
  let answer: Snap = start

  on('fs.stat', ($, e) => {
    const mtimeMs = files.get(e.path)
    if (mtimeMs === undefined) throw new Error('ENOENT')
    return { value: { kind: 'file', size: 1, mtimeMs, isLink: false } }
  })
  on('fs.write', ($, e) => {
    writes.push([e.path, e.text])
    return { value: undefined }
  })
  // The engine's own band beneath the mod: other mods' rows, or nothing.
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine</Text>
  })
  on('process.run', ($, e) => {
    calls.push([...e.argv])
    return {
      value: {
        exitCode: 0,
        stdout: JSON.stringify(answer),
        stderr: '',
        isStdoutTruncated: false,
        isStderrTruncated: false,
      },
    }
  })
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.end', () => ({ sessionId: 's' }))

  return {
    clock,
    calls,
    writes,
    touch: (name: string) => files.set(`/home/u/.parley/${name}`, clock.now()),
    answer: (snap: Snap) => {
      answer = snap
    },
  }
}

async function texts($: Engine, surface: 'terminal' | 'desktop') {
  const ui = await $.ui.mount({ ...BAND, surface })
  const found: string[] = []
  for (const pattern of [/parley|[○●♪◐◓◑◒↻]/, /okay computer|send it|stop talking/, /⚠/, /engine/]) {
    const text = (await ui.find({ type: 'Text', text: pattern }))?.text
    if (text) found.push(text)
  }
  await ui.unmount()
  return found.join(' | ')
}

const start = ($: Engine) =>
  $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })

test('idle is quiet: the mark, the wordmark and the wake phrase', async ($, on) => {
  const w = world(on, ready)
  await start($)

  expect(w.calls[0]).toEqual(['parley', 'indicator', '--json', '%42'])
  for (const surface of SURFACES) {
    const shown = await texts($, surface)
    expect(shown).toContain('parley')
    expect(shown).toContain('okay computer')
    expect(shown).not.toContain('⚠')
    // What the engine drew beneath still shows under the band.
    expect(shown).toContain('engine')
  }
})

test('a wake shows within one tick, with a running timer', async ($, on) => {
  const w = world(on, ready)
  await start($)

  w.answer({ ...ready, state: 'listening' })
  await w.clock.advance(10)
  w.touch('listener.state')
  await w.clock.advance(250)

  let shown = await texts($, 'terminal')
  expect(shown).toContain('listening')
  expect(shown).toContain('send it')

  await w.clock.advance(3_000)
  shown = await texts($, 'terminal')
  expect(shown).toMatch(/0:0[23]/)
})

test('an idle band does not redraw; Parley is asked only on the heartbeat', async ($, on) => {
  const w = world(on, ready)
  await start($)

  await w.clock.advance(2_000)
  expect(w.calls.length).toBe(1)
  await w.clock.advance(2_500)
  expect(w.calls.length).toBe(2)
})

test('the band marks its pane so the tmux badge stands aside', async ($, on) => {
  const w = world(on, ready)
  await start($)
  expect(w.writes[0]?.[0]).toBe('/home/u/.parley/bands/pane-_42')
  expect(w.writes[0]?.[1]).not.toBe('')

  await w.clock.advance(4_250)
  expect(w.writes.length).toBe(2)

  await $.session.end({ reason: 'other', sessionId: 's', resume: { id: 's' } })
  expect(w.writes.at(-1)).toEqual(['/home/u/.parley/bands/pane-_42', ''])
})

test('PARLEY_PANE stands in for TMUX_PANE', async ($, on) => {
  const w = world(on, ready, { PARLEY_PANE: '%7', PARLEY_STATE: '/fake' })
  await start($)
  expect(w.calls[0]).toEqual(['parley', 'indicator', '--json', '%7'])
  expect(w.writes[0]?.[0]).toBe('/fake/bands/pane-_7')
})

test('outside tmux the band shows nothing and writes nothing', async ($, on) => {
  const w = world(on, ready, {})
  await start($)
  expect(w.calls.length).toBe(0)
  expect(w.writes.length).toBe(0)
  expect(await texts($, 'terminal')).toBe('engine')
})

test('a dictation bound for another session is flagged', async ($, on) => {
  world(on, { ...ready, route: 'other', warning: true })
  await start($)
  expect(await texts($, 'terminal')).toContain('⚠ dictation goes to windy-falcon')
})

test('off, the band is left to the engine', async ($, on) => {
  world(on, { state: 'off', route: '', label: '', pane: '', warning: false, text: '' })
  await start($)
  for (const surface of SURFACES) {
    expect(await texts($, surface)).toBe('engine')
  }
})

for (const style of ['chip', 'glyph'] as const) {
  test(`the ${style} style draws the listening state`, { options: { style } }, async ($, on) => {
    world(on, { ...ready, state: 'listening' })
    await start($)
    const shown = await texts($, 'terminal')
    expect(shown).toContain('●')
    if (style === 'chip') expect(shown).toContain('listening')
  })
}
