import PaperCore
import SwiftUI
import UIKit

// The three sheets of the library's New menu: Notebook, Whiteboard and Folder.
// They hand back what was chosen and write nothing to the store; the library
// makes the document or the folder. The notebook and whiteboard sheets open
// with the choices of the last "Create" of their kind and save the new ones
// (`NewDocumentOptions`); the folder sheet remembers nothing.
//
// Keyboard: focus starts in the title, Return creates, Escape cancels.

// MARK: - Logic

/// The title or folder name a sheet hands back for what was typed.
enum NewDocumentTitle {
    /// The folder record's limit (1-200 scalars), applied to titles too.
    static let maximumScalars = 200
    static let fallback = "Untitled"

    /// Line breaks, tabs and other controls become spaces, the ends are
    /// trimmed, and the rest is cut to 200 scalars on a character boundary.
    /// Nothing left reads "Untitled", as the store and the server would.
    static func resolved(_ raw: String) -> String {
        var scalars = String.UnicodeScalarView()
        for scalar in raw.unicodeScalars {
            switch scalar.properties.generalCategory {
            case .control, .lineSeparator, .paragraphSeparator: scalars.append(" ")
            default: scalars.append(scalar)
            }
        }
        let trimmed = String(scalars).trimmingCharacters(in: .whitespacesAndNewlines)
        var result = ""
        var count = 0
        for character in trimmed {
            let length = character.unicodeScalars.count
            guard count + length <= maximumScalars else { break }
            result.append(character)
            count += length
        }
        let final = result.trimmingCharacters(in: .whitespacesAndNewlines)
        return final.isEmpty ? fallback : final
    }
}

/// What a New Notebook or New Whiteboard sheet holds while it is open.
struct NewDocumentDraft: Equatable {
    let kind: DocumentKind
    var title = ""
    var options: NewDocumentOptions

    /// Opens with the choices the last "Create" of `kind` saved.
    init(kind: DocumentKind, defaults: UserDefaults) {
        self.kind = kind
        options = NewDocumentOptions.load(kind, defaults: defaults)
    }

    /// The first paper page as it will be made: the preset turned to the
    /// orientation. A whiteboard's is the board's default extent.
    var pageSize: CGSize { options.size.points(in: options.orientation) }

    /// What "Create" hands back. It saves the choices for the next sheet of
    /// this kind; the title is not remembered.
    func commit(defaults: UserDefaults) -> (title: String, options: NewDocumentOptions) {
        let chosen = options.sanitized(for: kind)
        chosen.save(kind, defaults: defaults)
        return (NewDocumentTitle.resolved(title), chosen)
    }
}

/// What the folder sheet holds while it is open.
struct FolderDraft: Equatable {
    var name: String
    var color: FolderColor
    var icon: FolderIcon?
    let isNew: Bool

    /// A new folder (blue, no icon) when `folder` is nil, otherwise that
    /// folder's name, colour and icon.
    init(editing folder: PaperFolder? = nil) {
        name = folder?.name ?? ""
        color = folder?.color ?? .blue
        icon = folder?.icon
        isNew = folder == nil
    }

    /// What "Done" hands back. An icon that would not survive the wire is
    /// dropped rather than sent.
    func commit() -> (name: String, color: FolderColor, icon: FolderIcon?) {
        (NewDocumentTitle.resolved(name), color, icon.flatMap { FolderIcon(wire: $0.wire) })
    }
}

// MARK: - Notebook

/// "+ New" > Notebook: a cover and a paper preview side by side, then the
/// title, the cover switch and its colour, the paper templates, the paper
/// colour, the size and the orientation.
struct NotebookSheet: View {
    private let defaults: UserDefaults
    private let onCreate: (_ title: String, _ options: NewDocumentOptions) -> Void
    private let onCancel: () -> Void
    @State private var draft: NewDocumentDraft
    @State private var finished = false
    @FocusState private var titleFocused: Bool

