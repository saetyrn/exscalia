import PaperCore
import SwiftUI

/// A compact table of contents for notebooks large enough that thumbnails
/// alone become a hunt. Titles are optional; page numbers remain the stable
/// fallback and bookmarks get their own section.
struct PageOutlineSheet: View {
    let pages: [Page]
    let currentPageID: String?
    let onOpen: (String) -> Void

    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                let bookmarks = pages.filter(\.bookmarked)
                if !bookmarks.isEmpty {
                    Section("Bookmarks") {
                        ForEach(bookmarks) { page in row(page) }
                    }
                }
                Section("All pages") {
                    ForEach(pages) { page in row(page) }
                }
            }
            .navigationTitle("Notebook outline")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
        }
    }

    private func row(_ page: Page) -> some View {
        let number = (pages.firstIndex(where: { $0.id == page.id }) ?? 0) + 1
        return Button {
            onOpen(page.id)
            dismiss()
        } label: {
            HStack(spacing: 12) {
                Text("\(number)")
                    .font(.callout.monospacedDigit())
                    .foregroundStyle(PaperTokens.textMuted)
                    .frame(width: 36, alignment: .trailing)
                VStack(alignment: .leading, spacing: 2) {
                    Text(page.title?.nilIfBlank ?? "Page \(number)")
                        .foregroundStyle(PaperTokens.text)
                        .lineLimit(1)
                    if let title = page.title?.nilIfBlank {
                        Text("Page \(number)")
                            .font(.caption)
                            .foregroundStyle(PaperTokens.textMuted)
                    }
                }
                Spacer(minLength: 0)
                if page.bookmarked {
                    Image(systemName: "bookmark.fill")
                        .symbolRenderingMode(.monochrome)
                        .foregroundStyle(PaperTokens.accent)
                        .accessibilityHidden(true)
                }
                if page.id == currentPageID {
                    Image(systemName: "checkmark")
                        .symbolRenderingMode(.monochrome)
                        .foregroundStyle(PaperTokens.accent)
                        .accessibilityHidden(true)
                }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(
            "\(page.title?.nilIfBlank ?? "Page \(number)")\(page.bookmarked ? ", bookmarked" : "")")
    }
}

struct NotebookInspectorDraft: Equatable {
    var folder: String
    var tags: String
    var favorite: Bool
    var coverPageID: String
    var template: Template
    var paperSize: PaperSize

    init(notebook: Notebook) {
        folder = notebook.folder
        tags = notebook.tags.joined(separator: ", ")
        favorite = notebook.favorite
        coverPageID = notebook.cover ?? ""
        template = notebook.template
        paperSize = notebook.paperSize
    }

    var parsedTags: [String] {
        var seen = Set<String>()
        return tags.split(separator: ",")
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty && seen.insert($0.lowercased()).inserted }
    }
}

/// Notebook-level properties in one deliberate place; none of these controls
/// belongs in the writing toolbar.
struct NotebookInspectorSheet: View {
    let notebook: Notebook
    let pages: [Page]
    let knownFolders: [String]
    let onSave: (NotebookInspectorDraft) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var draft: NotebookInspectorDraft

    init(
        notebook: Notebook, pages: [Page], knownFolders: [String],
        onSave: @escaping (NotebookInspectorDraft) -> Void
    ) {
        self.notebook = notebook
        self.pages = pages
        self.knownFolders = knownFolders
        self.onSave = onSave
        _draft = State(initialValue: NotebookInspectorDraft(notebook: notebook))
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Location") {
                    TextField("Folder", text: $draft.folder)
                        .textInputAutocapitalization(.words)
                    if !knownFolders.isEmpty {
                        Picker("Existing folder", selection: $draft.folder) {
                            Text("Top level").tag("")
                            ForEach(knownFolders, id: \.self) { Text($0).tag($0) }
                        }
                    }
                }
                Section("Organize") {
                    Toggle("Favorite", isOn: $draft.favorite)
                    TextField("Tags, separated by commas", text: $draft.tags)
                        .textInputAutocapitalization(.words)
                }
                Section("Cover") {
                    Picker("Cover page", selection: $draft.coverPageID) {
                        Text("Automatic").tag("")
                        ForEach(Array(pages.enumerated()), id: \.element.id) { index, page in
                            Text(page.title?.nilIfBlank ?? "Page \(index + 1)").tag(page.id)
                        }
                    }
                }
                Section("New page defaults") {
                    Picker("Template", selection: $draft.template) {
                        ForEach(Template.allCases, id: \.self) { value in
                            Text(value.displayName).tag(value)
                        }
                    }
                    Picker("Paper size", selection: $draft.paperSize) {
                        ForEach(PaperSize.allCases, id: \.self) { value in
                            Text(value.displayName).tag(value)
                        }
                    }
                }
            }
            .navigationTitle("Notebook inspector")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        onSave(draft)
                        dismiss()
                    }
                }
            }
        }
    }
}

private extension String {
    var nilIfBlank: String? {
        let value = trimmingCharacters(in: .whitespacesAndNewlines)
        return value.isEmpty ? nil : value
    }
}

/// Raw recognition is evidence and remains read-only. The editable value is a
/// separate user-authored layer, so rerunning OCR or comparing candidates can
/// never erase what the recognizer originally returned.
struct TranscriptCorrectionSheet: View {
    let text: PageText?
    let onSave: (String?) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var corrected: String

    init(text: PageText?, onSave: @escaping (String?) -> Void) {
        self.text = text
        self.onSave = onSave
        _corrected = State(initialValue: text?.preferredTranscript ?? "")
    }

    var body: some View {
        NavigationStack {
            Group {
                if let text {
                    Form {
                        Section("Corrected text") {
                            TextEditor(text: $corrected)
                                .frame(minHeight: 180)
                                .textInputAutocapitalization(.sentences)
                                .accessibilityLabel("Corrected recognized text")
                        }
                        Section("Original recognition") {
                            Text(text.rawTranscript.isEmpty ? "No text recognized" : text.rawTranscript)
                                .foregroundStyle(PaperTokens.textMuted)
                                .textSelection(.enabled)
                        }
                    }
                } else {
                    ContentUnavailableView(
                        "No recognized text",
                        systemImage: "text.viewfinder",
                        description: Text("Write on the page and wait for on-device recognition."))
                }
            }
            .navigationTitle("Correct recognition")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                if text != nil {
                    ToolbarItem(placement: .topBarLeading) {
                        Button("Use original") {
                            onSave(nil)
                            dismiss()
                        }
                    }
                    ToolbarItem(placement: .confirmationAction) {
                        Button("Save") {
                            onSave(corrected)
                            dismiss()
                        }
                    }
                }
            }
        }
    }
}
