import PaperCore
import SwiftUI
import UIKit

/// What the sidebar is pointing at.
enum LibraryScope: Hashable {
    case recents
    case favorites
    case all
    case folder(String)
    case tag(String)

    var title: String {
        switch self {
        case .recents: return "Recents"
        case .favorites: return "Favorites"
        case .all: return "All notebooks"
        case .folder(let name): return name
        case .tag(let name): return name
        }
    }
}

/// Where the web app asked Paper to open. Both fields are optional: "open
/// Paper" on its own is a legitimate request.
public struct PaperDeepLink: Equatable, Sendable {
    public var notebook: String?
    public var notebookID: String?
    public var page: String?
    public var box: CGRect?

    public init(
        notebook: String? = nil,
        notebookID: String? = nil,
        page: String? = nil,
        box: CGRect? = nil
    ) {
        self.notebook = notebook
        self.notebookID = notebookID
        self.page = page
        self.box = box
    }

    public var isEmpty: Bool { notebook == nil && notebookID == nil && page == nil }
}

/// The library: folders and tags on the left, covers on the right, one way in
/// and one way out.
public struct LibraryView: View {
    @State private var controller: PaperController
    // Optional because that is the shape a single-selection `List` takes on
    // iOS; the library always has a scope in practice.
    @State private var scope: LibraryScope? = .recents
    @State private var columnVisibility = NavigationSplitViewVisibility.automatic
    @State private var search = ""
    @State private var isCreating = false
    @State private var renaming: Notebook?
    @State private var renameText = ""
    @State private var moving: Notebook?
    @State private var moveText = ""
    @State private var pendingUndo: UndoToastModel?
    @State private var openNotebook: Notebook?
    // M3: the server's thirty-day trash, reachable from the sidebar.
    @State private var isShowingTrash = false
    // M5: where a search result or a web deep link wants the notebook opened.
    @State private var pendingReveal: SearchDestination?
    @FocusState private var searchFocused: Bool

    private let onBack: () -> Void
    private let deepLink: PaperDeepLink
    private let onOpenOnWeb: ((String, String?) -> Void)?
    private let onNavigateOnWeb: ((String) -> Void)?
    private let scanDocument: (@MainActor (UIViewController) async throws -> Data)?

    public init(
        controller: PaperController,
        deepLink: PaperDeepLink = PaperDeepLink(),
        onOpenOnWeb: ((String, String?) -> Void)? = nil,
        onNavigateOnWeb: ((String) -> Void)? = nil,
        scanDocument: (@MainActor (UIViewController) async throws -> Data)? = nil,
        onBack: @escaping () -> Void
    ) {
        _controller = State(initialValue: controller)
        self.deepLink = deepLink
        self.onOpenOnWeb = onOpenOnWeb
        self.onNavigateOnWeb = onNavigateOnWeb
        self.scanDocument = scanDocument
        self.onBack = onBack
    }

    public var body: some View {
        Group {
            if let notebook = openNotebook {
                NotebookScreen(
                    controller: controller, notebook: notebook,
                    reveal: pendingReveal?.notebookPath == notebook.path ? pendingReveal : nil,
                    onOpenOnWeb: onOpenOnWeb,
                    onNavigateOnWeb: onNavigateOnWeb,
                    scanDocument: scanDocument
                ) {
                    openNotebook = nil
                    pendingReveal = nil
                    controller.refresh()
                }
                .id(notebook.id)
            } else {
                library
            }
        }
        .task {
            // First launch lands on a page, not on an empty grid. The index is
            // repaired off the main actor, once per controller rather than on
            // every switch into Paper.
            if let created = await controller.bootstrapIfNeeded() {
                openNotebook = created
                return
            }
            #if DEBUG
                // The simulator has no Pencil and `simctl` cannot synthesise a
                // drag, so the smoke script needs a way to land on a page.
                // A notebook made the way the New notebook sheet makes one,
                // so a UI test starts from the real default rather than from
                // whatever an earlier run left in the container.
                if let index = CommandLine.arguments.firstIndex(of: "-debugNewNotebook") {
                    let named = CommandLine.arguments.indices.contains(index + 1)
                        ? PaperSize(rawValue: CommandLine.arguments[index + 1]) : nil
                    openNotebook = controller.createNotebook(
                        title: "UI test \(UUID().uuidString.prefix(8))", folder: "",
                        template: .ruled, size: named ?? .defaultForNewNotebooks)
                    return
                }
                if openNotebook == nil,
                    CommandLine.arguments.contains("-debugOpenNotebook")
                        || CommandLine.arguments.contains("-debugStroke")
                        || CommandLine.arguments.contains("-debugHandwriting")
                {
                    openNotebook = controller.notebooks.first
                }
            #endif
            follow(deepLink)
            #if DEBUG
                runDebugSearchIfRequested()
            #endif
        }
        .onChange(of: deepLink) { _, link in follow(link) }
    }

