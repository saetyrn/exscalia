import Foundation
import ImageIO
import PaperCore
import UIKit

// MARK: - What the library shows

/// A document's thumbnail, decoded, and the shape of the page it shows. A
/// class so `NSCache` can hold it; every field is a constant.
final class LibraryCover: @unchecked Sendable {
    let image: UIImage?
    /// Width over height of the first page (a board's render rectangle).
    let aspect: CGFloat
    let template: Template
    let paperColor: PaperColor
    let isBoard: Bool

    init(image: UIImage?, aspect: CGFloat, template: Template, paperColor: PaperColor, isBoard: Bool) {
        self.image = image
        self.aspect = aspect
        self.template = template
        self.paperColor = paperColor
        self.isBoard = isBoard
    }
}

/// A page whose title matched a search: the Outline group.
struct OutlineEntry: Identifiable, Equatable, Sendable {
    let notebookID: String
    let notebookPath: String
    let notebookTitle: String
    let pageID: String
    let pageTitle: String
    /// 1-based, as a person counts pages.
    let pageNumber: Int
    let kind: DocumentKind
    let modified: Date

    var id: String { "\(notebookPath)/\(pageID)" }
}

/// What the library's search finds, in the order it shows it.
struct LibrarySearchResults: Equatable {
    /// Each group shows its newest results first, at most this many.
    static let groupLimit = 50

    var folders: [PaperFolder] = []
    var documents: [Notebook] = []
    var outline: [OutlineEntry] = []
    var notebooks: [PageTextMatch] = []
    var whiteboards: [PageTextMatch] = []

    var isEmpty: Bool {
        folders.isEmpty && documents.isEmpty && outline.isEmpty && notebooks.isEmpty
            && whiteboards.isEmpty
    }
}

/// What is directly inside a folder, for its Info.
struct FolderCounts: Equatable, Sendable {
    var folders = 0
    var notebooks = 0
    var whiteboards = 0
}

/// One row of the move picker's folder tree.
struct FolderTreeNode: Identifiable, Equatable, Sendable {
    /// Nil for the top level, which the picker calls Documents.
    let folder: PaperFolder?
    let path: String
    let depth: Int
    let hasChildren: Bool

    var id: String { path }
    var name: String { folder?.name ?? "Documents" }
}

// MARK: - Library operations

/// The library's reads and writes, hung off the controller. Every write goes
/// to the store, then refreshes what the views read, wakes the sync engine
/// and tells the windows which document paths moved under them.
extension PaperController {
    nonisolated static let rootFolderPath = "paper"

    // MARK: Folders as the library shows them

    /// The folder records, plus the legacy record (`PaperFolder.legacy`) for
    /// every directory that holds a document or a folder and has no record
    /// yet. The launch reconcile writes the same record with the same id, so
    /// a folder shown before it finishes is the folder shown after: nothing
    /// is missing on the first launch over an older tree, and nothing is
    /// listed twice. A synthesised folder is dated by the newest thing in it.
    nonisolated static func libraryFolders(records: [PaperFolder], notebooks: [Notebook])
        -> [PaperFolder]
    {
        var byPath: [String: PaperFolder] = [:]
        for record in records where !record.path.isEmpty && byPath[record.path] == nil {
            byPath[record.path] = record
        }
        var missing: [String: Date] = [:]
        func claim(_ path: String, date: Date) {
            var current = path
            while current != rootFolderPath, current.hasPrefix(rootFolderPath + "/") {
                if byPath[current] == nil {
                    missing[current] = max(missing[current] ?? .distantPast, date)
                }
                current = parent(of: current)
            }
        }
        for notebook in notebooks { claim(notebook.folderPath, date: notebook.modified) }
        for record in records { claim(record.parentPath, date: record.modified) }
        for (path, date) in missing {
            byPath[path] = PaperFolder.legacy(path: path, now: date)
        }
        return byPath.values.sorted { $0.path < $1.path }
    }

