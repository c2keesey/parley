import ParleyCore

func runLaunchAtLoginTests() throws {
  try testModelsEveryNativeLoginItemStatus()
  try testPlansIdempotentReversibleOperations()
  try testPreservesStatusWhenAnOperationFails()
}

private func testModelsEveryNativeLoginItemStatus() throws {
  let expectedLabels: [LaunchAtLoginStatus: String] = [
    .notRegistered: "Off",
    .enabled: "On",
    .requiresApproval: "Approval Required",
    .notFound: "Unavailable",
  ]

  try expect(
    Set(LaunchAtLoginStatus.allCases) == Set(expectedLabels.keys),
    "every native SMAppService status should be modeled"
  )
  for (status, label) in expectedLabels {
    try expect(status.label == label, "\(status) should have honest status wording")
    try expect(!status.detail.isEmpty, "\(status) should explain its meaning")
  }
  try expect(
    LaunchAtLoginStatus.requiresApproval.isRequested,
    "approval-required should remain visibly requested"
  )
  try expect(
    !LaunchAtLoginStatus.notFound.canChange,
    "a missing service should not expose a control that cannot work"
  )
}

private func testPlansIdempotentReversibleOperations() throws {
  try expect(
    LaunchAtLoginPlanner.operation(toSet: true, from: .notRegistered) == .register,
    "turning login launch on should register"
  )
  try expect(
    LaunchAtLoginPlanner.operation(toSet: false, from: .enabled) == .unregister,
    "turning login launch off should unregister"
  )
  try expect(
    LaunchAtLoginPlanner.operation(toSet: false, from: .requiresApproval) == .unregister,
    "an approval-pending request should remain reversible"
  )
  try expect(
    LaunchAtLoginPlanner.operation(toSet: true, from: .enabled) == nil,
    "repeated enable should be a no-op"
  )
  try expect(
    LaunchAtLoginPlanner.operation(toSet: false, from: .notRegistered) == nil,
    "repeated disable should be a no-op"
  )
  try expect(
    LaunchAtLoginPlanner.operation(toSet: true, from: .notFound) == nil,
    "a missing service should not attempt registration"
  )
}

private func testPreservesStatusWhenAnOperationFails() throws {
  let state = LaunchAtLoginState(
    status: .notRegistered,
    failureMessage: "The app bundle is not eligible."
  )

  try expect(
    state.detail.contains("Current status: Off."),
    "failure wording should retain the last system status"
  )
  try expect(
    state.detail.contains("not eligible"),
    "failure wording should retain the native error"
  )
}
