import ParleyCore

func runCommandTests() throws {
  try testMapsSafeControlsToExistingCLICommands()
  try testDisablesControlsThatAreUnsafeOrAlreadySatisfied()
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
    cliVersion: "0.5.4",
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