    /// `paper` for a top-level entry, `paper/Work` for `paper/Work/Ideas`.
    nonisolated static func parent(of path: String) -> String {
        guard let slash = path.lastIndex(of: "/") else { return rootFolderPath }
        return String(path[..<slash])
    }

    /// `path` is `folderPath` or somewhere inside it, by whole components.
    nonisolated static func isInside(_ path: String, _ folderPath: String) -> Bool {
        path == folderPath || path.hasPrefix(folderPath + "/")
    }

    /// `Work/Plans` for `paper/Work/Plans`, `""` for the top level: what
    /// `Notebook.folder` and `PaperStore.move(notebookID:toFolder:)` take.
    nonisolated static func unprefixed(_ folderPath: String) -> String {
        guard folderPath != rootFolderPath, folderPath.hasPrefix(rootFolderPath + "/") else {
            return ""
        }
        return String(folderPath.dropFirst(rootFolderPath.count + 1))
    }

    /// The folders and documents directly inside `folderPath`, unsorted.
    nonisolated static func items(
        inFolder folderPath: String, folders: [PaperFolder], notebooks: [Notebook]
    ) -> [PaperCore.LibraryItem] {
        folders.filter { $0.parentPath == folderPath }.map(PaperCore.LibraryItem.folder)
            + notebooks.filter { $0.folderPath == folderPath }.map(PaperCore.LibraryItem.document)
    }

    nonisolated static func counts(
        inFolder folderPath: String, folders: [PaperFolder], notebooks: [Notebook]
    ) -> FolderCounts {
        var counts = FolderCounts()
        counts.folders = folders.filter { $0.parentPath == folderPath }.count
        for notebook in notebooks where notebook.folderPath == folderPath {
            if notebook.kind == .whiteboard { counts.whiteboards += 1 } else { counts.notebooks += 1 }
        }
        return counts
    }

    /// The folders from the top level down to `folderPath`, inclusive: what
    /// Info's location reads and the back chip climbs.
    nonisolated static func folderChain(to folderPath: String, in folders: [PaperFolder])
        -> [PaperFolder]
    {
        var chain: [PaperFolder] = []
        var current = folderPath
        while current != rootFolderPath, current.hasPrefix(rootFolderPath + "/") {
            if let folder = folders.first(where: { $0.path == current }) { chain.append(folder) }
            current = parent(of: current)
        }
        return chain.reversed()
    }

    func libraryItems(inFolder folderPath: String) -> [PaperCore.LibraryItem] {
        Self.items(inFolder: folderPath, folders: libraryFolders, notebooks: notebooks)
    }

    func libraryFolder(id: String) -> PaperFolder? {
        libraryFolders.first { $0.id == id }
    }

    func libraryFolder(path: String) -> PaperFolder? {
        libraryFolders.first { $0.path == path }
    }

    func folderCounts(_ folderPath: String) -> FolderCounts {
        Self.counts(inFolder: folderPath, folders: libraryFolders, notebooks: notebooks)
    }

    /// Favorites: starred folders and documents at any depth.
    func favoriteItems(sort: LibrarySort, filter: LibraryFilter) -> [PaperCore.LibraryItem] {
        LibraryArrangement.favorites(
            folders: libraryFolders, documents: notebooks, sort: sort, filter: filter)
    }

    /// Recents: the last twenty documents opened on this device.
    func recentItems(filter: LibraryFilter) -> [PaperCore.LibraryItem] {
        LibraryArrangement.recents(
            notebooks, opened: (try? store.openedDates()) ?? [:], filter: filter)
    }

    /// The record behind a folder the library shows. A folder synthesised
    /// before the launch reconcile reached it has none yet; the tree is read
    /// now, which writes it.
    private func record(for folder: PaperFolder) -> PaperFolder? {
        if let record = folderRecords.first(where: { $0.id == folder.id && $0.path == folder.path }) {
            return record
        }
        if let record = (try? store.folder(path: folder.path)) ?? nil { return record }
        _ = try? store.reconcile()
        refresh()
        return (try? store.folder(path: folder.path)) ?? nil
    }

