import ParleyCore
import SwiftUI

struct StatusWindowView: View {
  @ObservedObject var model: ParleyModel
  @ObservedObject var launchAtLogin: LaunchAtLoginController

  private var stateColor: Color {
    switch model.presentation.state {
    case .ready: .green
    case .listening: .red
    case .sending: .orange
    case .speaking: .blue
    case .degraded: .yellow
    case .error: .red
    case .off: .secondary
    }
  }

  var body: some View {
    VStack(alignment: .leading, spacing: 22) {
      HStack(spacing: 16) {
        Image(systemName: model.presentation.state.systemImage)
          .font(.system(size: 32, weight: .semibold))
          .foregroundStyle(stateColor)
          .frame(width: 58, height: 58)
          .background(stateColor.opacity(0.12), in: Circle())
          .accessibilityHidden(true)

        VStack(alignment: .leading, spacing: 4) {
          Text(model.presentation.state.label)
            .font(.title2.weight(.semibold))
          Text(model.presentation.detail)
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
        }
      }
      .accessibilityElement(children: .combine)
      .accessibilityLabel(model.presentation.accessibilityLabel)

      Grid(alignment: .leading, horizontalSpacing: 18, verticalSpacing: 10) {
        statusRow("Target", value: model.snapshot?.target.displayName ?? "Unavailable")
        statusRow(
          "Voice output",
          value: model.snapshot?.voiceOn == true ? "On" : "Off"
        )
        statusRow(
          "Hands-free listener",
          value: model.snapshot?.listenerRunning == true ? "On" : "Off"
        )
        statusRow("Launch at login", value: launchAtLogin.state.status.label)
        statusRow("CLI", value: model.cli.displayName)
        statusRow(
          "Last checked",
          value: model.lastUpdated?.formatted(date: .omitted, time: .standard)
            ?? "Not yet"
        )
      }
      .accessibilityElement(children: .contain)

      Divider()
      ControlButtons(model: model)

      Divider()
      LaunchAtLoginControl(controller: launchAtLogin, showsDetail: true)

      HStack {
        if model.isWorking {
          ProgressView()
            .controlSize(.small)
            .accessibilityLabel("Updating Parley status")
        }
        Spacer()
        Button("Refresh") {
          model.refresh()
        }
        .disabled(model.isWorking)
        .keyboardShortcut("r", modifiers: .command)
      }
    }
    .padding(26)
    .frame(width: 470)
    .onAppear { launchAtLogin.refresh() }
    .background(
      LinearGradient(
        colors: [Color.accentColor.opacity(0.06), .clear],
        startPoint: .topLeading,
        endPoint: .center
      )
    )
  }

  private func statusRow(_ label: String, value: String) -> some View {
    GridRow {
      Text(label)
        .foregroundStyle(.secondary)
      Text(value)
        .textSelection(.enabled)
    }
  }
}
