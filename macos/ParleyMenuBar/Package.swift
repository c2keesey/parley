// swift-tools-version: 6.0
import PackageDescription

let package = Package(
  name: "ParleyMenuBar",
  platforms: [.macOS(.v13)],
  products: [
    .executable(name: "ParleyMenuBar", targets: ["ParleyMenuBar"]),
    .library(name: "ParleyCore", targets: ["ParleyCore"]),
    .executable(name: "ParleyCoreTests", targets: ["ParleyCoreTests"]),
  ],
  targets: [
    .target(name: "ParleyCore"),
    .executableTarget(
      name: "ParleyMenuBar",
      dependencies: ["ParleyCore"]
    ),
    .executableTarget(
      name: "ParleyCoreTests",
      dependencies: ["ParleyCore"],
      path: "Tests/ParleyCoreTests"
    ),
  ],
  swiftLanguageModes: [.v6]
)