    /// After any write: what the views read, the sync engine, and the windows
    /// holding a document whose path changed.
    func libraryChanged(_ paths: Set<String> = []) {
        refresh()
        if let sync { Task { await sync.kick() } }
        if !paths.isEmpty { notifyRemoteChange(paths) }
    }

    /// The paths of the documents inside a folder, which a rename or a move
    /// of the folder changes.
    private func documentPaths(inside folderPath: String) -> Set<String> {
        Set(notebooks.filter { Self.isInside($0.path, folderPath) }.map(\.path))
    }

    // MARK: Creating

    /// A notebook or whiteboard from the New sheets, in the folder at
    /// `folderPath` (`paper` for the top level).
    func createDocument(
        title: String, folderPath: String, kind: DocumentKind, options: NewDocumentOptions
    ) -> Notebook? {
        let notebook = try? store.createDocument(
            title: title, folderPath: folderPath, kind: kind, options: options)
        libraryChanged()
        return notebook
    }

    /// A Quick Note: a notebook made at once with the notebook defaults.
    func createQuickNote(in folderPath: String, defaults: UserDefaults = .standard) -> Notebook? {
        createDocument(
            title: "Quick Note", folderPath: folderPath, kind: .notebook,
            options: NewDocumentOptions.load(.notebook, defaults: defaults))
    }

    func createFolder(
        name: String, in parentPath: String, color: FolderColor = .blue, icon: FolderIcon? = nil
    ) -> PaperFolder? {
        let folder = try? store.createFolder(name: name, in: parentPath, color: color, icon: icon)
        libraryChanged()
        return folder
    }

    /// A PDF -- an import, a scan, a picture made into one -- as a new
    /// document in the folder at `folderPath`, one page per PDF page. Nil, and
    /// nothing left behind, when the PDF cannot be read.
    func importDocument(pdf data: Data, title: String, folderPath: String) async -> Notebook? {
        let title = NewDocumentTitle.resolved(title)
        guard
            let notebook = try? store.createNotebook(
                title: title, folder: Self.unprefixed(folderPath), template: .blank, size: .a4,
                pageCount: 0)
        else { return nil }
        refresh()
        let result = await importPDF(data: data, name: title, into: notebook, after: nil)
        guard result != nil, let imported = try? store.notebook(id: notebook.id),
            !imported.pages.isEmpty
        else {
            _ = try? store.delete(notebookID: notebook.id)
            libraryChanged()
            return nil
        }
        libraryChanged()
        return imported
    }

    /// One picture as a one-page PDF whose long edge is 1,024 points, which
    /// is how Image and Take Photo become a document.
    nonisolated static func pdfDocument(fromImage image: UIImage) -> Data? {
        let pixels = CGSize(
            width: image.size.width * image.scale, height: image.size.height * image.scale)
        let longEdge = max(pixels.width, pixels.height)
        guard longEdge >= 1 else { return nil }
        let factor = 1024 / longEdge
        let page = CGRect(
            x: 0, y: 0, width: max(1, (pixels.width * factor).rounded()),
            height: max(1, (pixels.height * factor).rounded()))
        return UIGraphicsPDFRenderer(bounds: page).pdfData { context in
            context.beginPage()
            image.draw(in: page)
        }
    }

    // MARK: Changing

    /// Renames a folder or a document; an empty or unchanged name does
    /// nothing.
    func rename(_ item: PaperCore.LibraryItem, to name: String) {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, trimmed != item.name else { return }
        let resolved = NewDocumentTitle.resolved(trimmed)
        autosave.flushAll()
        switch item {
        case .document(let notebook):
            let renamed = try? store.rename(notebookID: notebook.id, to: resolved)
            libraryChanged(Set([notebook.path, renamed?.path].compactMap { $0 }))
        case .folder(let folder):
            guard let record = record(for: folder) else { return }
            let inside = documentPaths(inside: record.path)
            _ = try? store.renameFolder(id: record.id, to: resolved)
            libraryChanged(inside)
        }
    }

