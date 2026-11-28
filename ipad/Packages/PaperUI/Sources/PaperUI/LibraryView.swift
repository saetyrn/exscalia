import Observation
import PaperCore
import PhotosUI
import SwiftUI
import UIKit
import UniformTypeIdentifiers

/// Where the web app asked Paper to open. Every field is optional: "open
/// Paper" on its own is a legitimate request.
public struct PaperDeepLink: Equatable, Sendable {
    public var notebook: String?
    public var notebookID: String?
    public var page: String?
    public var box: CGRect?
    /// A folder to show in the library, root kept (`paper/Work`), when the
    /// web asked for `/paper?folder=…` rather than a notebook.
    public var folder: String?

    public init(
        notebook: String? = nil,
        notebookID: String? = nil,
        page: String? = nil,
        box: CGRect? = nil,
        folder: String? = nil
    ) {
        self.notebook = notebook
        self.notebookID = notebookID
        self.page = page
        self.box = box
        self.folder = folder
    }

    public var isEmpty: Bool { notebook == nil && notebookID == nil && page == nil && folder == nil }
}

// MARK: - State

/// The library sidebar's four places.
enum LibrarySection: String, CaseIterable, Identifiable, Sendable {
    case documents, favorites, recents, trash

    var id: String { rawValue }

    var title: String {
        switch self {
        case .documents: return "Documents"
        case .favorites: return "Favorites"
        case .recents: return "Recents"
        case .trash: return "Trash"
        }
    }

    var symbol: String {
        switch self {
        case .documents: return "doc.on.doc"
        case .favorites: return "star"
        case .recents: return "clock"
        case .trash: return "trash"
        }
    }
}

/// What the library shows in a sheet.
enum LibrarySheet: Identifiable {
    case notebook, whiteboard, folder
    case move([PaperCore.LibraryItem])
    case info(PaperCore.LibraryItem)

    var id: String {
        switch self {
        case .notebook: return "notebook"
        case .whiteboard: return "whiteboard"
        case .folder: return "folder"
        case .move(let items): return "move:" + items.map(\.id).joined(separator: ",")
        case .info(let item): return "info:" + item.id
        }
    }
}

/// Where the library is and what it is doing: the section, the open folder,
/// search, select mode, the keyboard's row, the open menu and sheet. One per
/// window, so two windows can browse two folders.
@MainActor
@Observable
final class LibraryState {
    var section: LibrarySection = .documents
    /// The open folder under Documents, by id; nil at the top level.
    private(set) var folderID: String?
    /// Where that folder was when it was opened, should its id change.
    private(set) var folderPath = PaperController.rootFolderPath
    var isSidebarVisible = false
    var isSearching = false
    var query = ""
    /// Bumped to put the cursor in the search field.
    var searchFocusRequest = 0
    var isSelecting = false
    var selection: Set<String> = []
    /// The row or tile the arrow keys are on.
    var keyboardItemID: String?
    /// The document last opened from the list, which the list scrolls back
    /// to when the editor goes.
    var returnItemID: String?
    var menu: LibraryMenuRequest?
    var isShowingNewMenu = false
    var sheet: LibrarySheet?
    var pendingUndo: UndoToastModel?
    var pendingTokens: [TrashToken] = []
    var isImporting = false
    var isPickingPhoto = false
    var isTakingPhoto = false
    var alert: String?
    /// Columns the grid has at the window's width, for the arrow keys.
    var gridColumns = 1

    /// The open folder, found by id, else where it was.
    func currentFolder(_ controller: PaperController) -> PaperFolder? {
        guard section == .documents, let folderID else { return nil }
        return controller.libraryFolder(id: folderID) ?? controller.libraryFolder(path: folderPath)
    }

    /// The folder the listing shows, root kept.
    func currentPath(_ controller: PaperController) -> String {
        currentFolder(controller)?.path ?? PaperController.rootFolderPath
    }

    /// Where something new lands: the open folder, or the top level from
    /// Favorites, Recents, Trash or a search.
    func newItemFolderPath(_ controller: PaperController) -> String {
        guard section == .documents, !isSearching else { return PaperController.rootFolderPath }
        return currentPath(controller)
    }

    func items(_ controller: PaperController, sort: LibrarySort, filter: LibraryFilter)
        -> [PaperCore.LibraryItem]
    {
        switch section {
        case .documents:
            return LibraryArrangement.arrange(
                controller.libraryItems(inFolder: currentPath(controller)), sort: sort, filter: filter)
        case .favorites:
            return controller.favoriteItems(sort: sort, filter: filter)
        case .recents:
            return controller.recentItems(filter: filter)
        case .trash:
            return []
        }
    }

