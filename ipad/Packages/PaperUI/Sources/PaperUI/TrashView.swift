import PaperCore
import SwiftUI

/// The server's Paper trash: deleted folders, documents and pages, each with
/// how long it has left and the one button that matters.
///
/// The server keeps what was deleted for thirty days, so "I deleted the wrong
/// notebook" has an answer long after the six-second undo toast has gone.
/// Restoring puts the same bytes back under the same ids: a move, never a
/// re-creation. A folder comes back with everything that was in it.
///
/// The library shows it in place, from its sidebar; a document's More menu
/// shows it as a sheet.
public struct TrashView: View {
    private let controller: PaperController
    private let embedded: Bool

    @Environment(\.dismiss) private var dismiss
    @Environment(\.displayScale) private var displayScale
    @State private var items: [PaperTrashItem] = []
    @State private var isLoading = true
    @State private var message: String?
    @State private var busy: String?

    public init(controller: PaperController) {
        self.init(controller: controller, embedded: false)
    }

    init(controller: PaperController, embedded: Bool) {
        self.controller = controller
        self.embedded = embedded
    }

    public var body: some View {
        if embedded {
            content
        } else {
            NavigationStack {
                ScrollView { content }
                    .background(PaperTokens.libraryBackground)
                    .navigationTitle("Trash")
                    .navigationBarTitleDisplayMode(.inline)
                    .toolbar {
                        ToolbarItem(placement: .topBarTrailing) {
                            Button("Done") { dismiss() }
                                .accessibilityLabel("Done")
                        }
                    }
                    .refreshable { await load() }
            }
        }
    }

    private var content: some View {
        LazyVStack(alignment: .leading, spacing: 0) {
            if let message {
                Text(message)
                    .font(.callout)
                    .foregroundStyle(PaperTokens.textMuted)
                    .padding(.horizontal, 20)
                    .padding(.vertical, 16)
            }
            ForEach(items) { item in
                entry(item)
            }
        }
        .task { await load() }
    }

    private func entry(_ item: PaperTrashItem) -> some View {
        HStack(spacing: 12) {
            Image(systemName: Self.symbol(for: item))
                .font(.system(size: 22, weight: .light))
                .symbolRenderingMode(.monochrome)
                .foregroundStyle(PaperTokens.textMuted)
                .frame(width: 70, height: 54)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                Text(item.displayTitle)
                    .font(.system(size: 17))
                    .foregroundStyle(PaperTokens.text)
                    .lineLimit(1)
                Text(Self.subtitle(for: item))
                    .font(.system(size: 13))
                    .foregroundStyle(PaperTokens.textMuted)
                    .lineLimit(1)
            }
            Spacer(minLength: 8)
            Button("Restore") {
                Task { await restore(item) }
            }
            .buttonStyle(.bordered)
            .tint(PaperTokens.newButton)
            .disabled(busy != nil)
            .accessibilityLabel("Restore \(item.displayTitle)")
        }
        .padding(.leading, 16)
        .padding(.trailing, 20)
        .frame(height: LibraryRow.height)
        .overlay(alignment: .bottom) {
            Rectangle()
                .fill(PaperTokens.libraryHairline)
                .frame(height: 1 / max(displayScale, 1))
                .padding(.horizontal, 16)
                .accessibilityHidden(true)
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(Self.kind(of: item)), \(item.displayTitle), \(Self.subtitle(for: item))")
    }

    static func symbol(for item: PaperTrashItem) -> String {
        switch item.kind {
        case "paper_folder": return "folder"
        case "page": return "doc"
        default: return "book.closed"
        }
    }

    static func kind(of item: PaperTrashItem) -> String {
        switch item.kind {
        case "paper_folder": return "Folder"
        case "page": return "Page"
        default: return "Document"
        }
    }

    /// "Deletes in 12 days", with what went with a folder.
    static func subtitle(for item: PaperTrashItem, now: Date = Date()) -> String {
        var parts: [String] = []
        if let expires = item.expiresAt.flatMap(PaperDate.parse) {
            let days = Int((expires.timeIntervalSince(now) / 86_400).rounded(.up))
            parts.append(
                days <= 0 ? "Deletes today" : days == 1 ? "Deletes in 1 day" : "Deletes in \(days) days")
        }
        if item.kind == "paper_folder", let count = item.notebookCount, count > 0 {
            parts.append(count == 1 ? "1 document" : "\(count) documents")
        }
        return parts.joined(separator: " · ")
    }

    private func load() async {
        isLoading = true
        defer { isLoading = false }
        do {
            guard let fetched = try await controller.serverTrashItems() else {
                items = []
                message = "No server"
                return
            }
            items = fetched.sorted {
                ($0.deletedAt.flatMap(PaperDate.parse) ?? .distantPast)
                    > ($1.deletedAt.flatMap(PaperDate.parse) ?? .distantPast)
            }
            message = nil
        } catch {
            // An unreachable server has not emptied the trash: keep the list.
            message = "The Trash could not be loaded"
        }
    }

    private func restore(_ item: PaperTrashItem) async {
        busy = item.id
        let restored = await controller.restoreFromServerTrash(item)
        busy = nil
        if restored {
            await load()
        } else {
            message = "\(item.displayTitle) could not be restored"
        }
    }
}
