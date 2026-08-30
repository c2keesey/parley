import AppKit
import SwiftUI

struct MenuContentView: View {
  @ObservedObject var model: ParleyModel
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
    Button("Quit Parley") {
      NSApplication.shared.terminate(nil)
    }
    .keyboardShortcut("q", modifiers: .command)
    .accessibilityHint("Quits only the menu-bar app, not the Parley listener.")
  }
}