    /// A folder opens under Documents, wherever it was tapped.
    func open(_ folder: PaperFolder) {
        section = .documents
        folderID = folder.id
        folderPath = folder.path
        leaveListing()
    }

    func goToRoot() {
        folderID = nil
        folderPath = PaperController.rootFolderPath
        leaveListing()
    }

    /// The back chip: one folder up.
    func goUp(_ controller: PaperController) {
        guard let folder = currentFolder(controller) else { return }
        let parent = folder.parentPath
        let previous = folder.id
        if let up = controller.libraryFolder(path: parent) {
            open(up)
        } else {
            goToRoot()
        }
        // The keyboard lands on the folder just left.
        keyboardItemID = "folder/\(previous)"
    }

    func select(_ section: LibrarySection) {
        self.section = section
        if section == .documents { goToRoot() } else { leaveListing() }
        isSearching = false
    }

    /// Follows the open folder when it is renamed or moved, and climbs to the
    /// nearest folder still there when it has gone.
    func follow(_ controller: PaperController) {
        guard let folderID else { return }
        if let folder = controller.libraryFolder(id: folderID) {
            folderPath = folder.path
            return
        }
        if let folder = controller.libraryFolder(path: folderPath) {
            self.folderID = folder.id
            return
        }
        var path = PaperController.parent(of: folderPath)
        while path != PaperController.rootFolderPath {
            if let folder = controller.libraryFolder(path: path) {
                self.folderID = folder.id
                folderPath = folder.path
                return
            }
            path = PaperController.parent(of: path)
        }
        goToRoot()
    }

    func beginSearch() {
        isSearching = true
        isSelecting = false
        searchFocusRequest += 1
    }

    func endSearch() {
        isSearching = false
        query = ""
    }

    func beginSelecting() {
        isSelecting = true
        selection = []
        menu = nil
    }

    func endSelecting() {
        isSelecting = false
        selection = []
    }

    func toggleSelection(_ item: PaperCore.LibraryItem) {
        if selection.contains(item.id) {
            selection.remove(item.id)
        } else {
            selection.insert(item.id)
        }
    }

    /// The arrow keys: `delta` rows or tiles on, clamped to the ends; the
    /// first press lands on the first (or the last).
    func moveKeyboard(by delta: Int, in items: [PaperCore.LibraryItem]) {
        guard !items.isEmpty else { return }
        guard let current = keyboardItemID, let index = items.firstIndex(where: { $0.id == current })
        else {
            keyboardItemID = (delta < 0 ? items.last : items.first)?.id
            return
        }
        keyboardItemID = items[min(max(index + delta, 0), items.count - 1)].id
    }

    /// Cmd-F: search, out of select mode.
    func searchFromKeyboard() {
        endSelecting()
        beginSearch()
    }

    /// Return: the row the keys are on, to open. In select mode it is
    /// toggled instead, and with no row yet the keys land on the first;
    /// both answer nil.
    func activateKeyboardItem(in items: [PaperCore.LibraryItem]) -> PaperCore.LibraryItem? {
        guard let id = keyboardItemID, let item = items.first(where: { $0.id == id }) else {
            moveKeyboard(by: 1, in: items)
            return nil
        }
        if isSelecting {
            toggleSelection(item)
            return nil
        }
        return item
    }

    /// Escape closes an open menu, then search, then select mode, then the
    /// sidebar, one at a time.
    func escape() {
        if menu != nil || isShowingNewMenu {
            menu = nil
            isShowingNewMenu = false
        } else if isSearching {
            endSearch()
        } else if isSelecting {
            endSelecting()
        } else if isSidebarVisible {
            isSidebarVisible = false
        }
    }

    private func leaveListing() {
        isSelecting = false
        selection = []
        keyboardItemID = nil
        returnItemID = nil
        menu = nil
    }
}

// MARK: - Library

/// The library: Documents as a folder browser, with Favorites, Recents and
/// the Trash in its sidebar. Laid out on the owner's screenshots of the
/// reference, in list or grid view, following the system appearance.
public struct LibraryView: View {
    @State private var controller: PaperController
    @State private var state = LibraryState()
    @State private var anchor = PresenterAnchor()
    @State private var photo: PhotosPickerItem?
    @AppStorage(LibrarySort.storageKey) private var sortValue = LibrarySort.standard.rawValue
    @AppStorage(LibraryFilter.storageKey) private var filterValue = LibraryFilter.standard.rawValue
    @AppStorage(LibraryLayout.storageKey) private var layoutValue = LibraryLayout.standard.rawValue

