import PaperCore
import SwiftUI
import UIKit

/// The blue bar under the tabs, in three groups: the document's own controls
/// at the left (Sidebar, Search, OMNIA, Read Only), the toolbar in the
/// middle, and Add, Share and More at the right.
///
/// It never touches PencilKit: it changes `ToolState` and the window's tab
/// state, and acts on the canvas through `session.commands`. In Read Only the
/// toolbar goes; when the window is too narrow for one row, the toolbar takes
/// a row of its own under the other two groups.
struct EditorNavigationBar: View {
    let tabs: DocumentTabsModel
    @Bindable var session: NotebookSession
    let actions: EditorMenuActions
    let onSearch: () -> Void

    @State private var omniaAnchor = PopoverAnchor()

    static let height: CGFloat = 52

    var body: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 8) {
                leading.frame(maxWidth: .infinity, alignment: .leading)
                middle
                trailing.frame(maxWidth: .infinity, alignment: .trailing)
            }
            .frame(height: Self.height)
            VStack(spacing: 0) {
                HStack(spacing: 8) {
                    leading
                    Spacer(minLength: 8)
                    trailing
                }
                .frame(height: Self.height)
                if !isReadOnly {
                    middle.frame(height: Self.height)
                }
            }
        }
        .padding(.horizontal, 6)
        .frame(maxWidth: .infinity)
        .background(PaperTokens.navigationBar)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("NavigationBar")
    }

    private var isReadOnly: Bool { session.chrome.isReadOnly }

    private var leading: some View {
        HStack(spacing: 2) {
            Button {
                tabs.isSidebarVisible.toggle()
            } label: {
                NavigationBarGlyph(symbol: "sidebar.left")
            }
            .buttonStyle(.plain)
            .help("Sidebar")
            .accessibilityLabel("Sidebar")
            .accessibilityValue(tabs.isSidebarVisible ? "shown" : "hidden")

            Button(action: onSearch) {
                NavigationBarGlyph(symbol: "magnifyingglass")
            }
            .buttonStyle(.plain)
            .help("Search")
            .accessibilityLabel("Search")

            Button {
                session.commands?.askOMNIA(from: omniaAnchor.view)
            } label: {
                NavigationBarGlyph(symbol: "sparkles")
                    .background(PopoverAnchorView(anchor: omniaAnchor))
            }
            .buttonStyle(.plain)
            .disabled(!session.hasCommands)
            .help("OMNIA")
            .accessibilityLabel("OMNIA")

            Button {
                tabs.setReadOnly(!isReadOnly, for: session.notebook.id)
            } label: {
                NavigationBarGlyph(symbol: "pencil.slash", isOn: isReadOnly)
            }
            .buttonStyle(.plain)
            .help("Read Only")
            .accessibilityLabel("Read Only")
            .accessibilityValue(isReadOnly ? "on" : "off")
            .accessibilityAddTraits(isReadOnly ? [.isSelected] : [])
        }
    }

    @ViewBuilder
    private var middle: some View {
        if !isReadOnly {
            EditorToolbar(tools: session.tools)
                .fixedSize()
        }
    }

    private var trailing: some View {
        HStack(spacing: 2) {
            EditorAddMenu(session: session, actions: actions, isReadOnly: isReadOnly)
            EditorShareMenu(session: session, actions: actions)
            EditorMoreMenu(session: session, actions: actions, isReadOnly: isReadOnly)
        }
    }
}

/// Search inside the open document: its handwriting and its page titles.
/// A sheet rather than a field over the canvas, which would move the page
/// under the pen.
struct DocumentSearchSheet: View {
    let session: NotebookSession
    let onOpen: (_ pageID: String, _ box: CGRect?) -> Void

    @State private var query = ""
    @Environment(\.dismiss) private var dismiss
    @FocusState private var focused: Bool

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                HStack(spacing: 8) {
                    Image(systemName: "magnifyingglass")
                        .symbolRenderingMode(.monochrome)
                        .foregroundStyle(PaperTokens.textMuted)
                        .accessibilityHidden(true)
                    TextField("Search", text: $query)
                        .textFieldStyle(.plain)
                        .focused($focused)
                        .submitLabel(.search)
                        .accessibilityLabel("Search in document")
                }
                .padding(.horizontal, 20)
                .frame(height: PaperTokens.minimumTarget)
                Divider().overlay(PaperTokens.border)
                if trimmed.isEmpty {
                    Spacer()
                } else if titled.isEmpty && matches.isEmpty {
                    Text("No pages match")
                        .font(.callout)
                        .foregroundStyle(PaperTokens.textMuted)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else {
                    List {
                        if !titled.isEmpty {
                            Section("Outline") {
                                ForEach(titled, id: \.page.id) { item in
                                    row(
                                        label: item.page.title ?? "", number: item.number,
                                        pageID: item.page.id, box: nil)
                                }
                            }
                        }
                        if !matches.isEmpty {
                            Section("Handwriting") {
                                ForEach(matches) { match in
                                    row(
                                        label: match.line, number: match.pageNumber,
                                        pageID: match.pageID, box: match.unionBox)
                                }
                            }
                        }
                    }
                    .listStyle(.plain)
                }
            }
            .background(PaperTokens.background)
            .navigationTitle(session.notebook.title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Done") { dismiss() }
                }
            }
            .onAppear { focused = true }
        }
    }

    private var trimmed: String { query.trimmingCharacters(in: .whitespacesAndNewlines) }

    private var noun: String { session.notebook.kind == .whiteboard ? "Board" : "Page" }

    /// Pages whose title holds the query, ignoring case and accents.
    private var titled: [(page: Page, number: Int)] {
        guard !trimmed.isEmpty else { return [] }
        return session.pages.enumerated().compactMap { index, page in
            guard let title = page.title,
                title.range(of: trimmed, options: [.caseInsensitive, .diacriticInsensitive]) != nil
            else { return nil }
            return (page, index + 1)
        }
    }

    /// Handwriting hits in this document only.
    private var matches: [PageTextMatch] {
        guard !trimmed.isEmpty else { return [] }
        return session.controller.search(trimmed)
            .filter { $0.notebookPath == session.notebook.path }
            .flatMap(\.pages)
    }

    private func row(label: String, number: Int, pageID: String, box: CGRect?) -> some View {
        Button {
            onOpen(pageID, box)
            dismiss()
        } label: {
            VStack(alignment: .leading, spacing: 3) {
                Text("\(noun) \(number)")
                    .font(.caption)
                    .monospacedDigit()
                    .foregroundStyle(PaperTokens.textMuted)
                Text(label)
                    .font(.callout)
                    .foregroundStyle(PaperTokens.text)
                    .lineLimit(2)
            }
            .frame(maxWidth: .infinity, minHeight: PaperTokens.minimumTarget, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(noun) \(number), \(label)")
    }
}
