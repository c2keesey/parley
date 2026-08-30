import Foundation

struct TestFailure: Error, CustomStringConvertible {
  let description: String

  init(_ description: String) {
    self.description = description
  }
}

func expect(
  _ condition: @autoclosure () -> Bool,
  _ message: String
) throws {
  guard condition() else { throw TestFailure(message) }
}

@main
struct ParleyCoreTestRunner {
  static func main() {
    do {
      try runStatusTests()
      try runCommandTests()
      try runLaunchAtLoginTests()
      print("ParleyCoreTests: all status, command, and login-item tests passed")
    } catch {
      let message = "ParleyCoreTests: FAILED: \(error)\n"
      FileHandle.standardError.write(Data(message.utf8))
      exit(1)
    }
  }
}