    // The window opens documents in its tabs.
    @Environment(\.paperOpenDocument) private var openDocument
    @Environment(\.paperLibrarySearchRequest) private var searchRequest
    @Environment(\.paperLibraryFolderID) private var requestedFolderID
    /// False while the editor covers the library (the window disables it).
    @Environment(\.isEnabled) private var isEnabled

    private let onBack: () -> Void
    private let onOpenOnWeb: ((String, String?) -> Void)?
    private let onNavigateOnWeb: ((String) -> Void)?
    private let scanDocument: (@MainActor (UIViewController) async throws -> Data)?

    public init(
        controller: PaperController,
        onOpenOnWeb: ((String, String?) -> Void)? = nil,
        onNavigateOnWeb: ((String) -> Void)? = nil,
        scanDocument: (@MainActor (UIViewController) async throws -> Data)? = nil,
        onBack: @escaping () -> Void
    ) {
        _controller = State(initialValue: controller)
        self.onOpenOnWeb = onOpenOnWeb
        self.onNavigateOnWeb = onNavigateOnWeb
        self.scanDocument = scanDocument
        self.onBack = onBack
    }

    public var body: some View {
        HStack(spacing: 0) {
            // Under the editor the rows are not drawn at all: hidden, their
            // controls still answered UI tests beside the editor's own
            // Sidebar and Search, and they held every thumbnail. Where the
            // library was is in `state`, and the list scrolls back to it.
            if isEnabled {
                if state.isSidebarVisible {
                    LibrarySidebar(state: state)
                        .transition(.move(edge: .leading))
                }
                LibraryBrowserView(
                    controller: controller, state: state, sort: sort, filter: filter,
                    layout: layout, items: items, actions: actions, commands: commands)
            } else {
                Color.clear
            }
        }
        .background(PaperTokens.libraryBackground.ignoresSafeArea())
        .background { PresenterAnchorView(anchor: anchor) }
        .background { shortcuts }

        .sheet(item: $state.sheet) { sheet in sheetContent(sheet) }
        .fileImporter(
            isPresented: $state.isImporting, allowedContentTypes: [.pdf],
            allowsMultipleSelection: true
        ) { result in
            if case .success(let urls) = result { importFiles(urls) }
        }
        .photosPicker(isPresented: $state.isPickingPhoto, selection: $photo, matching: .images)
        .onChange(of: photo) { _, item in
            guard let item else { return }
            photo = nil
            importPhoto(item)
        }
        .fullScreenCover(isPresented: $state.isTakingPhoto) {
            CameraPicker { image in
                state.isTakingPhoto = false
                if let image { importImage(image, title: "Photo") }
            }
            .ignoresSafeArea()
        }
        .overlay(alignment: .bottom) { toast }
        .alert(
            state.alert ?? "",
            isPresented: Binding(get: { state.alert != nil }, set: { if !$0 { state.alert = nil } })
        ) {
            Button("OK", role: .cancel) {}
        }
        .onChange(of: searchRequest) { _, _ in
            state.endSelecting()
            state.beginSearch()
        }
        .onChange(of: requestedFolderID) { _, id in showFolder(id: id) }
        .onChange(of: controller.libraryFolders) { _, _ in state.follow(controller) }
        .onAppear { showFolder(id: requestedFolderID) }
        .task {
            #if DEBUG
                await runDebugArguments()
            #endif
        }
        .newSheetDebugHost()
    }

    // MARK: Reading

    private var sort: Binding<LibrarySort> {
        Binding(
            get: { LibrarySort(rawValue: sortValue) ?? .standard },
            set: { sortValue = $0.rawValue })
    }

    private var filter: Binding<LibraryFilter> {
        Binding(
            get: { LibraryFilter(rawValue: filterValue) ?? .standard },
            set: { filterValue = $0.rawValue })
    }

    private var layout: Binding<LibraryLayout> {
        Binding(
            get: { LibraryLayout(rawValue: layoutValue) ?? .standard },
            set: { layoutValue = $0.rawValue })
    }

    private var items: [PaperCore.LibraryItem] {
        state.items(controller, sort: sort.wrappedValue, filter: filter.wrappedValue)
    }

    /// The item as the library has it now: a menu or a drag may hold an
    /// older copy.
    private func fresh(_ item: PaperCore.LibraryItem) -> PaperCore.LibraryItem? {
        switch item {
        case .folder(let folder):
            return (controller.libraryFolder(id: folder.id) ?? controller.libraryFolder(path: folder.path))
                .map(PaperCore.LibraryItem.folder)
        case .document(let notebook):
            return controller.notebooks.first { $0.id == notebook.id }.map(PaperCore.LibraryItem.document)
        }
    }