    init(
        defaults: UserDefaults = .standard,
        onCreate: @escaping (_ title: String, _ options: NewDocumentOptions) -> Void,
        onCancel: @escaping () -> Void
    ) {
        self.defaults = defaults
        self.onCreate = onCreate
        self.onCancel = onCancel
        _draft = State(initialValue: NewDocumentDraft(kind: .notebook, defaults: defaults))
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    HStack(alignment: .top, spacing: 36) {
                        coverPreview
                        paperPreview
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 6)
                }
                .listRowBackground(Color.clear)

                Section {
                    TitleField(label: "Title", text: $draft.title, focused: $titleFocused, onSubmit: create)
                }

                Section {
                    Toggle("Cover", isOn: $draft.options.cover)
                        .accessibilityLabel("Cover")
                    if draft.options.cover {
                        SwatchRow(
                            colors: PaperColor.coverChoices, showsNames: false,
                            selection: $draft.options.coverColor)
                    }
                }

                Section("Paper") {
                    HStack(alignment: .top, spacing: 18) {
                        ForEach(Template.paperTemplates, id: \.self) { template in
                            templateButton(template)
                        }
                        Spacer(minLength: 0)
                    }
                    .padding(.vertical, 4)
                }

                Section("Color") {
                    SwatchRow(
                        colors: PaperColor.paperChoices, showsNames: true,
                        selection: $draft.options.paperColor)
                }

