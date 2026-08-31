import Foundation
import ParleyCore

func runMicrophoneTests() throws {
  try testDecodesPrivacySafeInventory()
  try testRejectsUnknownMicrophoneContractVersion()
  try testEveryMicrophoneStateHasDistinctGuidance()
  try testPermissionRecoveryNamesAndLinksCorrectPane()
}

private func testDecodesPrivacySafeInventory() throws {
  let json =
    #"{"available":true,"contract_version":1,"devices":[{"index":2,"name":"USB Mic","serial":"serial-1","selector":"uid:stable-uid","uid":"stable-uid"}]}"#

  let inventory = try ParleyMicrophoneInventory.decode(Data(json.utf8))

  try expect(inventory.available, "inventory should be available")
  try expect(inventory.devices.count == 1, "one microphone should decode")
  try expect(
    inventory.devices[0].selector == "uid:stable-uid",
    "stable device selector should decode"
  )
}

private func testRejectsUnknownMicrophoneContractVersion() throws {
  let json = #"{"available":true,"contract_version":2,"devices":[]}"#

  do {
    _ = try ParleyMicrophoneInventory.decode(Data(json.utf8))
    throw TestFailure("unknown microphone version should fail")
  } catch let error as ParleyContractError {
    try expect(
      error == .unsupportedMicrophoneVersion(2),
      "wrong microphone contract error"
    )
  }
}

private func testEveryMicrophoneStateHasDistinctGuidance() throws {
  let statuses = ParleyMicrophoneState.allCases.map { state in
    ParleyMicrophoneStatus(
      state: state,
      reason: "bounded_reason",
      selector: "0",
      device: nil
    )
  }
  let labels = Set(statuses.map { $0.state.label })
  let summaries = Set(statuses.map(ParleyMicrophoneRecovery.summary))
  let actions = Set(statuses.map(ParleyMicrophoneRecovery.action))

  try expect(labels.count == statuses.count, "state labels should be distinct")
  try expect(
    summaries.count == statuses.count,
    "state summaries should be distinct"
  )
  try expect(actions.count == statuses.count, "recovery actions should be distinct")
}

private func testPermissionRecoveryNamesAndLinksCorrectPane() throws {
  let denied = ParleyMicrophoneStatus(
    state: .denied,
    reason: "permission_denied",
    selector: "0",
    device: nil
  )

  try expect(
    ParleyMicrophoneRecovery.action(denied).contains(
      "System Settings > Privacy & Security > Microphone"
    ),
    "denied recovery should name the exact pane"
  )
  try expect(
    ParleyMicrophoneRecovery.systemSettingsURL.absoluteString.contains(
      "Privacy_Microphone"
    ),
    "settings URL should deep-link the microphone pane"
  )
}
