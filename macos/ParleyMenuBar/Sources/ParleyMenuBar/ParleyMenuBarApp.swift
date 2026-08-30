import AppKit
import SwiftUI

@main
struct ParleyMenuBarApp: App {
  @StateObject private var model = ParleyModel()
  @StateObject private var launchAtLogin = LaunchAtLoginController()

  init() {
    NSApplication.shared.setActivationPolicy(.accessory)
  }

  var body: some Scene {
    MenuBarExtra {
      MenuContentView(model: model, launchAtLogin: launchAtLogin)
    } label: {
      Label(
        model.presentation.state.label,
        systemImage: model.presentation.state.systemImage
      )
      .accessibilityLabel(model.presentation.accessibilityLabel)
      .onAppear { model.start() }
    }
    .menuBarExtraStyle(.menu)

    Window("Parley Status", id: "status") {
      StatusWindowView(model: model, launchAtLogin: launchAtLogin)
        .onAppear { model.start() }
    }
    .defaultSize(width: 470, height: 520)
    .windowResizability(.contentSize)
  }
}
