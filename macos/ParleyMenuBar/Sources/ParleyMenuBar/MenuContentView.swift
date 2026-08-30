import AppKit
import SwiftUI

struct MenuContentView: View {
  @ObservedObject var model: ParleyModel
  @ObservedObject var launchAtLogin: LaunchAtLoginController
  @Environment(\.openWindow) private var openWindow

  var body: some View {
    Text("Parley · \(model.presentation.state.label)")
      .accessibilityLabel(model.presentation.accessibilityLabel)
    Text(model.presentation.detail)
    if let target = model.snapshot?.target {
      Text("Target: \(target.displayName)\(target.available ? "" : " · unavailable")")
    }

    Divider()
    ControlButtons(model: model)
    Divider()
    LaunchAtLoginControl(controller: launchAtLogin)
      .onAppear { launchAtLogin.refresh() }
    Divider()

    Button {
      NSApp.activate(ignoringOtherApps: true)
      openWindow(id: "status")
    } label: {
      Label("Open Status…", systemImage: "info.circle")
    }
    .keyboardShortcut(",", modifiers: .command)
    .accessibilityHint("Opens detailed Parley status in a window.")

    Button {
      model.refresh()
    } label: {
      Label("Refresh", systemImage: "arrow.clockwise")
    }
    .disabled(model.isWorking)
    .keyboardShortcut("r", modifiers: .command)
    .accessibilityHint("Refreshes local Parley status.")

    Divider()
    Button {
      NSApplication.shared.terminate(nil)
    } label: {
      Label("Quit Menu Bar App", systemImage: "xmark.circle")
    }
    .keyboardShortcut("q", modifiers: .command)
    .accessibilityHint(
      "Closes only this menu-bar interface. Voice, the listener, and Launch at Login keep their current settings."
    )
  }
}
