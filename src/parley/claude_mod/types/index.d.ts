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
}

declare module 'claude-code' {
  interface PluginState {
    parley: { snapshot: ParleySnapshot | null }
  }
}