    #if DEBUG
        /// The simulator has no keyboard the scripts can drive, so search gets
        /// the same treatment every other M2-M5 interaction did: a launch
        /// argument that puts the app where a person's finger would have.
        ///
        /// `-debugSearch <query>` fills the search field; `-debugReveal
        /// <query>` goes straight to the first hit with the outline held open.
        private func runDebugSearchIfRequested() {
            let arguments = CommandLine.arguments
            if let index = arguments.firstIndex(of: "-debugSearch"),
                arguments.indices.contains(index + 1)
            {
                search = arguments[index + 1]
            }
            guard let index = arguments.firstIndex(of: "-debugReveal"),
                arguments.indices.contains(index + 1)
            else { return }
            let query = arguments[index + 1]
            HighlightLayer.duration = 120
            guard let group = controller.search(query).first(where: { !$0.pages.isEmpty }),
                let match = group.pages.first,
                let notebook = controller.notebook(atPath: match.notebookPath)
            else { return }
            open(
                SearchDestination(
                    notebookPath: match.notebookPath, pageID: match.pageID,
                    box: match.unionBox),
                notebook: notebook)
        }
    #endif

    /// Opens what the web app (or a search result) pointed at. A notebook this
    /// device does not have is not an error worth a dialog: the library is
    /// what the person sees, and the sync engine may still be fetching it.
    private func follow(_ link: PaperDeepLink) {
        let notebook: Notebook?
        if let path = link.notebook {
            notebook = controller.notebook(atPath: path)
        } else if let notebookID = link.notebookID {
            notebook = try? controller.store.notebook(id: notebookID)
        } else {
            notebook = nil
        }
        guard let notebook else { return }
        open(
            SearchDestination(
                notebookPath: notebook.path,
                pageID: link.page,
                box: link.box),
            notebook: notebook)
    }

    private func open(_ destination: SearchDestination, notebook: Notebook) {
        pendingReveal = destination
        if openNotebook?.id != notebook.id {
            openNotebook = notebook
        }
    }

    private var library: some View {
        NavigationSplitView(columnVisibility: $columnVisibility) {
            sidebar
        } detail: {
            grid
        }
        .searchable(text: $search, prompt: "Search notebooks and handwriting")
        .searchFocused($searchFocused)
        .background {
            // Cmd-F is the search shortcut everywhere else in the system, so
            // it is the search shortcut here. No visible control.
            Button("Find") { searchFocused = true }
                .keyboardShortcut("f", modifiers: .command)
                .opacity(0)
                .accessibilityHidden(true)
        }
        .sheet(isPresented: $isShowingTrash) {
            TrashView(controller: controller)
        }
        .sheet(isPresented: $isCreating) {
            NewNotebookSheet(folder: currentFolder) { title, folder, template, size in
                isCreating = false
                if let notebook = controller.createNotebook(
                    title: title, folder: folder, template: template, size: size)
                {
                    openNotebook = notebook
                }
            } onCancel: {
                isCreating = false
            }
        }
        .alert("Rename notebook", isPresented: Binding(get: { renaming != nil }, set: { if !$0 { renaming = nil } })) {
            TextField("Title", text: $renameText)
            Button("Cancel", role: .cancel) { renaming = nil }
            Button("Rename") {
                if let notebook = renaming { controller.rename(notebook, to: renameText) }
                renaming = nil
            }
        }
        .alert("Move notebook", isPresented: Binding(get: { moving != nil }, set: { if !$0 { moving = nil } })) {
            TextField("Folder", text: $moveText)
            Button("Cancel", role: .cancel) { moving = nil }
            Button("Move") {
                if let notebook = moving { controller.move(notebook, toFolder: moveText) }
                moving = nil
            }
        } message: {
            Text("Use slashes for nested folders, or leave it empty for the top level.")
        }
        .overlay(alignment: .bottom) {
            if let pending = pendingUndo {
                UndoToast(
                    model: pending,
                    onUndo: {
                        controller.restore(pending.token)
                        pendingUndo = nil
                    },
                    onExpire: { pendingUndo = nil })
                .padding(.bottom, 24)
                .id(pending.id)
            }
        }
    }

    // MARK: - Sidebar

