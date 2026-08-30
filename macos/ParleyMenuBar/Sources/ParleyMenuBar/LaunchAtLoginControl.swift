import SwiftUI

struct LaunchAtLoginControl: View {
  @ObservedObject var controller: LaunchAtLoginController
  var showsDetail = false

  private var requested: Binding<Bool> {
    Binding(
      get: { controller.state.status.isRequested },
      set: { controller.setRequested($0) }
    )
  }

  var body: some View {
    Group {
      Toggle(isOn: requested) {
        Label("Launch at Login", systemImage: "power")
      }
      .disabled(!controller.state.status.canChange || controller.isWorking)
      .keyboardShortcut("l", modifiers: .command)
      .accessibilityLabel("Launch Parley Menu Bar at Login")
      .accessibilityValue(controller.state.status.label)
      .accessibilityHint(controller.state.detail)
      .help(controller.state.detail)

      if showsDetail || controller.state.status == .requiresApproval
        || controller.state.status == .notFound
        || controller.state.failureMessage != nil
      {
        Text("Login item: \(controller.state.status.label). \(controller.state.detail)")
          .font(.caption)
          .foregroundStyle(
            controller.state.failureMessage == nil ? Color.secondary : Color.red
          )
          .fixedSize(horizontal: false, vertical: true)
          .accessibilityLabel(
            "Launch at Login, \(controller.state.status.label). \(controller.state.detail)"
          )
      }
    }
  }
}
