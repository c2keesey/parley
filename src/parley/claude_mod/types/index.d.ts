/** What `parley indicator --json <pane>` prints; the band draws from it. */
export type ParleySnapshot = {
  state: 'off' | 'ready' | 'listening' | 'sending' | 'reconnecting' | 'speaking'
  route: '' | 'this' | 'other' | 'target' | 'unavailable'
  label: string
  pane: string
  warning: boolean
  text: string
  wake?: string
  send?: string
  stop?: string
}

/** The animation clock: ticks while a state moves, and time in that state. */
export type ParleyFrame = { tick: number; elapsedMs: number }

declare module 'claude-code' {
  interface PluginState {
    parley: { snapshot: ParleySnapshot | null; frame: ParleyFrame }
  }
}