                Section {
                    SegmentRow(label: "Size") {
                        Picker("Size", selection: $draft.options.size) {
                            ForEach(PaperSize.newNotebookChoices, id: \.self) { size in
                                Text(size.displayName).tag(size)
                            }
                        }
                    }
                    SegmentRow(label: "Orientation") {
                        Picker("Orientation", selection: $draft.options.orientation) {
                            ForEach(PageOrientation.allCases, id: \.self) { orientation in
                                Text(orientation.displayName).tag(orientation)
                            }
                        }
                    }
                }
            }
            .navigationTitle("New Notebook")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                SheetActions(confirm: "Create", onConfirm: create, onCancel: cancel)
            }
            .onAppear { titleFocused = true }
        }
        .presentationSizing(.page)
    }

    private var coverPreview: some View {
        VStack(spacing: 8) {
            PreviewBox {
                if draft.options.cover {
                    PagePreview(
                        template: .cover, paperColor: draft.options.coverColor,
                        pageSize: draft.pageSize)
                } else {
                    RoundedRectangle(cornerRadius: 4, style: .continuous)
                        .strokeBorder(
                            Color(uiColor: .separator),
                            style: StrokeStyle(lineWidth: 1, dash: [5, 4]))
                        .aspectRatio(draft.pageSize, contentMode: .fit)
                }
            }
            Text(draft.options.cover ? "Cover" : "No Cover")
                .font(.footnote)
                .foregroundStyle(.secondary)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(
            draft.options.cover ? "Cover, \(draft.options.coverColor.displayName)" : "No Cover")
    }

    private var paperPreview: some View {
        VStack(spacing: 8) {
            PreviewBox {
                PagePreview(
                    template: draft.options.template, paperColor: draft.options.paperColor,
                    pageSize: draft.pageSize)
            }
            Text("Paper")
                .font(.footnote)
                .foregroundStyle(.secondary)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(
            "Paper, \(draft.options.template.displayName), \(draft.options.paperColor.displayName), "
                + "\(draft.options.size.displayName), \(draft.options.orientation.displayName)")
    }

    private func templateButton(_ template: Template) -> some View {
        let selected = draft.options.template == template
        return Button {
            draft.options.template = template
        } label: {
            VStack(spacing: 6) {
                PagePreview(
                    template: template, paperColor: draft.options.paperColor,
                    pageSize: Self.thumbnailRegion(of: draft.pageSize)
                )
                .padding(3)
                .overlay(SelectionRing(selected: selected, cornerRadius: 6))
                .frame(width: 76, height: 76)
                Text(template.displayName)
                    .font(.footnote)
                    .foregroundStyle(selected ? .primary : .secondary)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help(template.displayName)
        .accessibilityLabel(template.displayName)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    /// The corner of the page a template thumbnail shows: the page's
    /// proportions, 200 points on the long side. A whole page at thumbnail
    /// size shrinks the 5 mm squares and dots to a grey wash; a corner drawn
    /// at about a third of real size keeps every pattern readable.
    static func thumbnailRegion(of page: CGSize) -> CGSize {
        let long = max(page.width, page.height)
        guard long > 0 else { return page }
        let scale = min(1, 200 / long)
        return CGSize(width: page.width * scale, height: page.height * scale)
    }

    private func create() {
        guard !finished else { return }
        finished = true
        let result = draft.commit(defaults: defaults)
        onCreate(result.title, result.options)
    }

    private func cancel() {
        guard !finished else { return }
        finished = true
        onCancel()
    }
}

// MARK: - Whiteboard

/// "+ New" > Whiteboard: a live preview of the board at 100 %, then the
/// title, the background pattern and the paper colour.
struct WhiteboardSheet: View {
    private let defaults: UserDefaults
    private let onCreate: (_ title: String, _ options: NewDocumentOptions) -> Void
    private let onCancel: () -> Void
    @State private var draft: NewDocumentDraft
    @State private var finished = false
    @FocusState private var titleFocused: Bool

    init(
        defaults: UserDefaults = .standard,
        onCreate: @escaping (_ title: String, _ options: NewDocumentOptions) -> Void,
        onCancel: @escaping () -> Void
    ) {
        self.defaults = defaults
        self.onCreate = onCreate
        self.onCancel = onCancel
        _draft = State(initialValue: NewDocumentDraft(kind: .whiteboard, defaults: defaults))
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    BoardPreview(template: draft.options.template, paperColor: draft.options.paperColor)
                        .frame(height: 240)
                        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                        .overlay(
                            RoundedRectangle(cornerRadius: 10, style: .continuous)
                                .strokeBorder(Color(uiColor: .separator), lineWidth: 1))
                        .accessibilityElement(children: .ignore)
                        .accessibilityLabel(
                            "Board, \(draft.options.template.displayName(for: .whiteboard)), "
                                + draft.options.paperColor.displayName)
                }
                .listRowBackground(Color.clear)
                .listRowInsets(EdgeInsets(top: 6, leading: 0, bottom: 6, trailing: 0))

                Section {
                    TitleField(label: "Title", text: $draft.title, focused: $titleFocused, onSubmit: create)
                }

                Section("Background") {
                    HStack(alignment: .top, spacing: 18) {
                        ForEach(Template.boardTemplates, id: \.self) { template in
                            backgroundButton(template)
                        }
                        Spacer(minLength: 0)
                    }
                    .padding(.vertical, 4)
                }

                Section("Color") {
                    SwatchRow(
                        colors: PaperColor.paperChoices, showsNames: true,
                        selection: $draft.options.paperColor)
                }
            }
            .navigationTitle("New Whiteboard")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                SheetActions(confirm: "Create", onConfirm: create, onCancel: cancel)
            }
            .onAppear { titleFocused = true }
        }
        .presentationSizing(.page)
    }

    private func backgroundButton(_ template: Template) -> some View {
        let selected = draft.options.template == template
        let name = template.displayName(for: .whiteboard)
        return Button {
            draft.options.template = template
        } label: {
            VStack(spacing: 6) {
                // At 56 %, as in the owner's screenshot: more squares and dots
                // than at 100 %, so a small tile still shows its pattern.
                BoardPreview(template: template, paperColor: draft.options.paperColor, zoom: 0.56)
                    .frame(width: 104, height: 72)
                    .clipShape(RoundedRectangle(cornerRadius: 4, style: .continuous))
                    .overlay(
                        RoundedRectangle(cornerRadius: 4, style: .continuous)
                            .strokeBorder(Color(uiColor: .separator), lineWidth: 1))
                    .padding(3)
                    .overlay(SelectionRing(selected: selected, cornerRadius: 7))
                Text(name)
                    .font(.footnote)
                    .foregroundStyle(selected ? .primary : .secondary)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help(name)
        .accessibilityLabel(name)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    private func create() {
        guard !finished else { return }
        finished = true
        let result = draft.commit(defaults: defaults)
        onCreate(result.title, result.options)
    }

    private func cancel() {
        guard !finished else { return }
        finished = true
        onCancel()
    }
}

// MARK: - Folder

/// "+ New" > Folder, and the same sheet to edit one: the folder as it will
/// look, its name, its colour and its icon.
struct FolderSheet: View {
    private let onDone: (_ name: String, _ color: FolderColor, _ icon: FolderIcon?) -> Void
    private let onCancel: () -> Void
    @State private var draft: FolderDraft
    @State private var finished = false
    @FocusState private var nameFocused: Bool

    /// A new folder when `folder` is nil; otherwise that folder, to edit.
    init(
        editing folder: PaperFolder? = nil,
        onDone: @escaping (_ name: String, _ color: FolderColor, _ icon: FolderIcon?) -> Void,
        onCancel: @escaping () -> Void
    ) {
        self.onDone = onDone
        self.onCancel = onCancel
        _draft = State(initialValue: FolderDraft(editing: folder))
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    FolderThumbnail(color: draft.color, icon: draft.icon, size: 112)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 4)
                }
                .listRowBackground(Color.clear)

                Section {
                    TitleField(label: "Name", text: $draft.name, focused: $nameFocused, onSubmit: done)
                }

                FolderAppearancePicker(color: $draft.color, icon: $draft.icon, onSubmit: done)
            }
            .navigationTitle(draft.isNew ? "New Folder" : "Edit Folder")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                SheetActions(confirm: "Done", onConfirm: done, onCancel: cancel)
            }
            .onAppear { nameFocused = true }
        }
        .presentationSizing(.page)
    }

    private func done() {
        guard !finished else { return }
        finished = true
        let result = draft.commit()
        onDone(result.name, result.color, result.icon)
    }

    private func cancel() {
        guard !finished else { return }
        finished = true
        onCancel()
    }
}

