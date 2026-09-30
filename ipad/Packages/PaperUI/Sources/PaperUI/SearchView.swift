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

/// Handwriting search.
///
/// Search here is **navigation**, not a list of documents: every row names the
/// notebook and the page, shows the line as it was written, and carries a crop
/// of the page around the words themselves. Tapping one opens the page at that
/// spot rather than at the top.
public struct SearchView: View {
    let controller: PaperController
    let query: String
    let onOpen: (SearchDestination) -> Void

    @State private var groups: [NotebookMatches] = []

    public init(
        controller: PaperController, query: String,
        onOpen: @escaping (SearchDestination) -> Void
    ) {
        self.controller = controller
        self.query = query
        self.onOpen = onOpen
    }

    public var body: some View {
        Group {
            if groups.isEmpty {
                emptyState
            } else {
                results
            }
        }
        .onAppear { reload() }
        .onChange(of: query) { _, _ in reload() }
    }

    private func reload() {
        groups = controller.search(query)
    }

    private var results: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 0) {
                ForEach(groups) { group in
                    Text(group.notebookTitle)
                        .font(.footnote.weight(.medium))
                        .foregroundStyle(PaperTokens.textMuted)
                        .padding(.horizontal, 20)
                        .padding(.top, 18)
                        .padding(.bottom, 6)
                        .accessibilityAddTraits(.isHeader)

                    if group.pages.isEmpty {
                        NotebookOnlyRow(title: group.notebookTitle) {
                            onOpen(SearchDestination(notebookPath: group.notebookPath))
                        }
                    } else {
                        ForEach(group.pages) { match in
                            SearchResultRow(controller: controller, match: match) {
                                onOpen(
                                    SearchDestination(
                                        notebookPath: match.notebookPath, pageID: match.pageID,
                                        box: match.unionBox))
                            }
                        }
                    }
                }
            }
            .padding(.bottom, 24)
        }
        .background(PaperTokens.background)
    }

    /// One muted line, per the plan. Nothing to explain, nothing to dismiss.
    private var emptyState: some View {
        Text(controller.store.textIndex.indexedPageCount() == 0
            ? "No pages have been read yet"
            : "No pages match")
            .font(.callout)
            .foregroundStyle(PaperTokens.textMuted)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .center)
            .padding(24)
            .background(PaperTokens.background)
    }
}

/// One hit: the page number, the line, and the handwriting itself.
struct SearchResultRow: View {
    let controller: PaperController
    let match: PageTextMatch
    let onOpen: () -> Void

    @State private var crop: UIImage?

    var body: some View {
        Button(action: onOpen) {
            HStack(alignment: .center, spacing: 14) {
                cropView
                VStack(alignment: .leading, spacing: 3) {
                    Text("Page \(match.pageNumber)")
                        .font(.caption)
                        .monospacedDigit()
                        .foregroundStyle(PaperTokens.textMuted)
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
        .accessibilityLabel("Page \(match.pageNumber), \(match.line)")
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

/// A notebook whose title matched but whose pages did not.
struct NotebookOnlyRow: View {
    let title: String
    let onOpen: () -> Void

    var body: some View {
        Button(action: onOpen) {
            HStack(spacing: 10) {
                Image(systemName: "book.closed")
                    .symbolRenderingMode(.monochrome)
                    .foregroundStyle(PaperTokens.textMuted)
                    .accessibilityHidden(true)
                Text(title)
                    .font(.callout)
                    .foregroundStyle(PaperTokens.text)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 20)
            .frame(minHeight: PaperTokens.minimumTarget)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(title)
        .accessibilityHint("Opens the notebook")
    }
}
