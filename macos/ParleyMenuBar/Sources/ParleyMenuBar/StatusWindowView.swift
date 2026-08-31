import AppKit
import ParleyCore
import SwiftUI

struct StatusWindowView: View {
  @ObservedObject var model: ParleyModel

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
    ScrollView {
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
          statusRow(
            "Microphone",
            value: model.snapshot?.microphone.state.label ?? "Checking"
          )
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

        if let microphone = model.snapshot?.microphone {
          microphoneRecovery(microphone)
        }

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
    }
    .frame(width: 510)
    .background(
      LinearGradient(
        colors: [Color.accentColor.opacity(0.06), .clear],
        startPoint: .topLeading,
        endPoint: .center
      )
    )
  }

  @ViewBuilder
  private func microphoneRecovery(_ status: ParleyMicrophoneStatus) -> some View {
    VStack(alignment: .leading, spacing: 12) {
      Label("Microphone · \(status.state.label)", systemImage: status.state.systemImage)
        .font(.headline)
      Text(ParleyMicrophoneRecovery.summary(status))
        .foregroundStyle(.secondary)
      Text(ParleyMicrophoneRecovery.action(status))
        .font(.callout)

      if status.state == .denied {
        Button("Open Microphone Settings") {
          NSWorkspace.shared.open(ParleyMicrophoneRecovery.systemSettingsURL)
        }
        .accessibilityHint(
          "Opens \(ParleyMicrophoneRecovery.systemSettingsPane); Parley does not change access."
        )
      }

      Divider()
      Text("Available microphones")
        .font(.subheadline.weight(.semibold))
      if let microphoneError = model.microphoneError {
        Text(microphoneError)
          .font(.callout)
          .foregroundStyle(.secondary)
      } else if model.microphones.isEmpty {
        Text("No microphone devices were found.")
          .font(.callout)
          .foregroundStyle(.secondary)
      } else {
        ForEach(model.microphones) { device in
          HStack {
            VStack(alignment: .leading, spacing: 2) {
              Text(device.name)
              Text("Current index \(device.index) · \(device.selector)")
                .font(.caption)
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
            }
            Spacer()
            Button("Use") {
              model.useMicrophone(device)
            }
            .disabled(model.isWorking || model.snapshot?.target.available != true)
            .accessibilityLabel("Use \(device.name) for hands-free listening")
            .accessibilityHint(
              "Explicitly starts the listener with this microphone; macOS may ask for permission."
            )
          }
        }
      }
    }
    .padding(16)
    .background(Color.secondary.opacity(0.08), in: RoundedRectangle(cornerRadius: 14))
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