    private var sidebar: some View {
        List(selection: $scope) {
            Section {
                row(.recents, symbol: "clock")
                row(.favorites, symbol: "star")
                row(.all, symbol: "books.vertical")
            }
            Section {
                Button {
                    isShowingTrash = true
                } label: {
                    Label {
                        Text("Trash").font(.body).foregroundStyle(PaperTokens.text)
                    } icon: {
                        Image(systemName: "trash").symbolRenderingMode(.monochrome)
                    }
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Trash")
                .accessibilityHint("Deleted notebooks and pages, restorable for thirty days")
            }
            if !controller.folders.isEmpty {
                Section("Folders") {
                    ForEach(controller.folders, id: \.self) { folder in
                        row(.folder(folder), symbol: "folder")
                    }
                }
            }
            if !controller.tags.isEmpty {
                Section("Tags") {
                    ForEach(controller.tags, id: \.self) { tag in
                        row(.tag(tag), symbol: "number")
                    }
                }
            }
        }
        .listStyle(.sidebar)
        .navigationTitle("Paper")
    }

    /// Lives on the detail column, not the sidebar: on a portrait iPad the
    /// sidebar is collapsed behind a toggle, and the way out of Paper is not
    /// allowed to be something you have to go looking for.
    private var backButton: some View {
        Button(action: onBack) {
            Image(systemName: "chevron.backward")
                .symbolRenderingMode(.monochrome)
        }
        .accessibilityLabel("Back to EXSCALIA")
        .accessibilityHint("Leaves Paper and returns to the web app")
    }

    private func row(_ scope: LibraryScope, symbol: String) -> some View {
        Label {
            Text(scope.title).font(.body)
        } icon: {
            Image(systemName: symbol).symbolRenderingMode(.monochrome)
        }
        .tag(scope)
        .accessibilityLabel(scope.title)
    }

    // MARK: - Grid

    @ViewBuilder
    private var grid: some View {
        if searchQuery.isEmpty {
            coverGrid
        } else {
            SearchView(controller: controller, query: searchQuery) { destination in
                guard let notebook = controller.notebook(atPath: destination.notebookPath) else {
                    return
                }
                open(destination, notebook: notebook)
            }
            .navigationTitle("Search")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) { backButton }
            }
        }
    }

    private var coverGrid: some View {
        ScrollView {
            if visibleNotebooks.isEmpty {
                emptyState
            } else {
                LazyVGrid(
                    columns: [GridItem(.adaptive(minimum: 170, maximum: 240), spacing: 20)],
                    spacing: 24
                ) {
                    ForEach(visibleNotebooks) { notebook in
                        NotebookCard(controller: controller, notebook: notebook) {
                            openNotebook = notebook
                        }
                        .contextMenu { menu(for: notebook) }
                    }
                }
                .padding(20)
            }
        }
        .background(PaperTokens.background)
        .navigationTitle((scope ?? .recents).title)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarLeading) { backButton }
            ToolbarItem(placement: .topBarTrailing) {
                // One primary action, per the plan: everything else is a
                // context menu on a notebook.
                Button(action: { isCreating = true }) {
                    Image(systemName: "plus").symbolRenderingMode(.monochrome)
                }
                .keyboardShortcut("n", modifiers: .command)
                .accessibilityLabel("New notebook")
                .accessibilityHint("Creates a notebook and opens its first page")
            }
        }
    }

    private var emptyState: some View {
        VStack(spacing: 10) {
            Image(systemName: "books.vertical")
                .font(.system(size: 34, weight: .light))
                .symbolRenderingMode(.monochrome)
                .foregroundStyle(PaperTokens.textMuted)
                .accessibilityHidden(true)
            Text("No notebooks here")
                .font(.title3)
                .foregroundStyle(PaperTokens.text)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 80)
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder
    private func menu(for notebook: Notebook) -> some View {
        Button("Rename") {
            renameText = notebook.title
            renaming = notebook
        }
        Button("Move") {
            moveText = notebook.folder
            moving = notebook
        }
        Button(notebook.favorite ? "Remove from favorites" : "Add to favorites") {
            controller.toggleFavorite(notebook)
        }
        Button("Duplicate") { controller.duplicate(notebook) }
        Divider()
        Button("Delete", role: .destructive) {
            guard let token = controller.delete(notebook) else { return }
            pendingUndo = UndoToastModel(message: "Notebook deleted", token: token)
        }
    }

    // MARK: - Filtering

    private var currentFolder: String {
        if case .folder(let name) = scope ?? .recents { return name }
        return ""
    }

    private var searchQuery: String {
        search.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private var visibleNotebooks: [Notebook] {
        var result = controller.notebooks
        switch scope ?? .recents {
        case .recents:
            result = (try? controller.store.recentlyOpened()) ?? result
        case .favorites:
            result = result.filter(\.favorite)
        case .all:
            break
        case .folder(let name):
            result = result.filter { $0.folder == name || $0.folder.hasPrefix(name + "/") }
        case .tag(let name):
            result = result.filter { $0.tags.contains(name) }
        }
        return result
    }
}

/// One notebook in the grid: its cover, its title, when it was last touched.
struct NotebookCard: View {
    let controller: PaperController
    let notebook: Notebook
    let onOpen: () -> Void

    var body: some View {
        Button(action: onOpen) {
            VStack(alignment: .leading, spacing: 8) {
                cover
                    .aspectRatio(aspect, contentMode: .fit)
                    .frame(maxWidth: .infinity)
                    .clipShape(RoundedRectangle(cornerRadius: 4))
                    .overlay(
                        RoundedRectangle(cornerRadius: 4)
                            .strokeBorder(PaperTokens.border, lineWidth: 1))

                HStack(spacing: 6) {
                    Text(notebook.title)
                        .font(.callout)
                        .foregroundStyle(PaperTokens.text)
                        .lineLimit(2)
                        .multilineTextAlignment(.leading)
                    if notebook.favorite {
                        Image(systemName: "star.fill")
                            .font(.caption2)
                            .symbolRenderingMode(.monochrome)
                            .foregroundStyle(PaperTokens.textMuted)
                            .accessibilityHidden(true)
                    }
                }
                Text(relative)
                    .font(.caption)
                    .foregroundStyle(PaperTokens.textMuted)
            }
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(
            "\(notebook.title), \(notebook.pages.count) pages, edited \(relative)"
                + (notebook.favorite ? ", favorite" : ""))
        .accessibilityHint("Opens the notebook where you left off")
    }

    private var aspect: CGFloat {
        let size = notebook.paperSize.points
        return size.width / size.height
    }

    @ViewBuilder
    private var cover: some View {
        if let image = controller.coverImage(for: notebook) {
            Image(uiImage: image)
                .resizable()
                .aspectRatio(contentMode: .fill)
        } else {
            TemplatePreview(template: notebook.template)
        }
    }

    private var relative: String {
        SaveState.relative(since: notebook.modified)
    }
}

/// The one sheet in the library: title, template, size. Ruled and Infinite are
/// preselected, and Return opens page 1 ready to write.
struct NewNotebookSheet: View {
    let folder: String
    let onCreate: (String, String, Template, PaperSize) -> Void
    let onCancel: () -> Void

    @State private var title = ""
    @State private var template: Template = .ruled
    // Infinite and Ruled: a page that runs out of room is the thing people
    // notice first, so the default is the one that does not.
    @State private var size: PaperSize = .defaultForNewNotebooks
    @FocusState private var titleFocused: Bool

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Title", text: $title)
                        .focused($titleFocused)
                        .submitLabel(.done)
                        .onSubmit(create)
                        .accessibilityLabel("Notebook title")
                }
                Section("Template") {
                    HStack(spacing: 12) {
                        ForEach(Template.allCases, id: \.self) { option in
                            templateOption(option)
                        }
                    }
                    .padding(.vertical, 4)
                }
                Section("Size") {
                    Picker("Size", selection: $size) {
                        ForEach(PaperSize.allCases, id: \.self) { option in
                            Text(option.displayName).tag(option)
                        }
                    }
                    .pickerStyle(.segmented)
                    .accessibilityLabel("Paper size")
                }
            }
            .navigationTitle("New notebook")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel", action: onCancel)
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Create", action: create)
                }
            }
            .onAppear { titleFocused = true }
        }
    }

    private func templateOption(_ option: Template) -> some View {
        Button {
            template = option
        } label: {
            VStack(spacing: 6) {
                TemplatePreview(template: option)
                    .frame(width: 54, height: 74)
                    .clipShape(RoundedRectangle(cornerRadius: 3))
                    .overlay(
                        RoundedRectangle(cornerRadius: 3)
                            .strokeBorder(
                                template == option ? PaperTokens.accent : PaperTokens.border,
                                lineWidth: template == option ? 2 : 1))
                Text(option.displayName)
                    .font(.caption)
                    .foregroundStyle(template == option ? PaperTokens.accent : PaperTokens.textMuted)
            }
        }
        .buttonStyle(.plain)
        .accessibilityLabel(option.displayName)
        .accessibilityAddTraits(template == option ? [.isSelected] : [])
    }

    private func create() {
        let trimmed = title.trimmingCharacters(in: .whitespacesAndNewlines)
        onCreate(trimmed.isEmpty ? "Untitled" : trimmed, folder, template, size)
    }
}