    func setFavorite(_ item: PaperCore.LibraryItem, _ favorite: Bool) {
        switch item {
        case .document(let notebook):
            _ = try? store.setFavorite(notebookID: notebook.id, favorite)
        case .folder(let folder):
            guard let record = record(for: folder) else { return }
            _ = try? store.setFolderFavorite(id: record.id, favorite)
        }
        libraryChanged()
    }

    func setFolderColor(_ folder: PaperFolder, _ color: FolderColor) {
        guard let record = record(for: folder) else { return }
        _ = try? store.setFolderAppearance(id: record.id, color: color, icon: nil)
        libraryChanged()
    }

    /// `nil` takes the icon away.
    func setFolderIcon(_ folder: PaperFolder, _ icon: FolderIcon?) {
        guard let record = record(for: folder) else { return }
        _ = try? store.setFolderAppearance(id: record.id, color: nil, icon: .some(icon))
        libraryChanged()
    }

    /// A document's tags, from its Info.
    func setTags(_ notebook: Notebook, _ tags: [String]) {
        _ = try? store.setTags(notebookID: notebook.id, tags)
        libraryChanged()
    }

    /// "<title> copy", beside the original. Folders are not duplicated.
    @discardableResult
    func duplicate(_ item: PaperCore.LibraryItem) -> Notebook? {
        guard case .document(let notebook) = item else { return nil }
        autosave.flushAll()
        let copy = try? store.duplicate(notebookID: notebook.id)
        libraryChanged()
        return copy
    }

    /// Into the device trash, for the undo toast. Nil when nothing moved.
    func trash(_ item: PaperCore.LibraryItem) -> TrashToken? {
        autosave.flushAll()
        let token: TrashToken?
        switch item {
        case .document(let notebook):
            token = try? store.delete(notebookID: notebook.id)
        case .folder(let folder):
            token = record(for: folder).flatMap { try? store.deleteFolder(id: $0.id) }
        }
        libraryChanged()
        return token
    }

    /// The undo toast's Undo, for every token one action trashed.
    func restore(_ tokens: [TrashToken]) {
        for token in tokens { _ = try? store.restore(token) }
        libraryChanged()
    }

    /// Whether `items` may go into the folder at `target`: never a folder
    /// into itself or a folder inside it, and never past the 900-byte path
    /// limit for anything they hold.
    nonisolated static func canMove(
        _ items: [PaperCore.LibraryItem], into target: String, notebooks: [Notebook]
    ) -> Bool {
        guard !items.isEmpty else { return false }
        for item in items {
            switch item {
            case .folder(let folder):
                if isInside(target, folder.path) { return false }
                let leaf = folder.path.split(separator: "/").last.map(String.init) ?? folder.name
                let deepest =
                    notebooks.filter { isInside($0.path, folder.path) }
                    .map { $0.path.utf8.count - folder.path.utf8.count }.max() ?? 0
                if target.utf8.count + 1 + leaf.utf8.count + deepest > maximumPathBytes {
                    return false
                }
            case .document(let notebook):
                let leaf = notebook.path.split(separator: "/").last.map(String.init) ?? ""
                if target.utf8.count + 1 + leaf.utf8.count > maximumPathBytes { return false }
            }
        }
        return true
    }

    /// Every item already sits in `target`, so Move would do nothing.
    nonisolated static func isAlreadyIn(_ items: [PaperCore.LibraryItem], _ target: String)
        -> Bool
    {
        items.allSatisfy { item in
            switch item {
            case .folder(let folder): return folder.parentPath == target
            case .document(let notebook): return notebook.folderPath == target
            }
        }
    }

    /// The contract's vault path limit, in UTF-8 bytes.
    nonisolated static let maximumPathBytes = 900

