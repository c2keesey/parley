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
          control: .voiceOff,
          systemImage: "speaker.slash",
          shortcut: "1"
        )
      } else {
        controlButton(
          control: .voiceOn,
          systemImage: "speaker.wave.2",
          shortcut: "1"
        )
      }

      if model.snapshot?.listenerRunning == true {
        controlButton(
          control: .listenerOff,
          systemImage: "mic.slash",
          shortcut: "2"
        )
      } else {
        controlButton(
          control: .listenerOn,
          systemImage: "mic",
          shortcut: "2"
        )
      }

      controlButton(
        control: .stopSpeech,
        systemImage: "stop.circle",
        shortcut: "."
      )
    }
  }

  private func controlButton(
    control: ParleyControl,
    systemImage: String,
    shortcut: KeyEquivalent,
    modifiers: EventModifiers = .command
  ) -> some View {
    let presentation = control.presentation(targetName: targetName)
    return Button {
      model.perform(control)
    } label: {
      Label(presentation.title, systemImage: systemImage)
    }
    .disabled(model.command(for: control) == nil || model.isWorking)
    .keyboardShortcut(shortcut, modifiers: modifiers)
    .accessibilityLabel(presentation.title)
    .accessibilityHint(presentation.hint)
    .help(presentation.hint)
  }
}
