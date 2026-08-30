import Foundation

public enum LaunchAtLoginStatus: String, CaseIterable, Equatable, Sendable {
  case notRegistered
  case enabled
  case requiresApproval
  case notFound

  public var label: String {
    switch self {
    case .notRegistered: "Off"
    case .enabled: "On"
    case .requiresApproval: "Approval Required"
    case .notFound: "Unavailable"
    }
  }

  public var detail: String {
    switch self {
    case .notRegistered:
      "Parley opens only when you start it."
    case .enabled:
      "The menu-bar app opens automatically when you log in."
    case .requiresApproval:
      "Launch at login was requested, but macOS requires approval in Login Items."
    case .notFound:
      "macOS could not find a supported login item for this app bundle."
    }
  }

  public var isRequested: Bool {
    self == .enabled || self == .requiresApproval
  }

  public var canChange: Bool {
    self != .notFound
  }
}

public enum LaunchAtLoginOperation: Equatable, Sendable {
  case register
  case unregister
}

public enum LaunchAtLoginPlanner {
  public static func operation(
    toSet requested: Bool,
    from status: LaunchAtLoginStatus
  ) -> LaunchAtLoginOperation? {
    guard status.canChange, status.isRequested != requested else { return nil }
    return requested ? .register : .unregister
  }
}

public struct LaunchAtLoginState: Equatable, Sendable {
  public let status: LaunchAtLoginStatus
  public let failureMessage: String?

  public init(
    status: LaunchAtLoginStatus,
    failureMessage: String? = nil
  ) {
    self.status = status
    self.failureMessage = failureMessage
  }

  public var detail: String {
    guard let failureMessage else { return status.detail }
    return "The change failed: \(failureMessage) Current status: \(status.label)."
  }
}
