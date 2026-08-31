import Foundation

public struct ParleyCLI: Equatable, Sendable {
  public let executableURL: URL
  public let prefixArguments: [String]
  public let displayName: String

  public init(
    executableURL: URL,
    prefixArguments: [String] = [],
    displayName: String
  ) {
    self.executableURL = executableURL
    self.prefixArguments = prefixArguments
    self.displayName = displayName
  }

  public static func discover(
    environment: [String: String] = ProcessInfo.processInfo.environment,
    fileManager: FileManager = .default
  ) -> ParleyCLI {
    if let override = environment["PARLEY_CLI"],
      !override.isEmpty,
      fileManager.isExecutableFile(atPath: override)
    {
      return ParleyCLI(
        executableURL: URL(fileURLWithPath: override),
        displayName: override
      )
    }

    let home = environment["HOME"] ?? NSHomeDirectory()
    let candidates = [
      "\(home)/.local/bin/parley",
      "/opt/homebrew/bin/parley",
      "/usr/local/bin/parley",
    ]
    if let candidate = candidates.first(where: fileManager.isExecutableFile(atPath:)) {
      return ParleyCLI(
        executableURL: URL(fileURLWithPath: candidate),
        displayName: candidate
      )
    }

    return ParleyCLI(
      executableURL: URL(fileURLWithPath: "/usr/bin/env"),
      prefixArguments: ["parley"],
      displayName: "parley (PATH)"
    )
  }
}

public enum ParleyCLIError: Error, Equatable, LocalizedError, Sendable {
  case launch(String)
  case timedOut
  case failed(Int32, String)
  case invalidStatus(String)

  public var errorDescription: String? {
    switch self {
    case .launch(let message):
      "Parley CLI could not launch: \(message)"
    case .timedOut:
      "Parley CLI did not respond in time."
    case .failed(_, let message):
      message.isEmpty ? "Parley CLI returned an error." : message
    case .invalidStatus(let message):
      "Parley status is incompatible: \(message)"
    }
  }
}

public actor ParleyCLIClient {
  public let cli: ParleyCLI

  public init(cli: ParleyCLI = .discover()) {
    self.cli = cli
  }

  public func fetchStatus() -> Result<ParleySnapshot, ParleyCLIError> {
    switch execute(ParleyCommand(arguments: ["status", "--json"], timeout: 3)) {
    case .failure(let error):
      return .failure(error)
    case .success(let data):
      do {
        return .success(try ParleySnapshot.decode(data))
      } catch {
        return .failure(.invalidStatus(error.localizedDescription))
      }
    }
  }

  public func fetchMicrophones() -> Result<ParleyMicrophoneInventory, ParleyCLIError> {
    switch execute(
      ParleyCommand(arguments: ["mic", "devices", "--json"], timeout: 8)
    ) {
    case .failure(let error):
      return .failure(error)
    case .success(let data):
      do {
        return .success(try ParleyMicrophoneInventory.decode(data))
      } catch {
        return .failure(.invalidStatus(error.localizedDescription))
      }
    }
  }

  public func run(_ command: ParleyCommand) -> Result<Void, ParleyCLIError> {
    execute(command).map { _ in () }
  }

  private func execute(_ command: ParleyCommand) -> Result<Data, ParleyCLIError> {
    let process = Process()
    let standardOutput = Pipe()
    let standardError = Pipe()
    process.executableURL = cli.executableURL
    process.arguments = cli.prefixArguments + command.arguments
    process.standardOutput = standardOutput
    process.standardError = standardError
    process.standardInput = FileHandle.nullDevice
    process.environment = ProcessInfo.processInfo.environment.merging(
      command.environment,
      uniquingKeysWith: { _, commandValue in commandValue }
    )

    do {
      try process.run()
    } catch {
      return .failure(.launch(error.localizedDescription))
    }

    let deadline = Date().addingTimeInterval(command.timeout)
    while process.isRunning && Date() < deadline {
      Thread.sleep(forTimeInterval: 0.02)
    }
    if process.isRunning {
      process.terminate()
      process.waitUntilExit()
      return .failure(.timedOut)
    }

    let output = standardOutput.fileHandleForReading.readDataToEndOfFile()
    let errorData = standardError.fileHandleForReading.readDataToEndOfFile()
    guard process.terminationStatus == 0 else {
      let rawMessage =
        String(data: errorData, encoding: .utf8)?
        .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
      let message =
        rawMessage.split(whereSeparator: \.isNewline).last.map(String.init)
        ?? rawMessage
      return .failure(.failed(process.terminationStatus, message))
    }
    return .success(output)
  }
}