    /// `folder/<id>` or `document/<id>`, as `LibraryItem.id` spells them.
    private func item(id: String) -> PaperCore.LibraryItem? {
        if id.hasPrefix("folder/") {
            return controller.libraryFolder(id: String(id.dropFirst("folder/".count)))
                .map(PaperCore.LibraryItem.folder)
        }
        if id.hasPrefix("document/") {
            let notebookID = String(id.dropFirst("document/".count))
            return controller.notebooks.first { $0.id == notebookID }.map(PaperCore.LibraryItem.document)
        }
        return nil
    }

    private func showFolder(id: String?) {
        guard let id, let folder = controller.libraryFolder(id: id) else { return }
        state.endSearch()
        state.open(folder)
    }

    // MARK: Item actions

    private var actions: LibraryItemActions {
        LibraryItemActions(
            open: { item in open(item) },
            toggleFavorite: { item in
                guard let item = fresh(item) else { return }
                controller.setFavorite(item, !item.isFavorite)
            },
            toggleSelection: { item in state.toggleSelection(item) },
            rename: { item, name in
                guard let item = fresh(item) else { return }
                controller.rename(item, to: name)
            },
            duplicate: { item in
                guard let item = fresh(item) else { return }
                controller.duplicate(item)
            },
            move: { item in
                guard let item = fresh(item) else { return }
                present(.move([item]))
            },
            openInNewWindow: { item in
                switch item {
                case .folder(let folder):
                    PaperWindowView.requestWindow(.library(folderID: folder.id))
                case .document(let notebook):
                    controller.autosave.flushAll()
                    PaperWindowView.requestWindow(.document(id: notebook.id, pageID: nil))
                }
            },
            export: { item in
                guard let item = fresh(item) else { return }
                export([item])
            },
            info: { item in
                guard let item = fresh(item) else { return }
                present(.info(item))
            },
            trash: { item in
                guard let item = fresh(item) else { return }
                trash([item])
            },
            setColor: { folder, color in controller.setFolderColor(folder, color) },
            setIcon: { folder, icon in controller.setFolderIcon(folder, icon) },
            drop: { ids, folder in drop(ids, into: folder.path) },
            showMenu: { item, page in
                // After the long-press menu has gone.
                Task { @MainActor in
                    try? await Task.sleep(for: .milliseconds(450))
                    state.menu = LibraryMenuRequest(itemID: item.id, page: page)
                }
            })
    }

    private func open(_ item: PaperCore.LibraryItem) {
        switch item {
        case .folder(let folder):
            state.open(folder)
        case .document(let notebook):
            state.returnItemID = item.id
            openDocument(notebook)
        }
    }

    /// Sheets wait for a menu or popover that is closing.
    private func present(_ sheet: LibrarySheet) {
        afterMenus { state.sheet = sheet }
    }

    private func afterMenus(_ work: @escaping @MainActor () -> Void) {
        let hadMenu = state.menu != nil || state.isShowingNewMenu
        state.menu = nil
        state.isShowingNewMenu = false
        Task { @MainActor in
            try? await Task.sleep(for: .milliseconds(hadMenu ? 400 : 50))
            work()
        }
    }

    private func drop(_ ids: [String], into target: String) -> Bool {
        let dropped = ids.compactMap(item(id:))
        guard !dropped.isEmpty,
            PaperController.canMove(dropped, into: target, notebooks: controller.notebooks),
            !PaperController.isAlreadyIn(dropped, target)
        else { return false }
        let moved = controller.move(dropped, toFolderPath: target)
        if state.isSelecting { state.selection.subtract(ids) }
        if !moved { state.alert = "Could not move" }
        return moved
    }

    private func trash(_ trashed: [PaperCore.LibraryItem]) {
        let tokens = trashed.compactMap(controller.trash)
        guard !tokens.isEmpty else { return }
        let message: String
        if trashed.count == 1, let only = trashed.first {
            message = "\(LibraryItemText.kind(only)) deleted"
        } else {
            message = "\(trashed.count) items deleted"
        }
        state.pendingTokens = tokens
        state.pendingUndo = UndoToastModel(message: message, token: tokens[0])
    }

    private func export(_ exported: [PaperCore.LibraryItem]) {
        state.menu = nil
        UIAccessibility.post(notification: .announcement, argument: "Exporting")
        Task { @MainActor in
            var urls: [URL] = []
            for item in exported {
                switch item {
                case .folder(let folder):
                    if let url = await FolderExporter.export(folder, controller: controller) {
                        urls.append(url)
                    }
                case .document(let notebook):
                    if let url = await controller.exportPDF(notebook: notebook) { urls.append(url) }
                }
            }
            guard !urls.isEmpty else {
                state.alert = "Export failed"
                return
            }
            share(urls)
        }
    }