    /// Moves folders and documents into the folder at `target`, flushing
    /// first. False when any of them could not go.
    @discardableResult
    func move(_ items: [PaperCore.LibraryItem], toFolderPath target: String) -> Bool {
        guard Self.canMove(items, into: target, notebooks: notebooks) else { return false }
        autosave.flushAll()
        var moved = true
        var changed: Set<String> = []
        for item in items {
            switch item {
            case .document(let notebook):
                guard let current = try? store.notebook(id: notebook.id) else {
                    moved = false
                    continue
                }
                guard current.folderPath != target else { continue }
                if let result = try? store.move(
                    notebookID: notebook.id, toFolder: Self.unprefixed(target))
                {
                    changed.formUnion([current.path, result.path])
                } else {
                    moved = false
                }
            case .folder(let folder):
                guard let record = record(for: folder) else {
                    moved = false
                    continue
                }
                guard record.parentPath != target else { continue }
                let inside = documentPaths(inside: record.path)
                if (try? store.moveFolder(id: record.id, into: target)) != nil {
                    changed.formUnion(inside)
                } else {
                    moved = false
                }
            }
        }
        libraryChanged(changed)
        return moved
    }

    /// The move picker's rows: Documents, then each folder under its parent,
    /// by name, showing the children of every path in `expanded`.
    nonisolated static func folderTree(_ folders: [PaperFolder], expanded: Set<String>)
        -> [FolderTreeNode]
    {
        var children: [String: [PaperFolder]] = [:]
        for folder in folders { children[folder.parentPath, default: []].append(folder) }
        for key in children.keys {
            children[key]?.sort {
                let order = $0.name.localizedStandardCompare($1.name)
                return order == .orderedSame ? $0.path < $1.path : order == .orderedAscending
            }
        }
        var rows = [
            FolderTreeNode(
                folder: nil, path: rootFolderPath, depth: 0,
                hasChildren: !(children[rootFolderPath] ?? []).isEmpty)
        ]
        func visit(_ path: String, depth: Int) {
            guard expanded.contains(path) else { return }
            for child in children[path] ?? [] {
                rows.append(
                    FolderTreeNode(
                        folder: child, path: child.path, depth: depth,
                        hasChildren: !(children[child.path] ?? []).isEmpty))
                visit(child.path, depth: depth + 1)
            }
        }
        visit(rootFolderPath, depth: 1)
        return rows
    }

    // MARK: Search

    /// Case- and diacritic-blind, as the library's search matches names.
    nonisolated static func matches(_ text: String, _ query: String) -> Bool {
        text.range(of: query, options: [.caseInsensitive, .diacriticInsensitive, .widthInsensitive])
            != nil
    }

    /// The five groups: folders and documents by name, page titles, then the
    /// handwriting of notebooks and of whiteboards. Each group newest first,
    /// at most `LibrarySearchResults.groupLimit`.
    nonisolated static func search(
        _ query: String, folders: [PaperFolder], notebooks: [Notebook], outline: [OutlineEntry],
        handwriting: [NotebookMatches]
    ) -> LibrarySearchResults {
        let query = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty else { return LibrarySearchResults() }
        let limit = LibrarySearchResults.groupLimit
        var results = LibrarySearchResults()
        results.folders = Array(
            folders.filter { matches($0.name, query) }
                .sorted { $0.modified != $1.modified ? $0.modified > $1.modified : $0.path < $1.path }
                .prefix(limit))
        results.documents = Array(
            notebooks.filter { matches($0.title, query) }
                .sorted { $0.modified != $1.modified ? $0.modified > $1.modified : $0.path < $1.path }
                .prefix(limit))
        results.outline = Array(
            outline.filter { matches($0.pageTitle, query) }
                .sorted {
                    $0.modified != $1.modified
                        ? $0.modified > $1.modified : ($0.notebookPath, $0.pageNumber)
                            < ($1.notebookPath, $1.pageNumber)
                }
                .prefix(limit))
        let kinds = Dictionary(
            notebooks.map { ($0.path, $0.kind) }, uniquingKeysWith: { first, _ in first })
        // The index lists the newest documents first.
        for group in handwriting where !group.pages.isEmpty {
            if kinds[group.notebookPath] == .whiteboard {
                results.whiteboards.append(contentsOf: group.pages)
            } else {
                results.notebooks.append(contentsOf: group.pages)
            }
        }
        results.notebooks = Array(results.notebooks.prefix(limit))
        results.whiteboards = Array(results.whiteboards.prefix(limit))
        return results
    }