// MARK: - Shared pieces

/// Cancel (Escape) and the confirming button (Return when the title field
/// does not have it).
private struct SheetActions: ToolbarContent {
    let confirm: String
    let onConfirm: () -> Void
    let onCancel: () -> Void

    var body: some ToolbarContent {
        ToolbarItem(placement: .cancellationAction) {
            Button("Cancel", action: onCancel)
                .keyboardShortcut(.cancelAction)
                .accessibilityLabel("Cancel")
        }
        ToolbarItem(placement: .confirmationAction) {
            Button(confirm, action: onConfirm)
                .keyboardShortcut(.defaultAction)
                .accessibilityLabel(confirm)
        }
    }
}

/// A labelled one-line field whose empty state reads "Untitled", which is
/// what an empty title becomes.
private struct TitleField: View {
    let label: String
    @Binding var text: String
    var focused: FocusState<Bool>.Binding
    let onSubmit: () -> Void

    var body: some View {
        // Not `LabeledContent`: it folds its label into the field's, which
        // VoiceOver then reads twice ("Title, Title").
        HStack(spacing: 12) {
            Text(label)
                .accessibilityHidden(true)
            TextField(label, text: $text, prompt: Text(NewDocumentTitle.fallback))
                .focused(focused)
                .submitLabel(.done)
                .onSubmit(onSubmit)
                .textInputAutocapitalization(.words)
                .accessibilityLabel(label)
        }
    }
}