    private func share(_ urls: [URL]) {
        guard let presenter = anchor.top, let view = anchor.viewController?.view else { return }
        let activity = UIActivityViewController(activityItems: urls, applicationActivities: nil)
        activity.popoverPresentationController?.sourceView = view
        activity.popoverPresentationController?.sourceRect = CGRect(
            x: view.bounds.maxX - 120, y: view.safeAreaInsets.top + 110, width: 1, height: 1)
        presenter.present(activity, animated: !UIAccessibility.isReduceMotionEnabled)
    }

    // MARK: Commands

    private var commands: LibraryCommands {
        LibraryCommands(
            new: { kind in new(kind) },
            canScan: scanDocument != nil,
            canTakePhoto: UIImagePickerController.isSourceTypeAvailable(.camera),
            saveStateTapped: saveStateTapped,
            exit: onBack,
            openSearchResult: { destination in
                guard let notebook = controller.notebook(atPath: destination.notebookPath) else {
                    return
                }
                openDocument(notebook, reveal: destination)
            },
            exportSelection: { export(selectedItems) },
            duplicateSelection: {
                for item in selectedItems { controller.duplicate(item) }
                state.endSelecting()
            },
            moveSelection: {
                let selected = selectedItems
                guard !selected.isEmpty else { return }
                present(.move(selected))
            },
            trashSelection: {
                let selected = selectedItems
                state.endSelecting()
                trash(selected)
            },
            dropOnBack: { ids in
                guard let folder = state.currentFolder(controller) else { return false }
                return drop(ids, into: folder.parentPath)
            })
    }

    private var selectedItems: [PaperCore.LibraryItem] {
        items.filter { state.selection.contains($0.id) }
    }

    private func new(_ kind: NewItemKind) {
        switch kind {
        case .notebook: present(.notebook)
        case .whiteboard: present(.whiteboard)
        case .folder: present(.folder)
        case .quickNote:
            state.isShowingNewMenu = false
            guard let notebook = controller.createQuickNote(in: state.newItemFolderPath(controller))
            else { return }
            openDocument(notebook, isQuickNote: true)
        case .importPDF: afterMenus { state.isImporting = true }
        case .image: afterMenus { state.isPickingPhoto = true }
        case .takePhoto: afterMenus { state.isTakingPhoto = true }
        case .scan: afterMenus { scan() }
        }
    }

    @ViewBuilder
    private func sheetContent(_ sheet: LibrarySheet) -> some View {
        switch sheet {
        case .notebook:
            NotebookSheet { title, options in
                state.sheet = nil
                create(title: title, kind: .notebook, options: options)
            } onCancel: {
                state.sheet = nil
            }
        case .whiteboard:
            WhiteboardSheet { title, options in
                state.sheet = nil
                create(title: title, kind: .whiteboard, options: options)
            } onCancel: {
                state.sheet = nil
            }
        case .folder:
            FolderSheet { name, color, icon in
                state.sheet = nil
                if let folder = controller.createFolder(
                    name: name, in: state.newItemFolderPath(controller), color: color, icon: icon)
                {
                    state.keyboardItemID = "folder/\(folder.id)"
                }
            } onCancel: {
                state.sheet = nil
            }
        case .move(let moving):
            MovePickerSheet(
                controller: controller, items: moving,
                onMoved: { _ in
                    state.sheet = nil
                    state.endSelecting()
                },
                onCancel: { state.sheet = nil })
        case .info(let item):
            ItemInfoSheet(controller: controller, item: item) { state.sheet = nil }
        }
    }

    private func create(title: String, kind: DocumentKind, options: NewDocumentOptions) {
        guard
            let notebook = controller.createDocument(
                title: title, folderPath: state.newItemFolderPath(controller), kind: kind,
                options: options)
        else {
            state.alert = "Could not create the \(kind.displayName.lowercased())"
            return
        }
        openDocument(notebook)
    }

    // MARK: From outside

    private func importFiles(_ urls: [URL]) {
        let folder = state.newItemFolderPath(controller)
        Task { @MainActor in
            var imported: [Notebook] = []
            for url in urls {
                let scoped = url.startAccessingSecurityScopedResource()
                let data = try? Data(contentsOf: url)
                if scoped { url.stopAccessingSecurityScopedResource() }
                guard let data,
                    let notebook = await controller.importDocument(
                        pdf: data, title: url.deletingPathExtension().lastPathComponent,
                        folderPath: folder)
                else { continue }
                imported.append(notebook)
            }
            if imported.count == 1, let only = imported.first {
                openDocument(only)
            } else if imported.count < urls.count {
                state.alert = "Some files could not be imported"
            }
        }
    }