    func librarySearch(_ query: String, outline: [OutlineEntry]) -> LibrarySearchResults {
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return LibrarySearchResults() }
        return Self.search(
            trimmed, folders: libraryFolders, notebooks: notebooks, outline: outline,
            handwriting: store.textIndex.search(trimmed, limit: 400))
    }

    /// Every titled page, read off the main actor once per search.
    func outlineEntries() async -> [OutlineEntry] {
        let store = store
        let notebooks = notebooks
        return await Task.detached(priority: .userInitiated) {
            var entries: [OutlineEntry] = []
            for notebook in notebooks {
                guard let pages = try? store.pages(ofNotebook: notebook.id) else { continue }
                for (index, page) in pages.enumerated() {
                    let title = (page.title ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
                    guard !title.isEmpty else { continue }
                    entries.append(
                        OutlineEntry(
                            notebookID: notebook.id, notebookPath: notebook.path,
                            notebookTitle: notebook.title, pageID: page.id, pageTitle: title,
                            pageNumber: index + 1, kind: notebook.kind, modified: notebook.modified))
                }
            }
            return entries
        }.value
    }

    // MARK: Thumbnails

    /// The document's first page as the library draws it, read and decoded
    /// off the main actor and kept until that page renders again.
    func cover(for notebook: Notebook) async -> LibraryCover {
        let key = coverKey(for: notebook)
        if let cached = coverCache.object(forKey: key) { return cached }
        let store = store
        let cover = await Task.detached(priority: .utility) {
            Self.loadCover(store: store, notebook: notebook)
        }.value
        coverCache.setObject(cover, forKey: key)
        return cover
    }

    /// Changes when the first page, the document or its render does.
    func coverKey(for notebook: Notebook) -> NSString {
        let first = notebook.pages.first ?? ""
        let stamp = coverStamps[first] ?? 0
        return "\(notebook.id)|\(first)|\(notebook.modified.timeIntervalSince1970)|\(stamp)"
            as NSString
    }

    /// The shape a document's thumbnail takes before its first page is read:
    /// a board's 4:3, or the notebook's paper turned its way.
    nonisolated static func defaultAspect(for notebook: Notebook) -> CGFloat {
        if notebook.kind == .whiteboard || notebook.paperSize == .board { return 1366.0 / 1024.0 }
        let size =
            notebook.paperSize == .infinite
            ? PaperSize.iPad.points : notebook.paperSize.points(in: notebook.orientation)
        guard size.width > 0, size.height > 0 else { return 0.75 }
        return clampedAspect(size.width / size.height)
    }

    /// A grown infinite page can be any shape; a thumbnail stays readable.
    nonisolated static func clampedAspect(_ aspect: CGFloat) -> CGFloat {
        guard aspect.isFinite, aspect > 0 else { return 0.75 }
        return min(max(aspect, 0.5), 2)
    }

    nonisolated static func loadCover(store: PaperStore, notebook: Notebook) -> LibraryCover {
        let isWhiteboard = notebook.kind == .whiteboard
        guard let page = (try? store.coverPage(ofNotebook: notebook.id)) ?? nil else {
            return LibraryCover(
                image: nil, aspect: defaultAspect(for: notebook), template: notebook.template,
                paperColor: notebook.paperColor, isBoard: isWhiteboard)
        }
        let isBoard = page.size == .board
        let aspect: CGFloat
        if isBoard {
            if let rect = page.renderRect, rect.width > 0, rect.height > 0 {
                aspect = clampedAspect(CGFloat(rect.width / rect.height))
            } else {
                aspect = 1366.0 / 1024.0
            }
        } else {
            let size = page.pointSize
            aspect = size.height > 0 ? clampedAspect(size.width / size.height) : 0.75
        }
        return LibraryCover(
            image: thumbnail(store: store, notebookPath: notebook.path, pageID: page.id),
            aspect: aspect, template: page.template, paperColor: page.paperColor, isBoard: isBoard)
    }

    /// The renderer's small cover JPEG when it is at least as new as the
    /// render; otherwise the render itself, decoded small. A page pulled from
    /// another device has a render and no JPEG.
    private nonisolated static func thumbnail(store: PaperStore, notebookPath: String, pageID: String)
        -> UIImage?
    {
        let manager = FileManager.default
        func modified(_ url: URL) -> Date? {
            (try? manager.attributesOfItem(atPath: url.path(percentEncoded: false)))?[
                .modificationDate] as? Date
        }
        let jpeg = PageRenderer.coverURL(pageID: pageID)
        let render = store.paths.render(notebookPath, pageID)
        let jpegDate = modified(jpeg)
        let renderDate = modified(render)
        if let jpegDate, renderDate.map({ jpegDate >= $0 }) ?? true,
            let image = UIImage(contentsOfFile: jpeg.path(percentEncoded: false))
        {
            return image
        }
        guard renderDate != nil,
            let source = CGImageSourceCreateWithURL(render as CFURL, nil),
            let cgImage = CGImageSourceCreateThumbnailAtIndex(
                source, 0,
                [
                    kCGImageSourceCreateThumbnailFromImageAlways: true,
                    kCGImageSourceThumbnailMaxPixelSize: 400,
                    kCGImageSourceCreateThumbnailWithTransform: true,
                ] as CFDictionary)
        else { return nil }
        return UIImage(cgImage: cgImage)
    }

    // MARK: DEBUG

    #if DEBUG
        /// `-debugFolders` (DEBUG only): the folders of the owner's screenshot
        /// of the reference library -- Management in orange, Accounting in
        /// red, Econ in blue, a whiteboard at the top level and a dozen inside
        /// Accounting -- once per store, so the list can be laid over it.
        func seedDebugFoldersIfRequested() {
            guard CommandLine.arguments.contains("-debugFolders") else { return }
            refresh()
            guard libraryFolder(path: "paper/Accounting") == nil else { return }
            let board = NewDocumentOptions.whiteboardFactory
            _ = try? store.createFolder(name: "Econ", in: Self.rootFolderPath, color: .blue)
            guard
                let accounting = try? store.createFolder(
                    name: "Accounting", in: Self.rootFolderPath, color: .red)
            else { return }
            _ = try? store.createFolder(name: "Management", in: Self.rootFolderPath, color: .orange)
            // Oldest first, so the date sort lists them as the screenshot does.
            let inside = [
                "Chapter 2", "Chapter 4", "Review Sept.22", "Sept.24", "Excel2", "Chapter3",
                "Chapter 3 hw", "Chapter 3 problem set", "Midterm Chapter 1", "Midterm Chapter 2",
                "Midterm Chapter 4", "Practice Test",
            ]
            var made: [Notebook] = []
            for title in inside {
                if let notebook = try? store.createDocument(
                    title: title, folderPath: accounting.path, kind: .whiteboard, options: board)
                {
                    made.append(notebook)
                }
            }
            _ = try? store.createDocument(
                title: "Homework6", folderPath: Self.rootFolderPath, kind: .whiteboard,
                options: board)
            // Something for Recents, which lists what this device opened.
            for notebook in made.suffix(3) { try? store.markOpened(notebookID: notebook.id) }
            // Sent now, with a server, rather than at the next launch.
            libraryChanged()
        }
    #endif
}
