import PaperCore
import SwiftUI
import UIKit

/// Where a search result sends you: a notebook, and optionally a page and the
/// spot on it.
public struct SearchDestination: Equatable, Sendable {
    public let notebookPath: String
    public let pageID: String?
    public let box: CGRect?

    public init(notebookPath: String, pageID: String? = nil, box: CGRect? = nil) {
        self.notebookPath = notebookPath
        self.pageID = pageID
        self.box = box
    }
}

/// The library's search, in five groups and in this order: folders by name,
/// documents by title, page titles (Outline), then the handwriting in
/// notebooks and in whiteboards. Names match ignoring case and accents; each
/// group lists its newest results first, at most fifty.
///
/// A handwriting result is navigation, not a list entry: it names the
/// document and the page, shows the line as it was read, and carries a crop
/// of the page round the words. Tapping it opens the page at that spot.
struct SearchView: View {
    let controller: PaperController
    let query: String
    let onOpenFolder: (PaperFolder) -> Void
    let onOpen: (SearchDestination) -> Void

    @State private var outline: [OutlineEntry] = []
    @State private var results = LibrarySearchResults()

    var body: some View {
        Group {
            if results.isEmpty {
                Text(query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "" : "No Results")
                    .font(.callout)
                    .foregroundStyle(PaperTokens.textMuted)
                    .frame(maxWidth: .infinity)
                    .padding(.top, 60)
            } else {
                groups
            }
        }
        .task {
            // Page titles are read once per search, off the main actor.
            reload()
            outline = await controller.outlineEntries()
            reload()
        }
        .onChange(of: query) { _, _ in reload() }
        .onChange(of: controller.notebooks) { _, _ in reload() }
    }

    private func reload() {
        results = controller.librarySearch(query, outline: outline)
    }

    private var groups: some View {
        LazyVStack(alignment: .leading, spacing: 0) {
            if !results.folders.isEmpty {
                header("Folders")
                ForEach(results.folders) { folder in
                    SearchItemRow(controller: controller, item: .folder(folder)) {
                        onOpenFolder(folder)
                    }
                }
            }
            if !results.documents.isEmpty {
                header("Documents")
                ForEach(results.documents) { notebook in
                    SearchItemRow(controller: controller, item: .document(notebook)) {
                        onOpen(SearchDestination(notebookPath: notebook.path))
                    }
                }
            }
            if !results.outline.isEmpty {
                header("Outline")
                ForEach(results.outline) { entry in
                    OutlineResultRow(entry: entry) {
                        onOpen(SearchDestination(notebookPath: entry.notebookPath, pageID: entry.pageID))
                    }
                }
            }
            if !results.notebooks.isEmpty {
                header("Notebooks")
                handwriting(results.notebooks)
            }
            if !results.whiteboards.isEmpty {
                header("Whiteboards")
                handwriting(results.whiteboards)
            }
        }
        .padding(.bottom, 24)
    }

    private func handwriting(_ matches: [PageTextMatch]) -> some View {
        ForEach(matches) { match in
            SearchResultRow(
                controller: controller, match: match,
                title: controller.notebooks.first { $0.path == match.notebookPath }?.title
                    ?? String(
                        (match.notebookPath.split(separator: "/").last ?? "").dropLast(".paper".count))
            ) {
                onOpen(
                    SearchDestination(
                        notebookPath: match.notebookPath, pageID: match.pageID, box: match.unionBox))
            }
        }
    }

    private func header(_ title: String) -> some View {
        Text(title)
            .font(.footnote.weight(.semibold))
            .foregroundStyle(PaperTokens.textMuted)
            .padding(.horizontal, 20)
            .padding(.top, 20)
            .padding(.bottom, 6)
            .accessibilityAddTraits(.isHeader)
    }
}

/// A folder or a document that matched by name: its thumbnail, name and date
/// line, as in the list.
struct SearchItemRow: View {
    let controller: PaperController
    let item: PaperCore.LibraryItem
    let onOpen: () -> Void

    var body: some View {
        Button(action: onOpen) {
            HStack(spacing: 12) {
                LibraryThumbnail(controller: controller, item: item, box: CGSize(width: 52, height: 40))
                VStack(alignment: .leading, spacing: 2) {
                    Text(item.name.isEmpty ? "Untitled" : item.name)
                        .font(.body)
                        .foregroundStyle(PaperTokens.text)
                        .lineLimit(1)
                    Text(LibraryArrangement.dateLine(item.modified))
                        .font(.caption)
                        .foregroundStyle(PaperTokens.textMuted)
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 6)
            .frame(minHeight: 56)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(LibraryItemText.accessibilityLabel(item))
    }
}

/// A page whose title matched.
struct OutlineResultRow: View {
    let entry: OutlineEntry
    let onOpen: () -> Void

    var body: some View {
        Button(action: onOpen) {
            HStack(spacing: 12) {
                Image(systemName: "list.bullet.indent")
                    .font(.system(size: 17))
                    .symbolRenderingMode(.monochrome)
                    .foregroundStyle(PaperTokens.textMuted)
                    .frame(width: 52)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 2) {
                    Text(entry.pageTitle)
                        .font(.body)
                        .foregroundStyle(PaperTokens.text)
                        .lineLimit(1)
                    Text("\(entry.notebookTitle) · \(noun) \(entry.pageNumber)")
                        .font(.caption)
                        .monospacedDigit()
                        .foregroundStyle(PaperTokens.textMuted)
                        .lineLimit(1)
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 6)
            .frame(minHeight: 56)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(entry.pageTitle), \(entry.notebookTitle), \(noun) \(entry.pageNumber)")
    }

    private var noun: String { entry.kind == .whiteboard ? "Board" : "Page" }
}

/// One handwriting hit: the document and page, the line, and the handwriting
/// itself.
struct SearchResultRow: View {
    let controller: PaperController
    let match: PageTextMatch
    let title: String
    let onOpen: () -> Void

    @State private var crop: UIImage?

    var body: some View {
        Button(action: onOpen) {
            HStack(alignment: .center, spacing: 14) {
                cropView
                VStack(alignment: .leading, spacing: 3) {
                    Text("\(title) · Page \(match.pageNumber)")
                        .font(.caption)
                        .monospacedDigit()
                        .foregroundStyle(PaperTokens.textMuted)
                        .lineLimit(1)
                    Text(match.line)
                        .font(.callout)
                        .foregroundStyle(PaperTokens.text)
                        .lineLimit(2)
                        .multilineTextAlignment(.leading)
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 8)
            .frame(minHeight: PaperTokens.minimumTarget)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .task(id: match.id) { crop = controller.crop(for: match) }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(title), page \(match.pageNumber), \(match.line)")
        .accessibilityHint("Opens the page at these words")
    }

    @ViewBuilder
    private var cropView: some View {
        if let crop {
            Image(uiImage: crop)
                .resizable()
                .aspectRatio(contentMode: .fit)
                .frame(width: 132, height: 44)
                .clipShape(RoundedRectangle(cornerRadius: 3))
                .overlay(
                    RoundedRectangle(cornerRadius: 3)
                        .strokeBorder(PaperTokens.border, lineWidth: 1))
                .accessibilityHidden(true)
        } else {
            RoundedRectangle(cornerRadius: 3)
                .fill(PaperTokens.paper)
                .frame(width: 132, height: 44)
                .overlay(
                    RoundedRectangle(cornerRadius: 3)
                        .strokeBorder(PaperTokens.border, lineWidth: 1))
                .accessibilityHidden(true)
        }
    }
}
