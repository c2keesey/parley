import type { On } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'

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
}

/**
 * Stands in for the host: a fake Parley state directory whose files the test
 * touches, and a `parley indicator --json` whose answer the test sets.
 */
function world(on: On, start: Snap) {
  const clock = mock.clock(on, { now: 1_000 })
  mock.env(on, { TMUX_PANE: '%42', HOME: '/home/u' })
  const files = new Map<string, number>()
  const calls: string[][] = []
  let answer: Snap = start

  on('fs.stat', ($, e) => {
    const mtimeMs = files.get(e.path)
    if (mtimeMs === undefined) throw new Error('ENOENT')
    return { value: { kind: 'file', size: 1, mtimeMs, isLink: false } }
  })
  // The engine's own band beneath the mod: other mods' rows, or nothing.
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine</Text>
  })
  on('process.run', ($, e) => {
    calls.push([...e.argv])
    return { value: { exitCode: 0, stdout: JSON.stringify(answer), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })

  return {
    clock,
    calls,
    touch: (name: string) => files.set(`/home/u/.parley/${name}`, clock.now()),
    answer: (snap: Snap) => {
      answer = snap
    },
  }
}

test('the band shows the state and follows a wake within one tick', async ($, on) => {
  const w = world(on, ready)
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })

  expect(w.calls[0]).toEqual(['parley', 'indicator', '--json', '%42'])

  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ ...BAND, surface })
    expect((await ui.find({ type: 'Text', text: /PARLEY READY/ }))?.text).toBe('🎙 PARLEY READY')
    expect((await ui.find({ type: 'Text', text: /this pane/ }))?.text).toBe(
      'this pane · say “okay computer”',
    )
    await ui.unmount()
  }

  // The listener hears the wake word and rewrites listener.state.
  w.answer({ ...ready, state: 'listening' })
  await w.clock.advance(10)
  w.touch('listener.state')
  await w.clock.advance(250)

  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect((await ui.find({ type: 'Text', text: /PARLEY/ }))?.text).toBe('🔴 PARLEY LISTENING')
  expect((await ui.find({ type: 'Text', text: /send/ }))?.text).toBe('this pane · “send it” to send')
  // What the engine drew beneath still shows under the band.
  expect((await ui.find({ type: 'Text', text: /engine/ }))?.text).toBe('engine')
  await ui.unmount()
})

test('nothing changed on disk: Parley is asked only on the heartbeat', async ($, on) => {
  const w = world(on, ready)
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })

  await w.clock.advance(2_000)
  expect(w.calls.length).toBe(1)
  await w.clock.advance(2_500)
  expect(w.calls.length).toBe(2)
})

test('a dictation bound for another session is flagged', async ($, on) => {
  world(on, { ...ready, route: 'other', warning: true })
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })

  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect((await ui.find({ type: 'Text', text: /sends to/ }))?.text).toBe(
    'sends to windy-falcon · say “okay computer”',
  )
  expect((await ui.find({ type: 'Text', text: /⚠/ }))?.text).toBe('⚠')
  await ui.unmount()
})

test('off, the band is left to the engine', async ($, on) => {
  world(on, { state: 'off', route: '', label: '', pane: '', warning: false, text: '' })
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })

  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ ...BAND, surface })
    expect(await ui.find({ type: 'Text', text: /PARLEY/ })).toBeUndefined()
    expect((await ui.find({ type: 'Text', text: /engine/ }))?.text).toBe('engine')
    await ui.unmount()
  }
})