    private func importPhoto(_ item: PhotosPickerItem) {
        Task { @MainActor in
            guard let data = try? await item.loadTransferable(type: Data.self),
                let image = UIImage(data: data)
            else {
                state.alert = "The image could not be read"
                return
            }
            importImage(image, title: "Image")
        }
    }

    /// A picture becomes a one-page document, upright, at most 3,000 pixels
    /// on its long edge.
    private func importImage(_ image: UIImage, title: String) {
        guard let cgImage = image.cgImage else { return }
        let orientation = image.imageOrientation
        let scale = image.scale
        let folder = state.newItemFolderPath(controller)
        Task { @MainActor in
            let pdf = await Task.detached(priority: .userInitiated) { () -> Data? in
                guard let normalized = ImageImport.normalized(
                    cgImage: cgImage, orientation: orientation, scale: scale),
                    let upright = UIImage(data: normalized.data)
                else { return nil }
                return PaperController.pdfDocument(fromImage: upright)
            }.value
            guard let pdf,
                let notebook = await controller.importDocument(pdf: pdf, title: title, folderPath: folder)
            else {
                state.alert = "The image could not be imported"
                return
            }
            openDocument(notebook)
        }
    }

    private func scan() {
        guard let scanDocument, let presenter = anchor.top else { return }
        let folder = state.newItemFolderPath(controller)
        Task { @MainActor in
            guard let data = try? await scanDocument(presenter) else { return }
            guard let notebook = await controller.importDocument(pdf: data, title: "Scan", folderPath: folder)
            else {
                state.alert = "The scan could not be imported"
                return
            }
            openDocument(notebook)
        }
    }

    // MARK: Save state and toast

    /// Every save state has something the person might want to do about it.
    private func saveStateTapped() {
        switch controller.saveState {
        case .conflict:
            if let target = controller.conflict,
                let notebook = controller.notebook(atPath: target.notebookPath)
            {
                openDocument(
                    notebook,
                    reveal: SearchDestination(notebookPath: notebook.path, pageID: target.siblingPageID))
            }
            controller.clearConflict()
        case .attention:
            controller.retryParkedOperations()
        case .signedOut:
            if let onNavigateOnWeb {
                controller.autosave.flushAll()
                onNavigateOnWeb("/")
            } else {
                controller.syncNow()
            }
        default:
            controller.syncNow()
        }
    }

    @ViewBuilder
    private var toast: some View {
        if let pending = state.pendingUndo {
            UndoToast(
                model: pending,
                onUndo: {
                    controller.restore(state.pendingTokens)
                    state.pendingUndo = nil
                    state.pendingTokens = []
                },
                onExpire: {
                    state.pendingUndo = nil
                    state.pendingTokens = []
                }
            )
            .padding(.bottom, 24)
            .id(pending.id)
        }
    }

    // MARK: Keyboard

    /// Hidden buttons rather than key commands on a view, so the keys work
    /// wherever the library is and stop the moment a sheet or the editor is
    /// in front of it (the window disables the library then). XCUITest
    /// still finds them (hiding each one from accessibility also silenced
    /// its key), so their titles are never a visible control's label.
    private var shortcuts: some View {
        let listKeys = !state.isSearching && state.section != .trash
        return VStack(spacing: 0) {
            key("New Notebook", "n", .command) { new(.notebook) }
            key("New Notebook", "n", [.command, .option]) { new(.notebook) }
            key("New Whiteboard", "w", [.command, .shift]) { new(.whiteboard) }
            key("New Quick Note", "n", [.command, .shift]) { new(.quickNote) }
            key("Search Documents", "f", .command) { state.searchFromKeyboard() }
            key("Show Sidebar", "s", [.command, .control]) { state.isSidebarVisible.toggle() }
            key("Enclosing Folder", "[", .command) { state.goUp(controller) }
            if listKeys {
                key("Previous Item", .upArrow) { state.moveKeyboard(by: -rowStep, in: items) }
                key("Next Item", .downArrow) { state.moveKeyboard(by: rowStep, in: items) }
                key("Item Left", .leftArrow) { left() }
                key("Item Right", .rightArrow) { right() }
                key("Open Item", .return) {
                    if let item = state.activateKeyboardItem(in: items) { open(item) }
                }
                key("Select Every Item", "a", .command) {
                    if !state.isSelecting { state.beginSelecting() }
                    state.selection = Set(items.map(\.id))
                }
                key("Trash Item", .delete, .command) { trashFromKeyboard() }
            }
            key("Dismiss", .escape) { state.escape() }
        }
        .opacity(0)
        .accessibilityHidden(true)
    }

