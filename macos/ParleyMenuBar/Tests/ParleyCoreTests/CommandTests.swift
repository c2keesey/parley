import ParleyCore

func runCommandTests() throws {
  try testMapsSafeControlsToExistingCLICommands()
  try testDisablesControlsThatAreUnsafeOrAlreadySatisfied()
  try testLifecycleActionsHaveUnambiguousWording()
}

private func testLifecycleActionsHaveUnambiguousWording() throws {
  let target = "ivory lynx"
  let voiceOn = ParleyControl.voiceOn.presentation(targetName: target)
  let voiceOff = ParleyControl.voiceOff.presentation(targetName: target)
  let listenerOff = ParleyControl.listenerOff.presentation(targetName: target)
  let stopSpeech = ParleyControl.stopSpeech.presentation(targetName: target)

  try expect(
    voiceOn.title == "Enable Voice for ivory lynx",
    "voice-on wording should be an imperative action"
  )
  try expect(
    voiceOff.title == "Disable Voice for ivory lynx",
    "voice-off wording should be an imperative action"
  )
  try expect(
    voiceOff.hint
      == "Disables future spoken replies for this target, stops current speech, and clears queued speech. The listener keeps running.",
    "voice-off hint should explain current, queued, future, and listener behavior"
  )
  try expect(listenerOff.title == "Stop Listener", "listener action should be distinct")
  try expect(stopSpeech.title == "Stop Speech Now", "speech action should be distinct")
  try expect(
    Set([voiceOn.title, voiceOff.title, listenerOff.title, stopSpeech.title]).count == 4,
    "lifecycle actions should not share labels"
  )
}

private func testMapsSafeControlsToExistingCLICommands() throws {
  let off = makeSnapshot(running: false, speaking: false, voiceOn: false)
  let on = makeSnapshot(running: true, speaking: true, voiceOn: true)

  try expect(
    ParleyCommandPlanner.command(for: .voiceOn, snapshot: off)
      == ParleyCommand(arguments: ["on"], environment: ["TMUX_PANE": "%7"]),
    "voice on command should retain target context"
  )
  try expect(
    ParleyCommandPlanner.command(for: .listenerOn, snapshot: off)
      == ParleyCommand(
        arguments: ["listen", "on"],
        environment: ["TMUX_PANE": "%7"],
        timeout: 30
      ),
    "listener on command should retain target context"
  )
  try expect(
    ParleyCommandPlanner.command(for: .voiceOff, snapshot: on)
      == ParleyCommand(arguments: ["off"], environment: ["TMUX_PANE": "%7"]),
    "voice off command should retain target context"
  )
  try expect(
    ParleyCommandPlanner.command(for: .listenerOff, snapshot: on)
      == ParleyCommand(arguments: ["listen", "off"]),
    "listener off command should use the existing CLI"
  )
  try expect(
    ParleyCommandPlanner.command(for: .stopSpeech, snapshot: on)
      == ParleyCommand(arguments: ["stop"]),
    "stop command should use the existing CLI"
  )
}

private func testDisablesControlsThatAreUnsafeOrAlreadySatisfied() throws {
  let degraded = makeSnapshot(
    running: true,
    speaking: false,
    voiceOn: false,
    targetAvailable: false
  )

  try expect(
    ParleyCommandPlanner.command(for: .voiceOn, snapshot: degraded) == nil,
    "voice on should be disabled without a live target"
  )
  try expect(
    ParleyCommandPlanner.command(for: .listenerOn, snapshot: degraded) == nil,
    "listener on should be disabled without a live target"
  )
  try expect(
    ParleyCommandPlanner.command(for: .stopSpeech, snapshot: degraded) == nil,
    "stop should be disabled when nothing is speaking"
  )
  try expect(
    ParleyCommandPlanner.command(for: .listenerOff, snapshot: degraded) != nil,
    "listener off should remain available during degradation"
  )
}

private func makeSnapshot(
  running: Bool,
  speaking: Bool,
  voiceOn: Bool,
  targetAvailable: Bool = true
) -> ParleySnapshot {
  ParleySnapshot(
    contractVersion: 1,
    listenerRunning: running,
    listenerState: running ? "ready" : "off",
    speaking: speaking,
    target: ParleyTarget(
      available: targetAvailable,
      label: targetAvailable ? "work session" : nil,
      pane: "%7"
    ),
    voiceOn: voiceOn
  )
}
