import Foundation
import ParleyCore

@MainActor
final class ParleyModel: ObservableObject {
  @Published private(set) var snapshot: ParleySnapshot?
  @Published private(set) var errorMessage: String?
  @Published private(set) var isWorking = false
  @Published private(set) var lastUpdated: Date?

  let cli: ParleyCLI
  private let client: ParleyCLIClient
  private var pollingTask: Task<Void, Never>?

  init(cli: ParleyCLI = .discover()) {
    self.cli = cli
    client = ParleyCLIClient(cli: cli)
  }

  var presentation: ParleyPresentation {
    if let errorMessage {
      return .error(errorMessage)
    }
    if let snapshot {
      return .from(snapshot)
    }
    return ParleyPresentation(state: .off, detail: "Checking local status.")
  }

  func start() {
    guard pollingTask == nil else { return }
    refresh()
    pollingTask = Task { [weak self] in
      while !Task.isCancelled {
        try? await Task.sleep(for: .seconds(1))
        guard !Task.isCancelled else { return }
        await self?.loadStatus(showActivity: false)
      }
    }
  }

  func refresh() {
    Task { await loadStatus(showActivity: true) }
  }

  func perform(_ control: ParleyControl) {
    guard let snapshot,
      let command = ParleyCommandPlanner.command(
        for: control,
        snapshot: snapshot
      )
    else { return }
    Task {
      isWorking = true
      let result = await client.run(command)
      if case .failure(let error) = result {
        errorMessage = error.localizedDescription
      }
      await loadStatus(showActivity: false)
      isWorking = false
    }
  }

  func command(for control: ParleyControl) -> ParleyCommand? {
    snapshot.flatMap { ParleyCommandPlanner.command(for: control, snapshot: $0) }
  }

  private func loadStatus(showActivity: Bool) async {
    if showActivity { isWorking = true }
    switch await client.fetchStatus() {
    case .success(let value):
      snapshot = value
      errorMessage = nil
      lastUpdated = Date()
    case .failure(let error):
      errorMessage = error.localizedDescription
    }
    if showActivity { isWorking = false }
  }
}