    private func key(
        _ title: String, _ key: KeyEquivalent, _ modifiers: EventModifiers = [],
        action: @escaping () -> Void
    ) -> some View {
        Button(title, action: action)
            .keyboardShortcut(key, modifiers: modifiers)
    }

    /// One row in the list, one line of tiles in the grid.
    private var rowStep: Int {
        layout.wrappedValue == .grid ? max(state.gridColumns, 1) : 1
    }

    private func left() {
        if layout.wrappedValue == .grid {
            state.moveKeyboard(by: -1, in: items)
        } else {
            state.goUp(controller)
        }
    }

    private func right() {
        if layout.wrappedValue == .grid {
            state.moveKeyboard(by: 1, in: items)
        } else if let id = state.keyboardItemID, let item = item(id: id), case .folder = item {
            open(item)
        }
    }

    private func trashFromKeyboard() {
        if state.isSelecting {
            let selected = selectedItems
            state.endSelecting()
            trash(selected)
        } else if let id = state.keyboardItemID, let item = items.first(where: { $0.id == id }) {
            state.moveKeyboard(by: 1, in: items)
            if state.keyboardItemID == id { state.keyboardItemID = nil }
            trash([item])
        }
    }

    // MARK: DEBUG

    #if DEBUG
        /// The simulator has no keyboard the scripts can drive and `simctl`
        /// cannot tap, so the library's states each have a launch argument:
        ///
        /// - `-debugSearch <query>`: search, with the query typed.
        /// - `-debugLibraryFolder <name>`: that top-level folder open.
        /// - `-debugLibrary sidebar|new|select|favorites|recents|trash|keyboard`.
        /// - `-debugLibraryMenu <name>`, `-debugLibraryMove <name>`,
        ///   `-debugLibraryInfo <name>`: that item's menu, move picker or Info.
        /// - `-debugLibraryStar <name>`: star it first.
        private func runDebugArguments() async {
            let arguments = CommandLine.arguments
            func value(after flag: String) -> String? {
                guard let index = arguments.firstIndex(of: flag), arguments.indices.contains(index + 1)
                else { return nil }
                return arguments[index + 1]
            }
            await controller.waitForInitialReconcile()
            controller.seedDebugFoldersIfRequested()
            func named(_ name: String) -> PaperCore.LibraryItem? {
                items.first { $0.name == name }
                    ?? controller.libraryFolders.first { $0.name == name }.map(PaperCore.LibraryItem.folder)
                    ?? controller.notebooks.first { $0.title == name }.map(PaperCore.LibraryItem.document)
            }
            if let name = value(after: "-debugLibraryStar"), let item = named(name), !item.isFavorite {
                controller.setFavorite(item, true)
            }
            if let name = value(after: "-debugLibraryFolder"),
                let folder = controller.libraryFolders.first(where: { $0.name == name })
            {
                state.open(folder)
            }
            if let query = value(after: "-debugSearch") {
                state.isSearching = true
                state.query = query
            }
            switch value(after: "-debugLibrary") {
            case "sidebar": state.isSidebarVisible = true
            case "favorites": state.select(.favorites)
            case "recents": state.select(.recents)
            case "trash": state.select(.trash)
            case "keyboard": state.moveKeyboard(by: 1, in: items)
            case "select":
                state.beginSelecting()
                state.selection = Set(items.prefix(2).map(\.id))
            case "new":
                try? await Task.sleep(for: .seconds(1))
                state.isShowingNewMenu = true
            default: break
            }
            if let name = value(after: "-debugLibraryMenu"), let item = named(name) {
                try? await Task.sleep(for: .seconds(1))
                state.menu = LibraryMenuRequest(itemID: item.id, page: .main)
            }
            if let name = value(after: "-debugLibraryMove"), let item = named(name) {
                state.sheet = .move([item])
            }
            if let name = value(after: "-debugLibraryInfo"), let item = named(name) {
                state.sheet = .info(item)
            }
        }
    #endif
}

// MARK: - Sidebar

/// Documents, Favorites, Recents and Trash, and Settings at the foot when the
/// shell gives the library a way to open them. Tags are in each document's
/// Info now.
struct LibrarySidebar: View {
    @Bindable var state: LibraryState
    @Environment(\.paperOpenSettings) private var openSettings