/// A label and a segmented picker at the trailing edge. The picker carries
/// the label for VoiceOver; the visible one is not read a second time.
private struct SegmentRow<Content: View>: View {
    let label: String
    @ViewBuilder let content: Content

    var body: some View {
        HStack {
            Text(label)
                .accessibilityHidden(true)
            Spacer(minLength: 12)
            content
                .pickerStyle(.segmented)
                .fixedSize()
                .accessibilityLabel(label)
        }
    }
}

/// A square box the preview pages sit in, so a portrait and a landscape page
/// take the same room and the row does not jump when the orientation flips.
private struct PreviewBox<Content: View>: View {
    @ViewBuilder let content: Content

    var body: some View {
        content
            .frame(width: 240, height: 240)
    }
}

/// One page drawn by `TemplatePreview` at the page's own proportions, with
/// a hairline edge so white paper shows on a light sheet and dark paper on a
/// dark one.
private struct PagePreview: View {
    let template: Template
    let paperColor: PaperColor
    let pageSize: CGSize

    var body: some View {
        TemplatePreview(template: template, paperColor: paperColor, pageSize: pageSize)
            .aspectRatio(pageSize, contentMode: .fit)
            .clipShape(RoundedRectangle(cornerRadius: 4, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 4, style: .continuous)
                    .strokeBorder(Color(uiColor: .separator), lineWidth: 1))
    }
}

/// The ring round a chosen template, background or swatch: monochrome, the
/// label colour of the appearance.
private struct SelectionRing: View {
    let selected: Bool
    let cornerRadius: CGFloat

    var body: some View {
        RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
            .strokeBorder(selected ? Color.primary : .clear, lineWidth: 2)
    }
}

/// Paper or cover colours as round swatches, optionally named underneath.
private struct SwatchRow: View {
    let colors: [PaperColor]
    let showsNames: Bool
    @Binding var selection: PaperColor

    var body: some View {
        HStack(alignment: .top, spacing: showsNames ? 22 : 10) {
            ForEach(colors, id: \.self) { color in
                let selected = selection == color
                Button {
                    selection = color
                } label: {
                    VStack(spacing: 6) {
                        Circle()
                            .fill(swatch(color))
                            .overlay(Circle().strokeBorder(Color(uiColor: .separator), lineWidth: 1))
                            .frame(width: 30, height: 30)
                            .padding(4)
                            .overlay(Circle().strokeBorder(selected ? Color.primary : .clear, lineWidth: 2))
                        if showsNames {
                            Text(color.displayName)
                                .font(.footnote)
                                .foregroundStyle(selected ? .primary : .secondary)
                        }
                    }
                    .frame(minWidth: 44, minHeight: 44)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help(color.displayName)
                .accessibilityLabel(color.displayName)
                .accessibilityAddTraits(selected ? .isSelected : [])
            }
            Spacer(minLength: 0)
        }
        .padding(.vertical, 2)
    }

    private func swatch(_ color: PaperColor) -> Color {
        let rgb = color.rgb
        return Color(red: rgb.red, green: rgb.green, blue: rgb.blue)
    }
}

/// A piece of a board round its centre, drawn by the board's own background
/// code at `zoom` (100 %, where a new board opens), so the preview is the
/// board itself.
struct BoardPreview: View {
    let template: Template
    let paperColor: PaperColor
    var zoom: CGFloat = 1
    @Environment(\.displayScale) private var displayScale

