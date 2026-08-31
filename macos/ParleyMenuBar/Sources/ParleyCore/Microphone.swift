import Foundation

public enum ParleyMicrophoneState: String, Codable, CaseIterable, Equatable, Sendable {
  case unknown
  case denied
  case unavailable
  case busy
  case ready
  case failed

  public var label: String {
    switch self {
    case .unknown: "Unknown"
    case .denied: "Permission denied"
    case .unavailable: "Device unavailable"
    case .busy: "Device busy"
    case .ready: "Capture ready"
    case .failed: "Capture failed"
    }
  }

  public var systemImage: String {
    switch self {
    case .unknown: "questionmark.circle"
    case .denied: "hand.raised.slash"
    case .unavailable: "mic.slash"
    case .busy: "clock.badge.exclamationmark"
    case .ready: "mic.circle.fill"
    case .failed: "exclamationmark.octagon"
    }
  }
}

public struct ParleyMicrophoneDevice: Codable, Equatable, Sendable, Identifiable {
  public let index: Int
  public let name: String
  public let uid: String?
  public let serial: String?
  public let supportsStableSelector: Bool
  public let selector: String

  public init(
    index: Int,
    name: String,
    uid: String?,
    serial: String?,
    supportsStableSelector: Bool,
    selector: String
  ) {
    self.index = index
    self.name = name
    self.uid = uid
    self.serial = serial
    self.supportsStableSelector = supportsStableSelector
    self.selector = selector
  }

  public var id: String { selector }
}

public struct ParleyMicrophoneStatus: Codable, Equatable, Sendable {
  public let state: ParleyMicrophoneState
  public let reason: String
  public let selector: String
  public let device: ParleyMicrophoneDevice?

  public init(
    state: ParleyMicrophoneState,
    reason: String,
    selector: String,
    device: ParleyMicrophoneDevice?
  ) {
    self.state = state
    self.reason = reason
    self.selector = selector
    self.device = device
  }
}

public struct ParleyMicrophoneInventory: Codable, Equatable, Sendable {
  public let contractVersion: Int
  public let available: Bool
  public let devices: [ParleyMicrophoneDevice]

  public static func decode(_ data: Data) throws -> ParleyMicrophoneInventory {
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    let inventory = try decoder.decode(ParleyMicrophoneInventory.self, from: data)
    guard inventory.contractVersion == 1 else {
      throw ParleyContractError.unsupportedMicrophoneVersion(
        inventory.contractVersion
      )
    }
    return inventory
  }
}

public enum ParleyMicrophoneRecovery {
  public static let systemSettingsPane =
    "System Settings > Privacy & Security > Microphone"
  public static let systemSettingsURL = URL(
    string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
  )!

  public static func summary(_ status: ParleyMicrophoneStatus) -> String {
    switch status.state {
    case .unknown:
      "Capture has not been verified. Starting the listener may show a macOS prompt."
    case .denied:
      "The capture host was denied microphone access. Parley cannot change that choice."
    case .unavailable:
      "The selected microphone is missing or its numeric index now identifies another device."
    case .busy:
      "Another application is holding the selected microphone."
    case .ready:
      status.device.map { "Capture is delivering frames from \($0.name)." }
        ?? "The microphone capture stream is delivering frames."
    case .failed:
      "Capture failed for a reason Parley does not safely recognize."
    }
  }

  public static func action(_ status: ParleyMicrophoneStatus) -> String {
    switch status.state {
    case .unknown:
      "Start the listener when ready, then respond to any permission prompt yourself."
    case .denied:
      "Review \(systemSettingsPane), then retry the listener."
    case .unavailable:
      "Choose an available microphone below. Parley will reject later identity drift."
    case .busy:
      "Release the microphone in the other application, then retry."
    case .ready:
      "No recovery action is needed."
    case .failed:
      "Retry once. If it fails again, run `parley mic status` in Terminal."
    }
  }
}
