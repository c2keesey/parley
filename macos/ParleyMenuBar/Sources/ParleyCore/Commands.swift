import Foundation

public enum ParleyControl: String, CaseIterable, Equatable, Sendable {
  case voiceOn
  case voiceOff
  case listenerOn
  case listenerOff
  case stopSpeech
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
