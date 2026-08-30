import Foundation

public enum ParleyControl: String, CaseIterable, Equatable, Sendable {
  case voiceOn
  case voiceOff
  case listenerOn
  case listenerOff
  case stopSpeech

  public func presentation(targetName: String) -> ParleyControlPresentation {
    switch self {
    case .voiceOn:
      ParleyControlPresentation(
        title: "Enable Voice for \(targetName)",
        hint: "Speaks future agent replies for this target."
      )
    case .voiceOff:
      ParleyControlPresentation(
        title: "Disable Voice for \(targetName)",
        hint:
          "Disables future spoken replies for this target, stops current speech, and clears queued speech. The listener keeps running."
      )
    case .listenerOn:
      ParleyControlPresentation(
        title: "Start Listener for \(targetName)",
        hint: "Starts hands-free listening for this target."
      )
    case .listenerOff:
      ParleyControlPresentation(
        title: "Stop Listener",
        hint: "Stops hands-free listening without changing voice output."
      )
    case .stopSpeech:
      ParleyControlPresentation(
        title: "Stop Speech Now",
        hint:
          "Stops current speech and clears queued speech without disabling future spoken replies."
      )
    }
  }
}

public struct ParleyControlPresentation: Equatable, Sendable {
  public let title: String
  public let hint: String

  public init(title: String, hint: String) {
    self.title = title
    self.hint = hint
  }
}

public struct ParleyCommand: Equatable, Sendable {
  public let arguments: [String]
  public let environment: [String: String]
  public let timeout: TimeInterval

  public init(
    arguments: [String],
    environment: [String: String] = [:],
    timeout: TimeInterval = 8
  ) {
    self.arguments = arguments
    self.environment = environment
    self.timeout = timeout
  }
}

public enum ParleyCommandPlanner {
  public static func command(
    for control: ParleyControl,
    snapshot: ParleySnapshot
  ) -> ParleyCommand? {
    let pane = snapshot.target.pane
    let targetEnvironment = pane.map { ["TMUX_PANE": $0] } ?? [:]

    switch control {
    case .voiceOn:
      guard !snapshot.voiceOn, snapshot.target.available, pane != nil else {
        return nil
      }
      return ParleyCommand(arguments: ["on"], environment: targetEnvironment)
    case .voiceOff:
      guard snapshot.voiceOn, pane != nil else { return nil }
      return ParleyCommand(arguments: ["off"], environment: targetEnvironment)
    case .listenerOn:
      guard !snapshot.listenerRunning, snapshot.target.available, pane != nil else {
        return nil
      }
      return ParleyCommand(
        arguments: ["listen", "on"],
        environment: targetEnvironment,
        timeout: 30
      )
    case .listenerOff:
      guard snapshot.listenerRunning else { return nil }
      return ParleyCommand(arguments: ["listen", "off"])
    case .stopSpeech:
      guard snapshot.speaking else { return nil }
      return ParleyCommand(arguments: ["stop"])
    }
  }
}
