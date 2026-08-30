import Foundation
import ParleyCore
import ServiceManagement

@MainActor
final class LaunchAtLoginController: ObservableObject {
  @Published private(set) var state: LaunchAtLoginState
  @Published private(set) var isWorking = false

  private let service: SMAppService

  init(service: SMAppService = .mainApp) {
    self.service = service
    state = Self.readState(from: service)
  }

  func refresh() {
    guard !isWorking else { return }
    state = Self.readState(from: service)
  }

  func setRequested(_ requested: Bool) {
    guard !isWorking,
      let operation = LaunchAtLoginPlanner.operation(
        toSet: requested,
        from: state.status
      )
    else {
      refresh()
      return
    }

    isWorking = true
    defer { isWorking = false }

    do {
      switch operation {
      case .register:
        try service.register()
      case .unregister:
        try service.unregister()
      }
      state = Self.readState(from: service)
    } catch {
      let latest = Self.readState(from: service)
      if latest.failureMessage == nil,
        latest.status.canChange,
        latest.status.isRequested == requested
      {
        // Another caller completed the same request between our status read
        // and mutation. Treat the already-satisfied result as success.
        state = latest
      } else {
        state = LaunchAtLoginState(
          status: latest.status,
          failureMessage: error.localizedDescription
        )
      }
    }
  }

  private static func readState(from service: SMAppService) -> LaunchAtLoginState {
    switch service.status {
    case .notRegistered:
      LaunchAtLoginState(status: .notRegistered)
    case .enabled:
      LaunchAtLoginState(status: .enabled)
    case .requiresApproval:
      LaunchAtLoginState(status: .requiresApproval)
    case .notFound:
      LaunchAtLoginState(status: .notFound)
    @unknown default:
      LaunchAtLoginState(
        status: .notFound,
        failureMessage: "macOS returned an unrecognized login-item status."
      )
    }
  }
}
