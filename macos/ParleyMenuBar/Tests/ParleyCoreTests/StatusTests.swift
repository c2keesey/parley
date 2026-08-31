import Foundation
import ParleyCore

func runStatusTests() throws {
  try testDecodesVersionedStatusContract()
  try testRejectsUnknownContractVersion()
  try testMapsEveryOperationalState()
  try testMicrophoneFailuresOverridePidLiveness()
  try testStateIconographyAndLabelsAreDistinct()
}

private func testDecodesVersionedStatusContract() throws {
  let json =
    #"{"contract_version":2,"listener_running":true,"listener_state":"ready","microphone":{"state":"ready","reason":"capture_active","selector":"uid:built-in","device":{"index":0,"name":"Built-in Microphone","uid":"built-in","serial":null,"selector":"uid:built-in"}},"speaking":false,"target":{"available":true,"label":"ivory lynx","pane":"%531"},"voice_on":true}"#

  let status = try ParleySnapshot.decode(Data(json.utf8))

  try expect(status.contractVersion == 2, "contract version should decode")
  try expect(status.target.displayName == "ivory lynx", "target should decode")
  try expect(status.voiceOn, "voice state should decode")
}

private func testRejectsUnknownContractVersion() throws {
  let json =
    #"{"contract_version":3,"listener_running":false,"listener_state":"off","microphone":{"state":"unknown","reason":"not_checked","selector":"0","device":null},"speaking":false,"target":{"available":false,"label":null,"pane":null},"voice_on":false}"#

  do {
    _ = try ParleySnapshot.decode(Data(json.utf8))
    throw TestFailure("unknown contract version should fail")
  } catch let error as ParleyContractError {
    try expect(error == .unsupportedVersion(3), "wrong contract error")
  }
}

private func testMicrophoneFailuresOverridePidLiveness() throws {
  let cases: [(ParleyMicrophoneState, ParleyVisualState, String)] = [
    (.denied, .error, "permission"),
    (.unavailable, .degraded, "unavailable"),
    (.busy, .degraded, "busy"),
    (.failed, .error, "unexpectedly"),
    (.unknown, .degraded, "not yet confirmed"),
  ]

  for (state, visualState, detail) in cases {
    let presentation = ParleyPresentation.from(
      snapshot(
        state: "ready",
        running: true,
        microphone: state
      ))
    try expect(
      presentation.state == visualState,
      "\(state.rawValue) must not become ready from a live PID"
    )
    try expect(
      presentation.detail.lowercased().contains(detail),
      "\(state.rawValue) should have distinct recovery detail"
    )
  }
}

private func testMapsEveryOperationalState() throws {
  let cases: [(ParleySnapshot, ParleyVisualState)] = [
    (snapshot(state: "off", running: false), .off),
    (snapshot(state: "ready", running: true), .ready),
    (snapshot(state: "capturing", running: true), .listening),
    (snapshot(state: "sending", running: true), .sending),
    (snapshot(state: "ready", running: true, speaking: true), .speaking),
    (snapshot(state: "mystery", running: true), .degraded),
    (snapshot(state: "ready", running: true, targetAvailable: false), .degraded),
  ]

  for (status, expected) in cases {
    try expect(
      ParleyPresentation.from(status).state == expected,
      "\(status.listenerState) should map to \(expected.rawValue)"
    )
  }
}

private func testStateIconographyAndLabelsAreDistinct() throws {
  let labels = Set(ParleyVisualState.allCases.map(\.label))
  let icons = Set(ParleyVisualState.allCases.map(\.systemImage))

  try expect(
    labels.count == ParleyVisualState.allCases.count,
    "state labels should be distinct"
  )
  try expect(
    icons.count == ParleyVisualState.allCases.count,
    "state icons should be distinct"
  )
}

private func snapshot(
  state: String,
  running: Bool,
  speaking: Bool = false,
  targetAvailable: Bool = true,
  voiceOn: Bool = false,
  microphone: ParleyMicrophoneState = .ready
) -> ParleySnapshot {
  ParleySnapshot(
    contractVersion: 2,
    listenerRunning: running,
    listenerState: state,
    microphone: ParleyMicrophoneStatus(
      state: microphone,
      reason: microphone == .ready ? "capture_active" : "test_reason",
      selector: "uid:built-in",
      device: nil
    ),
    speaking: speaking,
    target: ParleyTarget(
      available: targetAvailable,
      label: targetAvailable ? "ivory lynx" : nil,
      pane: "%531"
    ),
    voiceOn: voiceOn
  )
}
