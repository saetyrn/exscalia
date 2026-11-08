import PaperCore
import SwiftUI

/// Info for a folder or a document: its name, kind and location, when it was
/// made and last changed, and then for a document its pages or boards, size
/// and orientation, and its tags; for a folder, what it holds. Tags live
/// here now, not in the sidebar.
struct ItemInfoSheet: View {
    let controller: PaperController
    let item: PaperCore.LibraryItem
    let onDone: () -> Void

    @State private var newTag = ""
    @FocusState private var tagFocused: Bool

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    value("Name", current.name.isEmpty ? "Untitled" : current.name)
                    value("Kind", LibraryItemText.kind(current))
                    value("Location", location)
                }
                Section {
                    value("Created", Self.date(created))
                    value("Modified", Self.date(current.modified))
                }
                switch current {
                case .document(let notebook):
                    Section {
                        if notebook.kind == .whiteboard {
                            value("Boards", "\(notebook.pages.count)")
                        } else {
                            value("Pages", "\(notebook.pages.count)")
                            value("Size", notebook.paperSize.displayName)
                            if notebook.paperSize.hasOrientation {
                                value("Orientation", notebook.orientation.displayName)
                            }
                        }
                    }
                    Section("Tags") {
                        tags(notebook)
                    }
                case .folder(let folder):
                    let counts = controller.folderCounts(folder.path)
                    Section {
                        value("Folders", "\(counts.folders)")
                        value("Notebooks", "\(counts.notebooks)")
                        value("Whiteboards", "\(counts.whiteboards)")
                    }
                }
            }
            .navigationTitle("Info")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done", action: onDone)
                        .keyboardShortcut(.defaultAction)
                        .accessibilityLabel("Done")
                }
            }
        }
        .presentationSizing(.form)
    }

    // MARK: The item now

    /// The item as the library has it now, so a tag added here shows at once.
    private var current: PaperCore.LibraryItem {
        switch item {
        case .folder(let folder):
            return controller.libraryFolder(id: folder.id).map(PaperCore.LibraryItem.folder) ?? item
        case .document(let notebook):
            return controller.notebooks.first { $0.id == notebook.id }
                .map(PaperCore.LibraryItem.document) ?? item
        }
    }

    private var created: Date {
        switch current {
        case .folder(let folder): return folder.created
        case .document(let notebook): return notebook.created
        }
    }

    /// "Documents › Work › Accounting".
    private var location: String {
        let parent: String
        switch current {
        case .folder(let folder): parent = folder.parentPath
        case .document(let notebook): parent = notebook.folderPath
        }
        let names = PaperController.folderChain(to: parent, in: controller.libraryFolders).map(\.name)
        return (["Documents"] + names).joined(separator: " › ")
    }

    private static func date(_ date: Date) -> String {
        LibraryArrangement.dateLine(date)
    }

    private func value(_ label: String, _ text: String) -> some View {
        LabeledContent(label) {
            Text(text)
                .foregroundStyle(PaperTokens.textMuted)
                .multilineTextAlignment(.trailing)
        }
        .accessibilityElement(children: .combine)
    }

    // MARK: Tags

    private func tags(_ notebook: Notebook) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            if !notebook.tags.isEmpty {
                TagFlowLayout(spacing: 8) {
                    ForEach(notebook.tags, id: \.self) { tag in
                        HStack(spacing: 4) {
                            Text(tag)
                                .font(.subheadline)
                                .lineLimit(1)
                            Button {
                                controller.setTags(notebook, notebook.tags.filter { $0 != tag })
                            } label: {
                                Image(systemName: "xmark")
                                    .font(.system(size: 10, weight: .bold))
                                    .symbolRenderingMode(.monochrome)
                                    .frame(width: 24, height: 24)
                                    .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            .help("Remove")
                            .accessibilityLabel("Remove \(tag)")
                        }
                        .foregroundStyle(PaperTokens.text)
                        .padding(.leading, 10)
                        .padding(.trailing, 2)
                        .background(Capsule().fill(PaperTokens.libraryChip))
                    }
                }
            }
            TextField("Add Tag", text: $newTag)
                .focused($tagFocused)
                .submitLabel(.done)
                .textInputAutocapitalization(.never)
                .onSubmit { add(to: notebook) }
                .accessibilityLabel("Add Tag")
        }
        .padding(.vertical, 4)
    }

    private func add(to notebook: Notebook) {
        let tags = Self.adding(newTag, to: notebook.tags)
        newTag = ""
        guard tags != notebook.tags else { return }
        controller.setTags(notebook, tags)
        tagFocused = true
    }

    /// `text` as tags: split on commas (the wire joins tags with them),
    /// trimmed, and only those not already there, ignoring case.
    static func adding(_ text: String, to tags: [String]) -> [String] {
        var result = tags
        var seen = Set(tags.map { $0.lowercased() })
        for part in text.split(separator: ",") {
            let tag = part.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !tag.isEmpty, seen.insert(tag.lowercased()).inserted else { continue }
            result.append(tag)
        }
        return result
    }
}

/// Tags laid out in lines, wrapping at the edge.
struct TagFlowLayout: Layout {
    var spacing: CGFloat = 8

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? .infinity
        var x: CGFloat = 0
        var y: CGFloat = 0
        var line: CGFloat = 0
        var widest: CGFloat = 0
        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x > 0, x + size.width > width {
                y += line + spacing
                x = 0
                line = 0
            }
            x += size.width + spacing
            line = max(line, size.height)
            widest = max(widest, x - spacing)
        }
        return CGSize(width: proposal.width ?? widest, height: y + line)
    }

    func placeSubviews(
        in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()
    ) {
        var x = bounds.minX
        var y = bounds.minY
        var line: CGFloat = 0
        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x > bounds.minX, x + size.width > bounds.maxX {
                y += line + spacing
                x = bounds.minX
                line = 0
            }
            subview.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
            x += size.width + spacing
            line = max(line, size.height)
        }
    }
}
