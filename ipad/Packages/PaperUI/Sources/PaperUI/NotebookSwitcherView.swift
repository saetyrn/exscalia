import PaperCore
import SwiftUI

/// Changing notebook without leaving the writing.
///
/// A popover over the page rather than a trip back to the library: the point of
/// the switcher is that the notebook you are in stays where it is until the
/// moment the new one takes its place, so nothing is popped, nothing animates
/// away, and the reading position of both books is kept.
struct NotebookSwitcherView: View {
    let controller: PaperController
    let currentID: String
    let onOpen: (Notebook) -> Void

    @State private var query = ""
    // Deliberately not focused on appear: a keyboard would cover the list the
    // switcher exists to show, and most switches are a tap on a cover.
    @FocusState private var searchFocused: Bool

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass")
                    .symbolRenderingMode(.monochrome)
                    .foregroundStyle(PaperTokens.textMuted)
                    .accessibilityHidden(true)
                TextField("Search notebooks", text: $query)
                    .textFieldStyle(.plain)
                    .focused($searchFocused)
                    .submitLabel(.go)
                    .onSubmit { if let first = results.first { onOpen(first) } }
                    .accessibilityLabel("Search notebooks")
            }
            .padding(.horizontal, 16)
            .frame(height: PaperTokens.minimumTarget)
            Divider().overlay(PaperTokens.border)
            List {
                if query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    if !recents.isEmpty {
                        Section("Recents") {
                            ForEach(recents, id: \.id) { row($0) }
                        }
                    }
                    Section("All notebooks") {
                        ForEach(all, id: \.id) { row($0) }
                    }
                } else {
                    ForEach(results, id: \.id) { row($0) }
                }
            }
            .listStyle(.plain)
        }
        .frame(width: 360, height: 480)
        .background(PaperTokens.background)
    }

    private var all: [Notebook] { controller.notebooks }

    private var recents: [Notebook] {
        controller.recentNotebooks(limit: 6).filter { $0.id != currentID }
    }

    private var results: [Notebook] {
        let needle = query.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !needle.isEmpty else { return all }
        return all.filter {
            $0.title.lowercased().contains(needle) || $0.folder.lowercased().contains(needle)
        }
    }

    private func row(_ notebook: Notebook) -> some View {
        let isCurrent = notebook.id == currentID
        return Button {
            onOpen(notebook)
        } label: {
            HStack(spacing: 12) {
                cover(notebook)
                    .frame(width: 34, height: 46)
                    .clipShape(RoundedRectangle(cornerRadius: 3))
                    .overlay(
                        RoundedRectangle(cornerRadius: 3)
                            .strokeBorder(
                                isCurrent ? PaperTokens.accent : PaperTokens.border,
                                lineWidth: isCurrent ? 2 : 1))
                VStack(alignment: .leading, spacing: 2) {
                    Text(notebook.title)
                        .font(.subheadline)
                        .foregroundStyle(PaperTokens.text)
                        .lineLimit(1)
                    Text(
                        notebook.folder.isEmpty
                            ? SaveState.relative(since: notebook.modified)
                            : "\(notebook.folder) · \(SaveState.relative(since: notebook.modified))"
                    )
                    .font(.caption)
                    .foregroundStyle(PaperTokens.textMuted)
                    .lineLimit(1)
                }
                Spacer(minLength: 0)
                if isCurrent {
                    Image(systemName: "checkmark")
                        .symbolRenderingMode(.monochrome)
                        .foregroundStyle(PaperTokens.accent)
                        .accessibilityHidden(true)
                }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(notebook.title)
        .accessibilityValue(isCurrent ? "open" : "")
        .accessibilityHint("Opens this notebook where you left it")
        .accessibilityAddTraits(isCurrent ? [.isSelected] : [])
    }

    @ViewBuilder
    private func cover(_ notebook: Notebook) -> some View {
        if let image = controller.coverImage(for: notebook) {
            Image(uiImage: image)
                .resizable()
                .aspectRatio(contentMode: .fill)
        } else {
            TemplatePreview(template: notebook.template)
        }
    }
}
