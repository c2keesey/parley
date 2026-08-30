import Foundation

public struct ParleyTarget: Codable, Equatable, Sendable {
  public let available: Bool
  public let label: String?
  public let pane: String?

  public init(available: Bool, label: String?, pane: String?) {
    self.available = available
    self.label = label
    self.pane = pane
  }

  public var displayName: String {
    label ?? pane ?? "No target"
  }
}

public struct ParleySnapshot: Codable, Equatable, Sendable {
  public let contractVersion: Int
  public let listenerRunning: Bool
  public let listenerState: String
  public let speaking: Bool
  public let target: ParleyTarget
  public let voiceOn: Bool

  public init(
    contractVersion: Int,
    listenerRunning: Bool,
    listenerState: String,
    speaking: Bool,
    target: ParleyTarget,
    voiceOn: Bool
  ) {
    self.contractVersion = contractVersion
    self.listenerRunning = listenerRunning
    self.listenerState = listenerState
    self.speaking = speaking
    self.target = target
    self.voiceOn = voiceOn
  }

  public static func decode(_ data: Data) throws -> ParleySnapshot {
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    let snapshot = try decoder.decode(ParleySnapshot.self, from: data)
    guard snapshot.contractVersion == 1 else {
      throw ParleyContractError.unsupportedVersion(snapshot.contractVersion)
    }
    return snapshot
  }
}

public enum ParleyContractError: Error, Equatable, LocalizedError, Sendable {
  case unsupportedVersion(Int)

  public var errorDescription: String? {
    switch self {
    case .unsupportedVersion(let version):
      "Unsupported Parley status contract version \(version)."
    }
  }
}

public enum ParleyVisualState: String, CaseIterable, Equatable, Sendable {
  case off
  case ready
  case listening
  case sending
  case speaking
  case degraded
  case error

  public var label: String {
    switch self {
    case .off: "Off"
    case .ready: "Ready"
    case .listening: "Listening"
    case .sending: "Sending"
    case .speaking: "Speaking"
    case .degraded: "Degraded"
    case .error: "Error"
    }
  }

  public var systemImage: String {
    switch self {
    case .off: "waveform.slash"
    case .ready: "mic.circle"
    case .listening: "waveform.circle.fill"
    case .sending: "arrow.up.circle.fill"
    case .speaking: "speaker.wave.2.circle.fill"
    case .degraded: "exclamationmark.triangle.fill"
    case .error: "exclamationmark.octagon.fill"
    }
  }
}

public struct ParleyPresentation: Equatable, Sendable {
  public let state: ParleyVisualState
  public let detail: String

  public init(state: ParleyVisualState, detail: String) {
    self.state = state
    self.detail = detail
  }

  public var accessibilityLabel: String {
    "Parley \(state.label). \(detail)"
  }

  public static func from(_ snapshot: ParleySnapshot) -> ParleyPresentation {
    if snapshot.listenerRunning && !snapshot.target.available {
      return ParleyPresentation(
        state: .degraded,
        detail: "Listener target \(snapshot.target.pane ?? "is missing") is unavailable."
      )
    }

    switch snapshot.listenerState {
    case "capturing":
      return ParleyPresentation(
        state: .listening,
        detail: "Capturing for \(snapshot.target.displayName)."
      )
    case "sending":
      return ParleyPresentation(
        state: .sending,
        detail: "Transcribing and sending to \(snapshot.target.displayName)."
      )
    case "ready", "off":
      break
    default:
      return ParleyPresentation(
        state: .degraded,
        detail: "Unknown listener state: \(snapshot.listenerState)."
      )
    }

    if snapshot.speaking {
      return ParleyPresentation(
        state: .speaking,
        detail: "Speaking for \(snapshot.target.displayName)."
      )
    }
    if snapshot.listenerRunning {
      return ParleyPresentation(
        state: .ready,
        detail: "Microphone ready for \(snapshot.target.displayName)."
      )
    }
    return ParleyPresentation(
      state: .off,
      detail: snapshot.voiceOn
        ? "Listener off; spoken replies remain on for \(snapshot.target.displayName)."
        : "Voice and listener are off."
    )
  }

  public static func error(_ message: String) -> ParleyPresentation {
    ParleyPresentation(state: .error, detail: message)
  }
}