    static let width: CGFloat = 260

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Color.clear.frame(height: LibraryBrowserView.topBarHeight)
            ForEach(LibrarySection.allCases) { section in
                entry(section)
            }
            Spacer(minLength: 0)
            if let openSettings {
                Button(action: openSettings) {
                    label(symbol: "gearshape", title: "Settings", selected: false)
                }
                .buttonStyle(.plain)
                .help("Settings")
                .accessibilityLabel("Settings")
                .accessibilityIdentifier("library.settings")
                .padding(.bottom, 8)
            }
        }
        .padding(.horizontal, 12)
        .frame(width: Self.width)
        .frame(maxHeight: .infinity)
        .background(PaperTokens.librarySidebar.ignoresSafeArea())
        .overlay(alignment: .trailing) {
            Rectangle()
                .fill(PaperTokens.libraryHairline)
                .frame(width: 1)
                .ignoresSafeArea()
                .accessibilityHidden(true)
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("library.sidebar")
    }

    private func entry(_ section: LibrarySection) -> some View {
        let selected = state.section == section
        return Button {
            state.select(section)
        } label: {
            label(symbol: section.symbol, title: section.title, selected: selected)
        }
        .buttonStyle(.plain)
        .accessibilityLabel(section.title)
        .accessibilityAddTraits(selected ? [.isSelected] : [])
    }

    private func label(symbol: String, title: String, selected: Bool) -> some View {
        HStack(spacing: 12) {
            Image(systemName: symbol)
                .font(.system(size: 18, weight: .regular))
                .symbolRenderingMode(.monochrome)
                .frame(width: 26)
                .accessibilityHidden(true)
            Text(title)
                .font(.system(size: 17, weight: selected ? .semibold : .regular))
            Spacer(minLength: 0)
        }
        .foregroundStyle(PaperTokens.text)
        .padding(.horizontal, 12)
        .frame(height: PaperTokens.minimumTarget)
        .background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(selected ? PaperTokens.librarySidebarSelected : Color.clear))
        .contentShape(Rectangle())
    }
}

private struct PaperOpenSettingsKey: EnvironmentKey {
    static let defaultValue: (@MainActor @Sendable () -> Void)? = nil
}

extension EnvironmentValues {
    /// Opens the shell's settings (server, recognition languages, the local
    /// cache): the library's sidebar shows a Settings entry while it is set.
    public var paperOpenSettings: (@MainActor @Sendable () -> Void)? {
        get { self[PaperOpenSettingsKey.self] }
        set { self[PaperOpenSettingsKey.self] = newValue }
    }
}

// MARK: - UIKit presentation

/// The view controller the library presents UIKit sheets from: the scanner
/// and the share sheet. Held weakly; the anchor view owns it.
@MainActor
final class PresenterAnchor {
    weak var viewController: UIViewController?

    /// The topmost controller presented over the anchor's window, which is
    /// the one that can present next.
    var top: UIViewController? {
        var current = viewController
        while let presented = current?.presentedViewController, !presented.isBeingDismissed {
            current = presented
        }
        return current
    }
}

struct PresenterAnchorView: UIViewControllerRepresentable {
    let anchor: PresenterAnchor

    func makeUIViewController(context: Context) -> UIViewController {
        let controller = UIViewController()
        controller.view.isUserInteractionEnabled = false
        controller.view.backgroundColor = .clear
        anchor.viewController = controller
        return controller
    }

    func updateUIViewController(_ controller: UIViewController, context: Context) {
        anchor.viewController = controller
    }
}

/// The camera, for "Take Photo".
struct CameraPicker: UIViewControllerRepresentable {
    let onFinish: (UIImage?) -> Void

    func makeUIViewController(context: Context) -> UIImagePickerController {
        let picker = UIImagePickerController()
        picker.sourceType = .camera
        picker.delegate = context.coordinator
        return picker
    }

    func updateUIViewController(_ picker: UIImagePickerController, context: Context) {}

    func makeCoordinator() -> Coordinator { Coordinator(onFinish: onFinish) }

    @MainActor
    final class Coordinator: NSObject, UIImagePickerControllerDelegate, UINavigationControllerDelegate {
        let onFinish: (UIImage?) -> Void

        init(onFinish: @escaping (UIImage?) -> Void) {
            self.onFinish = onFinish
        }

        func imagePickerController(
            _ picker: UIImagePickerController,
            didFinishPickingMediaWithInfo info: [UIImagePickerController.InfoKey: Any]
        ) {
            onFinish(info[.originalImage] as? UIImage)
        }

        func imagePickerControllerDidCancel(_ picker: UIImagePickerController) {
            onFinish(nil)
        }
    }
}
