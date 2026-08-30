import Foundation
import ParleyCore

func runStatusTests() throws {
  try testDecodesVersionedStatusContract()
  try testRejectsUnknownContractVersion()
  try testMapsEveryOperationalState()
  try testStateIconographyAndLabelsAreDistinct()
}

private func testDecodesVersionedStatusContract() throws {
  let json =
    #"{"cli_version":"0.5.4","contract_version":1,"listener_running":true,"listener_state":"ready","speaking":false,"target":{"available":true,"label":"ivory lynx","pane":"%531"},"voice_on":true}"#

  let status = try ParleySnapshot.decode(Data(json.utf8))

  try expect(status.contractVersion == 1, "contract version should decode")
  try expect(status.cliVersion == "0.5.4", "CLI version should decode")
  try expect(status.target.displayName == "ivory lynx", "target should decode")
  try expect(status.voiceOn, "voice state should decode")
}

private func testRejectsUnknownContractVersion() throws {
  let json =
    #"{"cli_version":"0.5.4","contract_version":2,"listener_running":false,"listener_state":"off","speaking":false,"target":{"available":false,"label":null,"pane":null},"voice_on":false}"#

  do {
    _ = try ParleySnapshot.decode(Data(json.utf8))
    throw TestFailure("unknown contract version should fail")
  } catch let error as ParleyContractError {
    try expect(error == .unsupportedVersion(2), "wrong contract error")
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
  voiceOn: Bool = false
) -> ParleySnapshot {
  ParleySnapshot(
    cliVersion: "0.5.4",
    contractVersion: 1,
    listenerRunning: running,
    listenerState: state,
    speaking: speaking,
    target: ParleyTarget(
      available: targetAvailable,
      label: targetAvailable ? "ivory lynx" : nil,
      pane: "%531"
    ),
    voiceOn: voiceOn
  )
}
