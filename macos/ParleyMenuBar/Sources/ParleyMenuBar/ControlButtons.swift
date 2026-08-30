import ParleyCore
import SwiftUI

struct ControlButtons: View {
  @ObservedObject var model: ParleyModel

  private var targetName: String {
    model.snapshot?.target.displayName ?? "target"
  }

  var body: some View {
    Group {
      if model.snapshot?.voiceOn == true {
        controlButton(
          "Turn Voice Off for \(targetName)",
          systemImage: "speaker.slash",
          control: .voiceOff,
          shortcut: "1",
          hint: "Stops future spoken agent replies for this target."
        )
      } else {
        controlButton(
          "Turn Voice On for \(targetName)",
          systemImage: "speaker.wave.2",
          control: .voiceOn,
          shortcut: "1",
          hint: "Speaks future agent replies for this target."
        )
      }

      if model.snapshot?.listenerRunning == true {
        controlButton(
          "Turn Listener Off",
          systemImage: "mic.slash",
          control: .listenerOff,
          shortcut: "2",
          hint: "Stops hands-free listening without changing voice output."
        )
      } else {
        controlButton(
          "Turn Listener On for \(targetName)",
          systemImage: "mic",
          control: .listenerOn,
          shortcut: "2",
          hint: "Starts hands-free listening for this target."
        )
      }

      controlButton(
        "Stop Speech",
        systemImage: "stop.circle",
        control: .stopSpeech,
        shortcut: ".",
        hint: "Stops current speech and clears queued speech."
      )
    }
  }

  private func controlButton(
    _ title: String,
    systemImage: String,
    control: ParleyControl,
    shortcut: KeyEquivalent,
    hint: String
  ) -> some View {
    Button {
      model.perform(control)
    } label: {
      Label(title, systemImage: systemImage)
    }
    .disabled(model.command(for: control) == nil || model.isWorking)
    .keyboardShortcut(shortcut, modifiers: .command)
    .accessibilityLabel(title)
    .accessibilityHint(hint)
    .help(hint)
  }
}