    var body: some View {
        Canvas { context, size in
            let extent = BoardGeometry.defaultExtent
            let visible = BoardGeometry.visibleRect(
                center: BoardGeometry.center(of: extent), zoom: zoom, viewport: size)
            let scale = displayScale
            let zoom = zoom
            let template = template
            let paperColor = paperColor
            context.withCGContext { cg in
                cg.scaleBy(x: zoom, y: zoom)
                cg.translateBy(x: -visible.minX, y: -visible.minY)
                BoardGeometry.drawBackground(
                    template: template, paperColor: paperColor, extent: extent,
                    visibleRect: visible, zoom: zoom, pixelsPerPoint: scale * zoom,
                    snapToPixels: true, in: cg)
            }
        }
    }
}

// MARK: - Debug

extension View {
    /// `-debugNewSheet notebook|whiteboard|folder` (DEBUG only) opens that
    /// sheet at launch over whatever this view shows, and prints what it hands
    /// back and shows it in a line at the bottom for UI tests. `-debugNewSheet folder <color> <icon>`, for example
    /// `folder red emoji:📒`, opens the folder sheet editing a folder
    /// "Accounting" with that colour and icon. `-debugResignFocus` drops the
    /// keyboard two seconds in, for a screenshot. A Release build ignores both.
    func newSheetDebugHost() -> some View {
        #if DEBUG
            modifier(NewSheetDebugHost())
        #else
            self
        #endif
    }
}

#if DEBUG
    private enum DebugNewSheet: String, Identifiable {
        case notebook, whiteboard, folder
        var id: String { rawValue }
    }

    private struct NewSheetDebugHost: ViewModifier {
        @State private var sheet: DebugNewSheet?
        @State private var folder: PaperFolder?
        @State private var result: String?

        init() {
            let arguments = CommandLine.arguments
            guard let index = arguments.firstIndex(of: "-debugNewSheet"),
                arguments.indices.contains(index + 1),
                let kind = DebugNewSheet(rawValue: arguments[index + 1])
            else { return }
            _sheet = State(initialValue: kind)
            if kind == .folder, arguments.indices.contains(index + 2),
                let color = FolderColor(rawValue: arguments[index + 2])
            {
                let icon =
                    arguments.indices.contains(index + 3)
                    ? FolderIcon(wire: arguments[index + 3]) : nil
                _folder = State(
                    initialValue: PaperFolder(name: "Accounting", color: color, icon: icon))
            }
        }

        func body(content: Content) -> some View {
            content
                .sheet(item: $sheet) { kind in
                    switch kind {
                    case .notebook:
                        NotebookSheet { title, options in
                            finish("notebook created: " + Self.describe(title, options))
                        } onCancel: {
                            finish("notebook cancelled")
                        }
                    case .whiteboard:
                        WhiteboardSheet { title, options in
                            finish("whiteboard created: " + Self.describe(title, options))
                        } onCancel: {
                            finish("whiteboard cancelled")
                        }
                    case .folder:
                        FolderSheet(editing: folder) { name, color, icon in
                            finish(
                                "folder done: name=\(name) color=\(color.rawValue) "
                                    + "icon=\(icon?.wire ?? "none")")
                        } onCancel: {
                            finish("folder cancelled")
                        }
                    }
                }
                .task {
                    // Headless, the simulator has no hardware keyboard and the
                    // software one covers half the sheet; this lets a
                    // screenshot show all of it.
                    guard CommandLine.arguments.contains("-debugResignFocus") else { return }
                    try? await Task.sleep(for: .seconds(2))
                    UIApplication.shared.sendAction(
                        #selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
                }
                .overlay(alignment: .bottom) {
                    if let result {
                        Text(result)
                            .font(.caption.monospaced())
                            .padding(8)
                            .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 6))
                            .padding(.bottom, 12)
                            .accessibilityIdentifier("debugNewSheet.result")
                    }
                }
        }

        private func finish(_ line: String) {
            print("debugNewSheet " + line)
            result = line
            sheet = nil
        }

        private static func describe(_ title: String, _ options: NewDocumentOptions) -> String {
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys]
            let json = (try? encoder.encode(options)).flatMap { String(data: $0, encoding: .utf8) }
            return "title=\(title) options=\(json ?? "?")"
        }
    }
#endif
