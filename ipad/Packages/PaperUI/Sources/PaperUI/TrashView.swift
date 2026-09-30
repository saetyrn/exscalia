import PaperCore
import SwiftUI

/// Deleted notebooks and pages, and the one button that matters.
///
/// The server keeps a deleted package for thirty days, so "I deleted the wrong
/// notebook" is a question with an answer long after the six-second undo toast
/// has gone. Restoring puts the same bytes back under the same ids: it is a
/// move, never a re-creation, so nothing about the notebook changes except that
/// it exists again.
public struct TrashView: View {
    private let controller: PaperController

    @Environment(\.dismiss) private var dismiss
    @State private var items: [PaperTrashItem] = []
    @State private var isLoading = true
    @State private var message: String?
    @State private var busy: String?
    /// The last load did not get an answer, so an empty list proves nothing.
    @State private var loadFailed = false

    public init(controller: PaperController) {
        self.controller = controller
    }

    public var body: some View {
        NavigationStack {
            List {
                if let message {
                    Section {
                        Text(message)
                            .font(.footnote)
                            .foregroundStyle(PaperTokens.textMuted)
                    }
                }
                if isLoading {
                    row("Loading the trash")
                } else if items.isEmpty {
                    if !loadFailed {
                        row("Nothing has been deleted in the last thirty days")
                    }
                } else {
                    ForEach(items) { item in
                        entry(item)
                    }
                }
            }
            .listStyle(.insetGrouped)
            .navigationTitle("Trash")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") { dismiss() }
                        .accessibilityHint("Closes the trash")
                }
            }
            .refreshable { await load() }
        }
        .task { await load() }
    }

    private func entry(_ item: PaperTrashItem) -> some View {
        HStack(spacing: 12) {
            Image(systemName: item.kind == "notebook" ? "books.vertical" : "doc")
                .font(.system(size: 16, weight: .regular))
                .symbolRenderingMode(.monochrome)
                .foregroundStyle(PaperTokens.textMuted)
                .frame(width: 24)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(item.displayTitle)
                    .font(.body)
                    .foregroundStyle(PaperTokens.text)
                    .lineLimit(1)
                Text(subtitle(for: item))
                    .font(.caption)
                    .foregroundStyle(PaperTokens.textMuted)
            }
            Spacer(minLength: 0)
            Button("Restore") {
                Task { await restore(item) }
            }
            .buttonStyle(.bordered)
            .tint(PaperTokens.accent)
            .disabled(busy != nil)
            .accessibilityHint("Puts this back where it was")
        }
        .frame(minHeight: PaperTokens.minimumTarget)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(item.kind == "notebook" ? "Notebook" : "Page") \(item.displayTitle)")
    }

    private func row(_ text: String) -> some View {
        Text(text)
            .font(.footnote)
            .foregroundStyle(PaperTokens.textMuted)
    }

    private func subtitle(for item: PaperTrashItem) -> String {
        var parts: [String] = []
        if let deleted = item.deletedAt.flatMap(PaperDate.parse) {
            parts.append("Deleted \(SaveState.relative(since: deleted))")
        }
        if let expires = item.expiresAt.flatMap(PaperDate.parse) {
            parts.append("kept until \(expires.formatted(date: .abbreviated, time: .omitted))")
        }
        return parts.joined(separator: " · ")
    }

    private func load() async {
        isLoading = true
        defer { isLoading = false }
        do {
            guard let fetched = try await controller.serverTrashItems() else {
                items = []
                loadFailed = false
                message =
                    "No server is configured, so there is no trash to show. Deleting on this iPad "
                    + "keeps a local copy that the undo toast restores."
                return
            }
            items = fetched
            loadFailed = false
            message = nil
        } catch {
            // An unreachable server has not emptied the trash: keep the list.
            loadFailed = true
            message = "The trash could not be loaded."
        }
    }

    private func restore(_ item: PaperTrashItem) async {
        busy = item.id
        let restored = await controller.restoreFromServerTrash(item)
        busy = nil
        if restored {
            await load()
        } else {
            message = "That could not be restored. The server may be unreachable."
        }
    }
}
