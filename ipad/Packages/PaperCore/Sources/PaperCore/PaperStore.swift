import Foundation
import GRDB

public enum PaperStoreError: Error, Equatable, Sendable {
    case notFound(String)
    case unsafePath(String)
    case titleInUse(String)
}

/// Handle onto something in the local trash, held by the undo toast for its six
/// seconds. Restoring is a move back, never a re-creation, so ids and ink
/// survive intact.
public struct TrashToken: Equatable, Sendable {
    public enum Kind: String, Sendable {
        case notebook
        case page
        /// A folder with everything in it. `restore(_:)` puts it back and
        /// returns nil: there is no one notebook to hand back.
        case folder
    }

    public let id: String
    public let kind: Kind
    /// What the toast says it deleted.
    public let title: String
}

/// Everything Paper knows about notebooks and pages on this device.
///
/// The file tree is the source of truth and SQLite is an index over it: every
/// mutation writes the JSON first and the row second, and `reconcile()` rebuilds
/// the rows from the tree on launch. That ordering is what makes a crash
/// mid-operation lose at most a row, never ink.
///
/// Deliberately synchronous and `Sendable` rather than an actor: the writes are
/// a few kilobytes of JSON plus one SQLite transaction, the callers are a
/// debounced autosave and direct user actions, and a synchronous store makes
/// "the stroke is on disk before the pill says saved" a fact rather than a race.
public final class PaperStore: Sendable {
    public let paths: PaperPaths
    public let database: PaperDatabase
    public let journal: OpJournal
    /// Held across every read-modify-write of `page.json`, `notebook.json`
    /// and `text.json`. Synchronous is not single-threaded: autosave and the
    /// user's edits write from the main actor while the sync engine records
    /// revisions and applies pulls from its own executor, and two writers that
    /// each read the file, change a field and write it back lose whichever
    /// change landed first. Recursive, so a locked method can call another --
    /// `updatePage` into `save(page:)`. Pure reads never take it.
    let writeLock = NSRecursiveLock()
    /// One Convert to Whiteboard per notebook at a time, whoever asks: two
    /// windows, a double tap, the launch finishing one a kill left.
    let conversions = ConversionGate()

    #if DEBUG
    /// Runs inside `updatePage`, `createPage` and `indexNotebook` between the
    /// read and the write, so a test can put a second writer in exactly that
    /// window.
    nonisolated(unsafe) var testHookBetweenReadAndWrite: (@Sendable () -> Void)?
    /// Runs in `convertToWhiteboard` once every board is built, before the
    /// lock is taken for the swaps.
    nonisolated(unsafe) var testHookBeforeConversionLock: (@Sendable () -> Void)?
    /// Makes `convertToWhiteboard`'s directory swap fail, as on a volume
    /// without `RENAME_SWAP`.
    nonisolated(unsafe) var testSwapFails = false
    /// Runs in `convertToWhiteboard` after each page is swapped in, under
    /// the lock, with the page's position.
    nonisolated(unsafe) var testHookAfterConversionSwap: (@Sendable (Int) -> Void)?
    /// Runs as `convertToWhiteboard` starts, once it has cleared what an
    /// earlier run staged.
    nonisolated(unsafe) var testHookConversionStarted: (@Sendable () -> Void)?
    #endif

    public init(paths: PaperPaths, database: PaperDatabase) throws {
        self.paths = paths
        self.database = database
        self.journal = OpJournal(database: database)
        try FileManager.default.createDirectory(at: paths.paper, withIntermediateDirectories: true)
    }

    public convenience init(root: URL) throws {
        let paths = PaperPaths(root: root)
        try self.init(paths: paths, database: try PaperDatabase(at: paths.database))
    }

    // MARK: - Reading

    /// Every notebook, newest activity first.
    public func notebooks() throws -> [Notebook] {
        try database.queue.read { db in
            try Row.fetchAll(db, sql: "SELECT * FROM notebooks ORDER BY modified DESC")
                .map { try Self.notebook(from: $0, in: db) }
        }
    }

    public func notebook(id: String) throws -> Notebook {
        try database.queue.read { db in
            guard
                let row = try Row.fetchOne(
                    db, sql: "SELECT * FROM notebooks WHERE id = ?", arguments: [id])
            else { throw PaperStoreError.notFound(id) }
            return try Self.notebook(from: row, in: db)
        }
    }

    public func notebook(path: String) throws -> Notebook? {
        try database.queue.read { db in
            guard
                let row = try Row.fetchOne(
                    db, sql: "SELECT * FROM notebooks WHERE path = ?", arguments: [path])
            else { return nil }
            return try Self.notebook(from: row, in: db)
        }
    }

    /// Distinct folder paths in use, sorted. `""` (the top level) is excluded.
    public func folders() throws -> [String] {
        try database.queue.read { db in
            try String.fetchAll(
                db, sql: "SELECT DISTINCT folder FROM notebooks WHERE folder <> '' ORDER BY folder")
        }
    }

    public func tags() throws -> [String] {
        var seen = Set<String>()
        for notebook in try notebooks() {
            for tag in notebook.tags { seen.insert(tag) }
        }
        return seen.sorted()
    }

    public func pages(ofNotebook id: String) throws -> [Page] {
        let path = try notebook(id: id).path
        return try database.queue.read { db in
            try String.fetchAll(
                db, sql: "SELECT id FROM pages WHERE notebook_id = ? ORDER BY position",
                arguments: [id])
        }
        .compactMap { pageID in
            try? AtomicFile.readJSON(Page.self, at: paths.pageJSON(path, pageID))
        }
    }

    /// The page whose render is the document's thumbnail: always the first,
    /// as on the server, whatever an older build pinned in `cover`. A cover
    /// page is page 1, and a whiteboard's cover is board 1.
    public func coverPage(ofNotebook id: String) throws -> Page? {
        let notebook = try notebook(id: id)
        for pageID in notebook.pages {
            // A page whose files are missing is skipped, as `pages(ofNotebook:)`
            // skips it, rather than leaving the document without a thumbnail.
            if let page = try? page(notebookPath: notebook.path, id: pageID) { return page }
        }
        return nil
    }

    public func page(notebookPath: String, id: String) throws -> Page {
        guard let page = try? AtomicFile.readJSON(Page.self, at: paths.pageJSON(notebookPath, id))
        else { throw PaperStoreError.notFound(id) }
        return page
    }

    /// Raw `PKDrawing.dataRepresentation()` bytes, or `nil` for a blank page.
    public func drawingData(notebookPath: String, pageID: String) -> Data? {
        try? Data(contentsOf: paths.drawing(notebookPath, pageID))
    }

    // MARK: - Notebooks

    public func createNotebook(
        title: String,
        folder: String = "",
        template: Template = .ruled,
        size: PaperSize = .a4,
        pageCount: Int = 1
    ) throws -> Notebook {
        let path = try uniqueNotebookPath(folder: folder, title: title)
        var notebook = Notebook(
            title: title, template: template, paperSize: size, path: path)

        // Folders made on the way get their records, as the server makes
        // them for the same create.
        try ensureFolderRecords(forAncestorsOf: path)
        try FileManager.default.createDirectory(
            at: paths.url(forVaultPath: path), withIntermediateDirectories: true)

        var created: [Page] = []
        // Zero is honoured: `duplicate` fills the package itself, and a blank
        // page made here would outlive it on disk and come back on reconcile.
        for _ in 0..<max(0, pageCount) {
            let page = Page(template: template, size: size)
            try AtomicFile.writeJSON(page, to: paths.pageJSON(path, page.id))
            created.append(page)
        }
        notebook.pages = created.map(\.id)
        // The cover is always page 1 (`coverPage`); nothing is pinned.
        notebook.cover = nil
        try writeNotebookJSON(notebook)

        try database.queue.write { db in
            try Self.upsertNotebookRow(notebook, in: db)
            for (index, page) in created.enumerated() {
                try Self.upsertPageRow(page, notebookID: notebook.id, position: index, in: db)
            }
        }

        try journal.append(.init(kind: .notebookCreate, notebookPath: path))
        for page in created {
            try journal.append(.init(kind: .pageCreate, notebookPath: path, pageID: page.id))
        }
        return notebook
    }

    /// Creates a notebook or a whiteboard the way the New sheets do, in the
    /// folder at `folderPath` (`paper` for the top level, `paper/Work` for a
    /// folder).
    ///
    /// - A notebook gets one page of `options`' paper, colour, size and
    ///   orientation -- landscape stored as the preset's sides swapped into
    ///   `size_points` -- preceded by a cover page when `options.cover` is on.
    /// - A whiteboard gets one board, "Board 1", at the default extent, with
    ///   `options`' background and colour; its notebook is `.board`-sized.
    ///
    /// `options` are held to what the sheet of that kind offers first
    /// (`NewDocumentOptions.sanitized(for:)`). Each page is journalled as a
    /// create-PUT, so its colour, cover template, extent and title reach the
    /// server with it; the document's kind, orientation and colour ride the
    /// notebook create, which reads them from this notebook.
    public func createDocument(
        title: String, folderPath: String, kind: DocumentKind, options: NewDocumentOptions
    ) throws -> Notebook {
        let options = options.sanitized(for: kind)
        let trimmed = title.trimmingCharacters(in: .whitespacesAndNewlines)
        let title = trimmed.isEmpty ? "Untitled" : trimmed
        let path = try uniqueNotebookPath(inFolderPath: folderPath, title: title)
        var notebook = Notebook(
            title: title, template: options.template, paperSize: options.size, kind: kind,
            orientation: kind == .notebook ? options.orientation : .portrait,
            paperColor: options.paperColor, path: path)

        var pages: [Page] = []
        switch kind {
        case .notebook:
            if options.cover {
                pages.append(
                    sheet(template: .cover, color: options.coverColor, in: notebook))
            }
            pages.append(sheet(template: options.template, color: options.paperColor, in: notebook))
        case .whiteboard:
            pages.append(
                board(template: options.template, color: options.paperColor, title: "Board 1"))
        }

        try FileManager.default.createDirectory(
            at: paths.url(forVaultPath: path), withIntermediateDirectories: true)
        for page in pages {
            try AtomicFile.writeJSON(page, to: paths.pageJSON(path, page.id))
        }
        notebook.pages = pages.map(\.id)
        try writeNotebookJSON(notebook)

        let snapshot = notebook
        try database.queue.write { db in
            try Self.upsertNotebookRow(snapshot, in: db)
            for (index, page) in pages.enumerated() {
                try Self.upsertPageRow(page, notebookID: snapshot.id, position: index, in: db)
            }
        }

        try journal.append(.init(kind: .notebookCreate, notebookPath: path))
        var previous: String?
        for page in pages {
            try journal.append(.creatingPage(notebookPath: path, pageID: page.id, after: previous))
            previous = page.id
        }
        return notebook
    }

    /// A new sheet for `notebook`: its size turned to its orientation, with
    /// the turned sides in `size_points` when they differ from the preset.
    private func sheet(template: Template, color: PaperColor, in notebook: Notebook) -> Page {
        let size = notebook.paperSize
        let turned = size.points(in: notebook.orientation)
        return Page(
            template: template, size: size,
            customSize: turned == size.points ? nil : SizePoints(turned), paperColor: color)
    }

    /// A new board at the default extent. Boards never carry `size_points`.
    private func board(template: Template, color: PaperColor, title: String) -> Page {
        Page(
            title: title, template: template, size: .board, paperColor: color,
            boardExtent: SizePoints(PaperSize.board.points))
    }

    /// Where a new package for `title` goes inside the folder at `folderPath`.
    /// The folder's directory names are taken as they are on disk -- they are
    /// already slugs -- and only the title is slugged. Anything not under the
    /// `paper` root is refused rather than guessed at.
    private func uniqueNotebookPath(inFolderPath folderPath: String, title: String) throws
        -> String
    {
        let parent = folderPath.hasSuffix("/") ? String(folderPath.dropLast()) : folderPath
        guard parent == "paper" || parent.hasPrefix("paper/") else {
            throw PaperStoreError.unsafePath(folderPath)
        }
        return try uniquePackagePath(
            base: "\(parent)/\(PaperPaths.slug(title)).paper", title: title)
    }

    public func rename(notebookID: String, to title: String) throws -> Notebook {
        writeLock.lock()
        defer { writeLock.unlock() }
        var notebook = try notebook(id: notebookID)
        let oldPath = notebook.path
        let newPath = try uniqueNotebookPath(
            folder: notebook.folder, title: title, excluding: notebook.id)
        notebook.title = title
        notebook.modified = Date()
        try move(notebook: &notebook, from: oldPath, to: newPath)
        try journalMove(
            .init(
                kind: .notebookRename, notebookPath: oldPath, notebookID: notebook.id,
                payload: ["new_path": newPath, "title": title]),
            to: newPath)
        return notebook
    }

    public func move(notebookID: String, toFolder folder: String) throws -> Notebook {
        writeLock.lock()
        defer { writeLock.unlock() }
        var notebook = try notebook(id: notebookID)
        let oldPath = notebook.path
        let newPath = try uniqueNotebookPath(
            folder: folder, title: notebook.title, excluding: notebook.id)
        notebook.modified = Date()
        try move(notebook: &notebook, from: oldPath, to: newPath)
        try journalMove(
            .init(
                kind: .notebookMove, notebookPath: oldPath, notebookID: notebook.id,
                payload: ["new_path": newPath]),
            to: newPath)
        return notebook
    }

    /// Queues a rename or move -- unless the server has never heard of the
    /// notebook. Then every op still queued for it is re-pointed at the new
    /// path and the create lands there: a rename op would find nothing to
    /// rename, queued behind a create that went up under the old name.
    private func journalMove(_ op: PaperOp, to newPath: String) throws {
        if try journal.rewritePath(
            from: op.notebookPath, to: newPath, notebookID: op.notebookID)
        {
            return
        }
        try journal.append(op)
    }

    public func setFavorite(notebookID: String, _ favorite: Bool) throws -> Notebook {
        writeLock.lock()
        defer { writeLock.unlock() }
        var notebook = try notebook(id: notebookID)
        notebook.favorite = favorite
        notebook.modified = Date()
        try writeNotebookJSON(notebook)
        try database.queue.write { db in try Self.upsertNotebookRow(notebook, in: db) }
        try journal.append(
            .init(
                kind: .notebookPatch, notebookPath: notebook.path,
                payload: ["favorite": favorite ? "true" : "false"]))
        return notebook
    }

    public func setTags(notebookID: String, _ tags: [String]) throws -> Notebook {
        writeLock.lock()
        defer { writeLock.unlock() }
        var notebook = try notebook(id: notebookID)
        notebook.tags = tags
        notebook.modified = Date()
        try writeNotebookJSON(notebook)
        try database.queue.write { db in try Self.upsertNotebookRow(notebook, in: db) }
        try journal.append(
            .init(
                kind: .notebookPatch, notebookPath: notebook.path,
                payload: ["tags": tags.joined(separator: ",")]))
        return notebook
    }

    /// Updates the notebook metadata fields that are safe for a client to
    /// edit in one operation. A `nil` argument leaves that field unchanged.
    /// `template`, `paperSize`, `orientation` and `paperColor` are what new
    /// pages get; the pages already there keep their own.
    @discardableResult
    public func updateNotebookMetadata(
        notebookID: String,
        tags: [String]? = nil,
        cover: String? = nil,
        template: Template? = nil,
        paperSize: PaperSize? = nil,
        orientation: PageOrientation? = nil,
        paperColor: PaperColor? = nil
    ) throws -> Notebook {
        writeLock.lock()
        defer { writeLock.unlock() }
        var notebook = try notebook(id: notebookID)
        if let tags { notebook.tags = tags }
        if let cover {
            guard notebook.pages.contains(cover) else { throw PaperStoreError.notFound(cover) }
            notebook.cover = cover
        }
        if let template { notebook.template = template }
        if let paperSize { notebook.paperSize = paperSize }
        if let orientation { notebook.orientation = orientation }
        if let paperColor { notebook.paperColor = paperColor }
        notebook.modified = Date()
        try writeNotebookJSON(notebook)
        try database.queue.write { db in try Self.upsertNotebookRow(notebook, in: db) }
        var payload: [String: String] = [:]
        if let tags { payload["tags"] = tags.joined(separator: ",") }
        if let cover { payload["cover"] = cover }
        if let template { payload["template"] = template.rawValue }
        if let paperSize { payload["paperSize"] = paperSize.rawValue }
        if let orientation { payload["orientation"] = orientation.rawValue }
        if let paperColor { payload["paperColor"] = paperColor.rawValue }
        if !payload.isEmpty {
            try journal.append(
                .init(kind: .notebookPatch, notebookPath: notebook.path, payload: payload))
        }
        return notebook
    }

    /// Copies the package with fresh ids, so the duplicate is a new notebook
    /// rather than a second name for the same one.
    ///
    /// The assets come along and the copied pages point at the copies, so the
    /// duplicate does not lose its images and PDF pages when the source is
    /// renamed or deleted. Each page is journalled as a create-PUT, which takes
    /// its ink, objects and PDF binding to the server rather than a blank page.
    public func duplicate(notebookID: String) throws -> Notebook {
        let source = try notebook(id: notebookID)
        let sourcePages = try pages(ofNotebook: notebookID)
        var copy = try createNotebook(
            title: duplicateTitle(of: source.title), folder: source.folder,
            template: source.template, size: source.paperSize, pageCount: 0)
        // A whiteboard's copy is a whiteboard; `writeOrder` below writes these.
        copy.kind = source.kind
        copy.orientation = source.orientation
        copy.paperColor = source.paperColor

        let sourceAssets = assets.directory(forNotebook: source.path)
        if FileManager.default.fileExists(atPath: sourceAssets.path(percentEncoded: false)) {
            try FileManager.default.copyItem(
                at: sourceAssets, to: assets.directory(forNotebook: copy.path))
        }
        // The copies are new files to the server too: the pages below name
        // them, so they go up after the create and ahead of those pages.
        try journalAssetUploads(of: copy)
        let sourcePrefix = "\(source.path)/assets/"
        let copyPrefix = "\(copy.path)/assets/"
        func relocated(_ assetPath: String) -> String {
            guard assetPath.hasPrefix(sourcePrefix) else { return assetPath }
            return copyPrefix + assetPath.dropFirst(sourcePrefix.count)
        }

        var newPageIDs: [String] = []
        for (index, page) in sourcePages.enumerated() {
            var duplicated = page
            duplicated.id = PaperID.make()
            duplicated.revision = 0
            duplicated.renderRevision = 0
            duplicated.textRevision = 0
            duplicated.created = Date()
            duplicated.modified = Date()
            if let asset = page.pdf?.asset { duplicated.pdf?.asset = relocated(asset) }
            duplicated.objects = page.objects.map { object in
                var object = object
                object.asset = object.asset.map(relocated)
                return object
            }
            try AtomicFile.writeJSON(duplicated, to: paths.pageJSON(copy.path, duplicated.id))
            if let ink = drawingData(notebookPath: source.path, pageID: page.id) {
                try AtomicFile.write(ink, to: paths.drawing(copy.path, duplicated.id))
            }
            // Derived, but it rides the create-PUT, so the web viewer shows the
            // copy as soon as it lands rather than after the next edit.
            if let render = renderData(notebookPath: source.path, pageID: page.id) {
                try AtomicFile.write(render, to: paths.render(copy.path, duplicated.id))
            }
            try database.queue.write { db in
                try Self.upsertPageRow(duplicated, notebookID: copy.id, position: index, in: db)
            }
            try journal.append(
                .creatingPage(
                    notebookPath: copy.path, pageID: duplicated.id, after: newPageIDs.last))
            newPageIDs.append(duplicated.id)
        }

        try writeOrder(newPageIDs, for: &copy)
        return copy
    }

    /// Moves the whole package into the device trash and returns the token the
    /// undo toast holds. Nothing is unlinked: undo is a move back.
    public func delete(notebookID: String) throws -> TrashToken {
        let notebook = try notebook(id: notebookID)
        let token = TrashToken(id: PaperID.make(), kind: .notebook, title: notebook.title)
        let destination = paths.trash.appending(path: token.id, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: destination, withIntermediateDirectories: true)
        // The delete drops every unpushed op for the notebook; the trash keeps
        // them so the undo can queue the same work again.
        let dropped = try journal.unpushed(notebookPath: notebook.path, notebookID: notebook.id)
        try AtomicFile.writeJSON(
            TrashManifest(
                kind: .notebook, notebookPath: notebook.path, notebookID: notebook.id,
                pageID: nil, position: nil, deletedAt: Date(), droppedOps: dropped),
            to: destination.appending(path: "manifest.json", directoryHint: .notDirectory))
        try FileManager.default.moveItem(
            at: paths.url(forVaultPath: notebook.path),
            to: destination.appending(path: "package", directoryHint: .isDirectory))

        try database.queue.write { db in
            // Before the notebook row: its pages cascade away with it, and
            // `page_text` has no key to follow them, so this is the last
            // moment the index can still tell which transcripts were its.
            let pages = "SELECT id FROM pages WHERE notebook_id = ?"
            try db.execute(
                sql: "DELETE FROM page_fts WHERE page_id IN (\(pages))", arguments: [notebookID])
            try db.execute(
                sql: "DELETE FROM page_text WHERE page_id IN (\(pages))", arguments: [notebookID])
            try db.execute(
                sql: "DELETE FROM notebooks WHERE id = ?", arguments: [notebookID])
        }
        // The row is gone already, so the op names the id itself.
        try journal.append(
            .init(kind: .notebookDelete, notebookPath: notebook.path, notebookID: notebook.id))
        return token
    }

    /// Undo for either kind of delete.
    @discardableResult
    public func restore(_ token: TrashToken) throws -> Notebook? {
        let directory = paths.trash.appending(path: token.id, directoryHint: .isDirectory)
        let manifest = try AtomicFile.readJSON(
            TrashManifest.self,
            at: directory.appending(path: "manifest.json", directoryHint: .notDirectory))

        switch manifest.kind {
        case .notebook:
            let destination = paths.url(forVaultPath: manifest.notebookPath)
            try FileManager.default.createDirectory(
                at: destination.deletingLastPathComponent(), withIntermediateDirectories: true)
            if FileManager.default.fileExists(atPath: destination.path(percentEncoded: false)) {
                try FileManager.default.removeItem(at: destination)
            }
            try FileManager.default.moveItem(
                at: directory.appending(path: "package", directoryHint: .isDirectory),
                to: destination)
            try? FileManager.default.removeItem(at: directory)
            let deleteNeverLeft = try journal.cancelUnpushed(
                kind: .notebookDelete, notebookPath: manifest.notebookPath)
            try indexNotebook(at: manifest.notebookPath)
            guard let notebook = try notebook(path: manifest.notebookPath) else { return nil }
            let dropped = manifest.droppedOps ?? []
            if deleteNeverLeft || dropped.contains(where: { $0.kind == .notebookCreate }) {
                // The server never saw the delete, or never saw the notebook:
                // the work the delete dropped is owed exactly as it was.
                for op in dropped { try journal.append(op) }
            } else {
                try requeueAsNew(notebook)
            }
            return notebook

        case .page:
            guard let pageID = manifest.pageID else { throw PaperStoreError.notFound(token.id) }
            let destination = paths.pageDirectory(manifest.notebookPath, pageID)
            try FileManager.default.createDirectory(
                at: destination.deletingLastPathComponent(), withIntermediateDirectories: true)
            if FileManager.default.fileExists(atPath: destination.path(percentEncoded: false)) {
                try FileManager.default.removeItem(at: destination)
            }
            try FileManager.default.moveItem(
                at: directory.appending(path: "page", directoryHint: .isDirectory), to: destination)
            try? FileManager.default.removeItem(at: directory)
            let deleteNeverLeft = try journal.cancelUnpushed(
                kind: .pageDelete, notebookPath: manifest.notebookPath, pageID: pageID)

            writeLock.lock()
            defer { writeLock.unlock() }
            guard var notebook = try notebook(path: manifest.notebookPath) else { return nil }
            var order = notebook.pages
            let position = min(manifest.position ?? order.count, order.count)
            order.insert(pageID, at: position)
            let dropped = manifest.droppedOps ?? []
            // `append` drops a page's delete along with its unpushed create, so
            // a create among the dropped ops means the server never saw either.
            let createDropped = dropped.contains { $0.kind == .pageCreate || $0.createsPage }
            if deleteNeverLeft || createDropped {
                for op in dropped { try journal.append(op) }
            } else {
                try requeueAsNew(
                    pageID: pageID, after: position > 0 ? order[position - 1] : nil, in: notebook)
            }
            if let page = try? AtomicFile.readJSON(
                Page.self, at: paths.pageJSON(manifest.notebookPath, pageID))
            {
                let notebookID = notebook.id
                try database.queue.write { db in
                    try Self.upsertPageRow(
                        page, notebookID: notebookID, position: manifest.position ?? 0, in: db)
                }
                // The delete took it out of search; its text.json came back
                // with it.
                indexPageText(
                    pageID: pageID, notebookPath: manifest.notebookPath,
                    textRevision: page.textRevision)
            }
            notebook.pages = order
            try writeOrder(order, for: &notebook)
            return notebook

        case .folder:
            try restoreFolder(manifest, from: directory)
            return nil
        }
    }

    /// The undo toast's fallback once the delete has reached the server, which
    /// holds the notebook in its trash now. Restoring it from there
    /// (`PaperController.restoreFromServerTrash`) keeps its history and is the
    /// better path; this one has only the files on this device, so it sends
    /// them up as new: the create (idempotent), every page as a PUT at base
    /// revision 0, and the metadata the create does not carry.
    func requeueAsNew(_ notebook: Notebook) throws {
        try journal.append(
            .init(kind: .notebookCreate, notebookPath: notebook.path, notebookID: notebook.id))
        // The server's copy of `assets/` went to its trash with the package.
        try journalAssetUploads(of: notebook)
        var previous: String?
        for pageID in notebook.pages {
            try requeueAsNew(pageID: pageID, after: previous, in: notebook)
            previous = pageID
        }
        var payload = [
            "favorite": notebook.favorite ? "true" : "false",
            "tags": notebook.tags.joined(separator: ","),
        ]
        if let cover = notebook.cover { payload["cover"] = cover }
        try journal.append(
            .init(
                kind: .notebookPatch, notebookPath: notebook.path, notebookID: notebook.id,
                payload: payload))
    }

    /// One page of the same fallback: the server trashed it, so the revisions
    /// this device holds name nothing upstream any more. Reset to 0, the PUT
    /// is a create that carries the ink, objects and binding in one request.
    private func requeueAsNew(pageID: String, after: String?, in notebook: Notebook) throws {
        try recordServerRevisions(
            notebookPath: notebook.path, pageID: pageID, revision: 0, renderRevision: 0,
            textRevision: 0, seq: 0)
        var op = PaperOp.creatingPage(notebookPath: notebook.path, pageID: pageID, after: after)
        op.notebookID = notebook.id
        try journal.append(op)
    }

    /// Notes that the notebook was opened, which is what "Recents" sorts by.
    public func markOpened(notebookID: String) throws {
        try database.queue.write { db in
            try db.execute(
                sql: "UPDATE notebooks SET opened = ? WHERE id = ?",
                arguments: [Date().timeIntervalSince1970, notebookID])
        }
    }

    /// When each notebook was last opened on this device, by notebook id; a
    /// notebook never opened here is absent. What the library's Recents
    /// orders by (`LibraryArrangement.recents`).
    public func openedDates() throws -> [String: Date] {
        try database.queue.read { db in
            var dates: [String: Date] = [:]
            for row in try Row.fetchAll(
                db, sql: "SELECT id, opened FROM notebooks WHERE opened IS NOT NULL")
            {
                guard let id: String = row["id"], let opened: Double = row["opened"] else { continue }
                dates[id] = Date(timeIntervalSince1970: opened)
            }
            return dates
        }
    }

    public func recentlyOpened(limit: Int = 12) throws -> [Notebook] {
        try database.queue.read { db in
            try Row.fetchAll(
                db,
                sql: """
                    SELECT * FROM notebooks
                    ORDER BY COALESCE(opened, modified) DESC LIMIT ?
                    """,
                arguments: [limit]
            ).map { try Self.notebook(from: $0, in: db) }
        }
    }

    // MARK: - Pages

    public func createPage(notebookID: String, after pageID: String? = nil) throws -> Page {
        // The order read here is the one written back: a PDF import appending
        // pages off the main actor must not land in between.
        writeLock.lock()
        defer { writeLock.unlock() }
        var notebook = try notebook(id: notebookID)
        #if DEBUG
        testHookBetweenReadAndWrite?()
        #endif
        // The notebook's template, colour, size and orientation: what "applies
        // to new pages" means. On a whiteboard the new page is a board.
        let template = notebook.template == .cover ? Template.ruled : notebook.template
        let page =
            notebook.kind == .whiteboard || notebook.paperSize == .board
            ? board(
                template: template, color: notebook.paperColor,
                title: try nextBoardTitle(notebookID: notebook.id, count: notebook.pages.count))
            : sheet(template: template, color: notebook.paperColor, in: notebook)
        try AtomicFile.writeJSON(page, to: paths.pageJSON(notebook.path, page.id))
        let notebookID = notebook.id
        try database.queue.write { db in
            try Self.upsertPageRow(
                page, notebookID: notebookID, position: notebook.pages.count, in: db)
        }

        var order = notebook.pages
        if let pageID, let index = order.firstIndex(of: pageID) {
            order.insert(page.id, at: index + 1)
        } else {
            order.append(page.id)
        }
        try writeOrder(order, for: &notebook)
        if page.paperColor == .white, page.size != .board, page.title == nil {
            try journal.append(
                .init(
                    kind: .pageCreate, notebookPath: notebook.path, pageID: page.id,
                    payload: pageID.map { ["after": $0] } ?? [:]))
        } else {
            // A page create carries only the template and the size; a colour,
            // a board's extent or its title needs the whole page.json.
            try journal.append(
                .creatingPage(notebookPath: notebook.path, pageID: page.id, after: pageID))
        }
        return page
    }

    /// "Board N" for the next board: one more than there are, or the next
    /// number no board of this whiteboard is already called.
    private func nextBoardTitle(notebookID: String, count: Int) throws -> String {
        let titles = try database.queue.read { db in
            Set(
                try String.fetchAll(
                    db, sql: "SELECT title FROM pages WHERE notebook_id = ? AND title IS NOT NULL",
                    arguments: [notebookID]))
        }
        var number = count + 1
        while titles.contains("Board \(number)") { number += 1 }
        return "Board \(number)"
    }

    public func reorderPages(notebookID: String, order: [String]) throws {
        writeLock.lock()
        defer { writeLock.unlock() }
        var notebook = try notebook(id: notebookID)
        // Never drop a page that exists: unknown ids are filtered, missing ids
        // are appended, exactly like the server's `pages[]` patch rule.
        let known = Set(notebook.pages)
        var next = order.filter(known.contains)
        for id in notebook.pages where !next.contains(id) { next.append(id) }
        try writeOrder(next, for: &notebook)
    }

    public func deletePage(notebookID: String, pageID: String) throws -> TrashToken {
        writeLock.lock()
        defer { writeLock.unlock() }
        var notebook = try notebook(id: notebookID)
        let position = notebook.pages.firstIndex(of: pageID) ?? notebook.pages.count
        let token = TrashToken(
            id: PaperID.make(), kind: .page, title: "Page \(position + 1)")
        let directory = paths.trash.appending(path: token.id, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let dropped = try journal.unpushed(notebookPath: notebook.path, pageID: pageID)
        try AtomicFile.writeJSON(
            TrashManifest(
                kind: .page, notebookPath: notebook.path, notebookID: notebook.id, pageID: pageID,
                position: position, deletedAt: Date(), droppedOps: dropped),
            to: directory.appending(path: "manifest.json", directoryHint: .notDirectory))
        let source = paths.pageDirectory(notebook.path, pageID)
        if FileManager.default.fileExists(atPath: source.path(percentEncoded: false)) {
            try FileManager.default.moveItem(
                at: source, to: directory.appending(path: "page", directoryHint: .isDirectory))
        }

        var order = notebook.pages
        order.removeAll { $0 == pageID }
        try writeOrder(order, for: &notebook)
        try database.queue.write { db in
            try db.execute(sql: "DELETE FROM pages WHERE id = ?", arguments: [pageID])
        }
        try textIndex.remove(pageID: pageID)
        try journal.append(
            .init(kind: .pageDelete, notebookPath: notebook.path, pageID: pageID))
        return token
    }

    /// Reads `page.json`, lets `body` change it and saves it, holding
    /// `writeLock` throughout: a revision the sync engine records in between
    /// can no longer be written over by the copy read before it. A body that
    /// changes nothing writes nothing.
    ///
    /// PaperUI's `PaperController.restoreLocalSnapshot` and
    /// `NotebookViewController.setTemplate` still read and save in two calls;
    /// they are the callers left to move onto this.
    @discardableResult
    public func updatePage(
        notebookPath: String, pageID: String, _ body: (inout Page) throws -> Void
    ) throws -> Page {
        writeLock.lock()
        defer { writeLock.unlock() }
        let read = try self.page(notebookPath: notebookPath, id: pageID)
        var page = read
        try body(&page)
        #if DEBUG
        testHookBetweenReadAndWrite?()
        #endif
        guard page != read else { return page }
        try save(page: page, notebookPath: notebookPath)
        return page
    }

    /// Writes `page.json`. Callers that also changed the ink write the drawing
    /// first; see `AutosaveCoordinator`. Callers that read the page to change
    /// it go through `updatePage`.
    public func save(page: Page, notebookPath: String) throws {
        writeLock.lock()
        defer { writeLock.unlock() }
        try AtomicFile.writeJSON(page, to: paths.pageJSON(notebookPath, page.id))
        // The notebook's own file has to learn about it too: the tree is the
        // truth, so a `modified` that lives only in SQLite is a "last edited"
        // that silently regresses on the next reconcile.
        if var notebook = try? AtomicFile.readJSON(
            Notebook.self, at: paths.notebookJSON(notebookPath)),
            notebook.modified < page.modified
        {
            notebook.modified = page.modified
            try AtomicFile.writeJSON(notebook, to: paths.notebookJSON(notebookPath))
        }
        try database.queue.write { db in
            guard
                let notebookID = try String.fetchOne(
                    db, sql: "SELECT id FROM notebooks WHERE path = ?", arguments: [notebookPath])
            else { return }
            let position =
                try Int.fetchOne(
                    db, sql: "SELECT position FROM pages WHERE id = ?", arguments: [page.id]) ?? 0
            try Self.upsertPageRow(page, notebookID: notebookID, position: position, in: db)
            try db.execute(
                sql: "UPDATE notebooks SET modified = ? WHERE id = ?",
                arguments: [page.modified.timeIntervalSince1970, notebookID])
        }
    }

    public func writeDrawing(_ data: Data, notebookPath: String, pageID: String) throws {
        try AtomicFile.write(data, to: paths.drawing(notebookPath, pageID))
    }

    /// Writes derived `text.json`. Existing AI text is retained when it still
    /// describes this page revision; this is the local half of the server's
    /// transcript merge rule.
    ///
    /// A fresh reading does not erase the user's correction of the previous
    /// one. If the ink has not been written since that correction was, it
    /// still describes the page and stays live; otherwise it is set aside as
    /// `stale_correction` -- kept, but no longer searched or shown as the
    /// page's text.
    public func writePageText(_ text: PageText, notebookPath: String, pageID: String) throws {
        writeLock.lock()
        defer { writeLock.unlock() }
        var text = text
        if !text.hasCorrection, text.staleCorrection == nil,
            let previous = pageText(notebookPath: notebookPath, pageID: pageID)
        {
            if !previous.hasCorrection {
                text.staleCorrection = previous.staleCorrection
            } else if transcriptPredatesInk(notebookPath: notebookPath, pageID: pageID) {
                text.staleCorrection = StaleCorrection(
                    correctedTranscript: previous.correctedTranscript,
                    corrections: previous.corrections,
                    correctionsRevision: previous.correctionsRevision)
            } else {
                text.correctedTranscript = previous.correctedTranscript
                text.corrections = previous.corrections
                // The same ink, read again: the correction now edits this
                // reading, and says so in the revision it carries.
                text.correctionsRevision = [previous.correctionsRevision, text.forRevision]
                    .compactMap { $0 }.max()
            }
        }
        try storePageText(text, notebookPath: notebookPath, pageID: pageID)
    }

    /// `text.json` as given, keeping only a same-revision AI reading from the
    /// file it replaces.
    private func storePageText(_ text: PageText, notebookPath: String, pageID: String) throws {
        // A transcript is of a page: never written where there is none,
        // which would make a directory a folder move left behind again.
        guard
            FileManager.default.fileExists(
                atPath: paths.pageJSON(notebookPath, pageID).path(percentEncoded: false))
        else { throw PaperStoreError.notFound(pageID) }
        var value = try JSONSerialization.jsonObject(with: PaperJSON.encode(text))
            as? [String: Any] ?? [:]
        let oldValue = (try? JSONSerialization.jsonObject(
            with: Data(contentsOf: paths.text(notebookPath, pageID)))) as? [String: Any]
        if value["transcript_ai"] == nil,
            let ai = oldValue?["transcript_ai"] as? [String: Any],
            let page = try? self.page(notebookPath: notebookPath, id: pageID)
        {
            let aiRevision: Int
            if let number = ai["revision"] as? NSNumber { aiRevision = number.intValue }
            else { aiRevision = Int(ai["revision"] as? String ?? "") ?? 0 }
            if aiRevision == page.revision { value["transcript_ai"] = ai }
        }
        let merged = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
        try AtomicFile.write(merged, to: paths.text(notebookPath, pageID))
    }

    /// The page's transcript as it is on disk, or `nil` when nothing has read
    /// the page yet.
    public func pageText(notebookPath: String, pageID: String) -> PageText? {
        try? AtomicFile.readJSON(PageText.self, at: paths.text(notebookPath, pageID))
    }

    /// Whether the ink was written after `text.json` was: the transcript, and
    /// any correction in it, then describe strokes that have since changed.
    /// Both halves are written through `AtomicFile`, so the dates are the
    /// dates of the last write.
    func transcriptPredatesInk(notebookPath: String, pageID: String) -> Bool {
        guard let ink = modificationDate(of: paths.drawing(notebookPath, pageID)),
            let text = modificationDate(of: paths.text(notebookPath, pageID))
        else { return false }
        return ink > text
    }

    private func modificationDate(of url: URL) -> Date? {
        (try? FileManager.default.attributesOfItem(atPath: url.path(percentEncoded: false)))?[
            .modificationDate] as? Date
    }

    /// Records a user's corrected transcript while preserving raw OCR lines
    /// and any same-revision AI transcript. Corrections are derived metadata,
    /// so this does not bump the ink revision.
    @discardableResult
    public func correctPageText(
        notebookID: String,
        pageID: String,
        correctedTranscript: String?,
        corrections: [TextCorrection] = []
    ) throws -> PageText {
        writeLock.lock()
        defer { writeLock.unlock() }
        let notebook = try notebook(id: notebookID)
        guard notebook.pages.contains(pageID) else { throw PaperStoreError.notFound(pageID) }
        let current = pageText(notebookPath: notebook.path, pageID: pageID)
            ?? PageText(lines: [], transcript: "")
        let pageRevision = (try? page(notebookPath: notebook.path, id: pageID).revision) ?? 0
        let textURL = paths.text(notebook.path, pageID)
        // Correcting a reading of older ink does not make it a reading of the
        // new strokes: its date goes back on after the write, so the push
        // still holds it back and the next reading sets this correction aside.
        let readAt =
            transcriptPredatesInk(notebookPath: notebook.path, pageID: pageID)
            ? modificationDate(of: textURL) : nil
        var corrected = current
        corrected.correctedTranscript = correctedTranscript
        corrected.corrections = corrections
        // Saving a correction, or going back to the original, settles one an
        // earlier edit left stale.
        corrected.staleCorrection = nil
        // Materialise correction-only edits for the server's index. The audit
        // spans remain alongside the materialised text and raw OCR.
        if corrected.correctedTranscript == nil, !corrections.isEmpty {
            corrected.correctedTranscript = PageText(
                lines: current.lines, transcript: current.transcript, corrections: corrections)
                .searchableTranscript
        }
        // The correction edits this reading, so it is never older than it:
        // that is what lets the server file both against the same ink.
        let forRevision = current.forRevision ?? pageRevision
        corrected.forRevision = forRevision
        corrected.correctionsRevision =
            corrected.hasCorrection ? max(pageRevision, forRevision) : nil
        try storePageText(corrected, notebookPath: notebook.path, pageID: pageID)
        if let readAt {
            try? FileManager.default.setAttributes(
                [.modificationDate: readAt], ofItemAtPath: textURL.path(percentEncoded: false))
        }
        try textIndex.upsert(
            notebookPath: notebook.path, pageID: pageID, text: corrected, forRevision: pageRevision,
            updatedAt: readAt ?? Date())
        try journal.append(
            .init(kind: .pageText, notebookPath: notebook.path, pageID: pageID,
                payload: ["corrected": "true"]))
        return pageText(notebookPath: notebook.path, pageID: pageID) ?? corrected
    }

    /// Convenience for UIs that apply one replacement over the raw OCR.
    @discardableResult
    public func addPageTextCorrection(
        notebookID: String, pageID: String, correction: TextCorrection
    ) throws -> PageText {
        writeLock.lock()
        defer { writeLock.unlock() }
        let notebook = try notebook(id: notebookID)
        guard notebook.pages.contains(pageID) else { throw PaperStoreError.notFound(pageID) }
        let current = pageText(notebookPath: notebook.path, pageID: pageID)
            ?? PageText(lines: [], transcript: "")
        var corrections = current.corrections
        corrections.append(correction)
        return try correctPageText(
            notebookID: notebookID, pageID: pageID,
            correctedTranscript: current.correctedTranscript, corrections: corrections)
    }

    /// The device's handwriting index, over this store's database.
    public var textIndex: TextIndex { TextIndex(database: database) }

    // MARK: - View state

    public func viewState(notebookID: String) -> NotebookViewState {
        let row = try? database.queue.read { db in
            try Row.fetchOne(
                db, sql: "SELECT * FROM notebook_state WHERE notebook_id = ?",
                arguments: [notebookID])
        }
        guard let row else { return NotebookViewState() }
        return NotebookViewState(
            lastPageID: row["last_page_id"],
            zoom: CGFloat(row["zoom"] as Double? ?? 1),
            offset: CGPoint(
                x: row["offset_x"] as Double? ?? 0, y: row["offset_y"] as Double? ?? 0))
    }

    public func setViewState(_ state: NotebookViewState, notebookID: String) throws {
        try database.queue.write { db in
            try db.execute(
                sql: """
                    INSERT INTO notebook_state (notebook_id, last_page_id, zoom, offset_x, offset_y)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(notebook_id) DO UPDATE SET
                        last_page_id = excluded.last_page_id,
                        zoom = excluded.zoom,
                        offset_x = excluded.offset_x,
                        offset_y = excluded.offset_y
                    """,
                arguments: [
                    notebookID, state.lastPageID, Double(state.zoom), Double(state.offset.x),
                    Double(state.offset.y),
                ])
        }
    }

    // MARK: - Objects and assets

    /// Images and imported PDFs for this store.
    public var assets: AssetStore { AssetStore(paths: paths) }

    /// Stores bytes as an asset of `notebookPath` and returns its
    /// vault-relative path. Content-hashed, so storing the same bytes twice
    /// costs one file.
    @discardableResult
    public func storeAsset(_ data: Data, preferredName: String, notebookPath: String) throws
        -> String
    {
        let path = try assets.store(data, preferredName: preferredName, notebookPath: notebookPath)
        try journal.append(
            .init(kind: .assetPut, notebookPath: notebookPath, payload: ["path": path]))
        return path
    }

    /// An upload for every asset the notebook holds on this device: files
    /// that arrived without `storeAsset`, which the server has not got.
    private func journalAssetUploads(of notebook: Notebook) throws {
        for path in assets.assetPaths(forNotebook: notebook.path) {
            try journal.append(
                .init(
                    kind: .assetPut, notebookPath: notebook.path, notebookID: notebook.id,
                    payload: ["path": path]))
        }
    }

    /// Replaces a page's objects and returns the page as written.
    ///
    /// Objects live in `page.json`, so this is the same debounced flush the ink
    /// uses: the caller marks the page dirty and autosave does the rest.
    @discardableResult
    public func setObjects(_ objects: [PageObject], notebookPath: String, pageID: String) throws
        -> Page
    {
        try updatePage(notebookPath: notebookPath, pageID: pageID) { page in
            page.objects = objects
            page.modified = Date()
        }
    }

    /// Records the new extent of an infinite page.
    ///
    /// `size` stays the `infinite` preset -- the page is still an infinite
    /// page, it is simply bigger -- and the measurement itself goes in
    /// `size_points`, which already rides `page.json` to the server.
    @discardableResult
    public func setPageExtent(_ extent: CGSize, notebookPath: String, pageID: String) throws
        -> Page
    {
        let rounded = SizePoints(
            CGSize(width: extent.width.rounded(), height: extent.height.rounded()))
        var changed = false
        let page = try updatePage(notebookPath: notebookPath, pageID: pageID) { page in
            guard page.customSize != rounded else { return }
            page.customSize = rounded
            page.modified = Date()
            changed = true
        }
        guard changed else { return page }
        try journal.append(
            .init(kind: .pagePut, notebookPath: notebookPath, pageID: pageID,
                payload: ["metadata": "extent"]))
        return page
    }

    /// Sets an optional page title without changing the page's stable id.
    @discardableResult
    public func setPageTitle(notebookID: String, pageID: String, title: String?) throws -> Page {
        let notebook = try notebook(id: notebookID)
        guard notebook.pages.contains(pageID) else { throw PaperStoreError.notFound(pageID) }
        let trimmed = title?.trimmingCharacters(in: .whitespacesAndNewlines)
        let page = try updatePage(notebookPath: notebook.path, pageID: pageID) { page in
            page.title = trimmed?.isEmpty == true ? nil : trimmed
            page.modified = Date()
        }
        try journal.append(
            .init(kind: .pagePut, notebookPath: notebook.path, pageID: pageID,
                payload: ["metadata": "title"]))
        return page
    }

    /// Toggles a page bookmark. It travels with the page PUT during sync.
    @discardableResult
    public func setPageBookmarked(notebookID: String, pageID: String, _ bookmarked: Bool) throws
        -> Page
    {
        let notebook = try notebook(id: notebookID)
        guard notebook.pages.contains(pageID) else { throw PaperStoreError.notFound(pageID) }
        let page = try updatePage(notebookPath: notebook.path, pageID: pageID) { page in
            page.bookmarked = bookmarked
            page.modified = Date()
        }
        try journal.append(
            .init(kind: .pagePut, notebookPath: notebook.path, pageID: pageID,
                payload: ["metadata": "bookmarked"]))
        return page
    }

    /// Changes one page's template, its paper colour, or both; `nil` leaves
    /// that one as it is. The ink is untouched -- on dark paper PencilKit
    /// shows the same strokes in their dark-appearance colours. A change that
    /// changes nothing writes and journals nothing.
    @discardableResult
    public func setPageStyle(
        notebookID: String, pageID: String, template: Template? = nil,
        paperColor: PaperColor? = nil
    ) throws -> Page {
        let notebook = try notebook(id: notebookID)
        guard notebook.pages.contains(pageID) else { throw PaperStoreError.notFound(pageID) }
        var changed = false
        let page = try updatePage(notebookPath: notebook.path, pageID: pageID) { page in
            let before = (page.template, page.paperColor)
            if let template { page.template = template }
            if let paperColor { page.paperColor = paperColor }
            guard before != (page.template, page.paperColor) else { return }
            page.modified = Date()
            changed = true
        }
        guard changed else { return page }
        try journal.append(
            .init(kind: .pagePut, notebookPath: notebook.path, pageID: pageID,
                payload: ["metadata": "style"]))
        return page
    }

    /// Adds an already-built page to a notebook, after `pageID` or at the end.
    ///
    /// The PDF importer needs this: its pages carry a media box and a binding
    /// that `createPage` knows nothing about.
    @discardableResult
    public func insertPage(_ page: Page, notebookID: String, after pageID: String? = nil) throws
        -> Page
    {
        writeLock.lock()
        defer { writeLock.unlock() }
        var notebook = try notebook(id: notebookID)
        try AtomicFile.writeJSON(page, to: paths.pageJSON(notebook.path, page.id))
        let identifier = notebook.id
        let count = notebook.pages.count
        try database.queue.write { db in
            try Self.upsertPageRow(page, notebookID: identifier, position: count, in: db)
        }

        var order = notebook.pages
        if let pageID, let index = order.firstIndex(of: pageID) {
            order.insert(page.id, at: index + 1)
        } else {
            order.append(page.id)
        }
        try writeOrder(order, for: &notebook)
        // A create-PUT rather than a `pageCreate`: the page arrives with the
        // binding and the media box it was built with, not as a blank page.
        try journal.append(
            .creatingPage(notebookPath: notebook.path, pageID: page.id, after: pageID))
        return page
    }

    // MARK: - Reconciler

    /// Rebuilds the index from the file tree.
    ///
    /// Runs at every launch because the tree is the truth and the rows are not:
    /// a crash between the JSON write and the row write, a restore from a
    /// backup, or an M3 sync pull that landed files, all show up here as drift
    /// and are repaired without touching a single byte of ink.
    @discardableResult
    public func reconcile() throws -> ReconcileReport {
        var report = ReconcileReport()
        let manager = FileManager.default
        try manager.createDirectory(at: paths.paper, withIntermediateDirectories: true)

        // Folders first: every directory that is not a package gets its
        // record, and one left half-way through a case-only rename is put
        // back where its packages can be found.
        try reconcileFolders()

        var found: Set<String> = []
        for path in try notebookPackagePaths() {
            found.insert(path)
            do {
                try indexNotebook(at: path)
                report.indexed += 1
            } catch {
                report.skipped.append(path)
            }
        }

        // A notebook created or moved while the walk ran has a row the walk
        // never saw; its package on disk says it is not stale. The reconcile
        // runs off the main actor, beside the library's own writes.
        let stale = try database.queue.read { db in
            try String.fetchAll(db, sql: "SELECT path FROM notebooks")
        }.filter {
            !found.contains($0)
                && !manager.fileExists(atPath: paths.notebookJSON($0).path(percentEncoded: false))
        }
        if !stale.isEmpty {
            try database.queue.write { db in
                for path in stale {
                    try db.execute(sql: "DELETE FROM notebooks WHERE path = ?", arguments: [path])
                }
            }
            report.removed = stale.count
        }
        // Transcripts of pages that are gone -- deleted by the server, lost to
        // a conflict, or removed before the index learned to follow them.
        try database.queue.write { db in
            try db.execute(
                sql: "DELETE FROM page_fts WHERE page_id NOT IN (SELECT id FROM pages)")
            try db.execute(
                sql: "DELETE FROM page_text WHERE page_id NOT IN (SELECT id FROM pages)")
        }
        return report
    }

    public struct ReconcileReport: Equatable, Sendable {
        public var indexed = 0
        public var removed = 0
        public var skipped: [String] = []
    }

    /// Reads one package off disk into the index, healing a `notebook.json`
    /// whose `pages[]` disagrees with the directories that actually exist.
    ///
    /// The launch reconcile runs this off the main actor, beside page adds,
    /// deletes and moves, so the walk holds `writeLock` from the read to the
    /// rows: an order read before a delete and written after it would bring
    /// the page back.
    func indexNotebook(at path: String) throws {
        writeLock.lock()
        let pages: [Page]
        do {
            defer { writeLock.unlock() }
            pages = try indexNotebookLocked(at: path)
        }

        // The handwriting index follows the tree like every other row here: a
        // `text.json` restored from a backup, or pulled from the server while
        // this device was not looking, is searchable without re-reading a
        // single page with Vision.
        let index = textIndex
        for page in pages {
            indexPageText(pageID: page.id, notebookPath: path, textRevision: page.textRevision,
                index: index)
        }
    }

    /// The part of `indexNotebook` that reads and writes the package and its
    /// rows. Callers hold `writeLock`.
    private func indexNotebookLocked(at path: String) throws -> [Page] {
        var notebook = try AtomicFile.readJSON(Notebook.self, at: paths.notebookJSON(path))
        notebook.path = path

        let onDisk = (try? FileManager.default.contentsOfDirectory(
            at: paths.pagesDirectory(path), includingPropertiesForKeys: nil)) ?? []
        let existing = Set(
            onDisk.filter { $0.hasDirectoryPath }.map { $0.lastPathComponent }
                .filter(PaperID.isValid))
        #if DEBUG
        testHookBetweenReadAndWrite?()
        #endif

        var order = notebook.pages.filter(existing.contains)
        for id in existing.sorted() where !order.contains(id) { order.append(id) }

        var pages: [Page] = []
        for id in order {
            if let page = try? AtomicFile.readJSON(Page.self, at: paths.pageJSON(path, id)) {
                pages.append(page)
            } else if FileManager.default.fileExists(
                atPath: paths.pageDirectory(path, id).path(percentEncoded: false))
            {
                // A page directory with no readable page.json still holds ink.
                // Give it a default descriptor rather than lose it.
                let recovered = Page(
                    id: id, template: notebook.template, size: notebook.paperSize)
                try AtomicFile.writeJSON(recovered, to: paths.pageJSON(path, id))
                pages.append(recovered)
            }
            // Otherwise the directory went since it was listed. Writing a
            // descriptor would create it again, as a blank page.
        }
        order = pages.map(\.id)
        let healed = order != notebook.pages
        notebook.pages = order
        if healed { try AtomicFile.writeJSON(notebook, to: paths.notebookJSON(path)) }

        try database.queue.write { db in
            try Self.upsertNotebookRow(notebook, in: db)
            try db.execute(
                sql: "DELETE FROM pages WHERE notebook_id = ?", arguments: [notebook.id])
            for (index, page) in pages.enumerated() {
                try Self.upsertPageRow(page, notebookID: notebook.id, position: index, in: db)
            }
        }
        return pages
    }

    /// Brings `page_text`/`page_fts` in step with the `text.json` on disk.
    ///
    /// Cheap on the common path: the file's modification date is compared with
    /// what the index already recorded, so a relaunch of a notebook full of
    /// transcripts re-reads nothing.
    public func indexPageText(
        pageID: String, notebookPath: String, textRevision: Int = 0, index: TextIndex? = nil
    ) {
        let index = index ?? textIndex
        let url = paths.text(notebookPath, pageID)
        let attributes = try? FileManager.default.attributesOfItem(
            atPath: url.path(percentEncoded: false))
        guard let modified = attributes?[.modificationDate] as? Date else {
            // No transcript on disk -- it went with a conflict sibling, say --
            // so nothing of this page should match.
            if index.indexedAt(pageID: pageID) != nil { try? index.remove(pageID: pageID) }
            return
        }
        if let indexed = index.indexedAt(pageID: pageID),
            abs(indexed.timeIntervalSince(modified)) < 0.5
        {
            return
        }
        guard let text = try? AtomicFile.readJSON(PageText.self, at: url) else { return }
        try? index.upsert(
            notebookPath: notebookPath, pageID: pageID, text: text, forRevision: textRevision,
            updatedAt: modified)
    }

    // MARK: - Derived files

    /// Deletes what this device drew from the ink: every page's `render.png`,
    /// the page files named in `alsoPageFiles`, the page's library cover, and
    /// its search rows. Returns how many files were removed.
    ///
    /// `text.json` stays although it is derived: it carries the user's
    /// corrections and the server's AI transcript, which no redraw can bring
    /// back. The rows come back from it through `indexPageText` -- a removed
    /// row has no `indexedAt`, so the next reconcile re-reads it too.
    @discardableResult
    public func clearDerivedFiles(alsoPageFiles: [String] = []) throws -> Int {
        let manager = FileManager.default
        // Bare file names only, and never one of the page's source files.
        let sources: Set<String> = ["page.json", "drawing.pkdrawing", "text.json"]
        let extras = alsoPageFiles.filter {
            !$0.isEmpty && !$0.contains("/") && $0 != "." && $0 != ".." && !sources.contains($0)
        }
        var removed = 0
        func remove(_ url: URL) throws {
            guard manager.fileExists(atPath: url.path(percentEncoded: false)) else { return }
            try manager.removeItem(at: url)
            removed += 1
        }
        let index = textIndex
        for path in try notebookPackagePaths() {
            let onDisk = (try? manager.contentsOfDirectory(
                at: paths.pagesDirectory(path), includingPropertiesForKeys: nil)) ?? []
            let pageIDs = onDisk.filter { $0.hasDirectoryPath }.map { $0.lastPathComponent }
                .filter(PaperID.isValid)
            for pageID in pageIDs {
                try remove(paths.render(path, pageID))
                let directory = paths.pageDirectory(path, pageID)
                for name in extras {
                    try remove(directory.appending(path: name, directoryHint: .notDirectory))
                }
                // Covers are keyed by page id alone, so this takes exactly this
                // server's and leaves every other server's in place.
                try remove(PageRenderer.coverURL(pageID: pageID))
                try index.remove(pageID: pageID)
            }
        }
        return removed
    }

    // MARK: - Internals

    /// Walks the tree by name rather than by comparing absolute paths: the
    /// container lives under a symlinked `/var` on both the simulator and the
    /// test host, and the enumerator hands back the resolved spelling.
    private func notebookPackagePaths() throws -> [String] {
        var result: [String] = []
        collectPackages(in: paths.paper, prefix: ["paper"], into: &result)
        return result.sorted()
    }

    private func collectPackages(in directory: URL, prefix: [String], into result: inout [String]) {
        let children =
            (try? FileManager.default.contentsOfDirectory(
                at: directory, includingPropertiesForKeys: [.isDirectoryKey],
                options: [.skipsHiddenFiles])) ?? []
        for child in children where child.hasDirectoryPath {
            let name = child.lastPathComponent
            let path = (prefix + [name]).joined(separator: "/")
            if name.hasSuffix(".paper") {
                if PaperPaths.isSafeNotebookPath(path) { result.append(path) }
            } else {
                collectPackages(in: child, prefix: prefix + [name], into: &result)
            }
        }
    }

    private func writeOrder(_ order: [String], for notebook: inout Notebook) throws {
        writeLock.lock()
        defer { writeLock.unlock() }
        notebook.pages = order
        notebook.modified = Date()
        if let cover = notebook.cover, !order.contains(cover) { notebook.cover = nil }
        try writeNotebookJSON(notebook)
        let snapshot = notebook
        try database.queue.write { db in
            try Self.upsertNotebookRow(snapshot, in: db)
            for (index, id) in order.enumerated() {
                try db.execute(
                    sql: "UPDATE pages SET position = ? WHERE id = ?", arguments: [index, id])
            }
        }
        // Page order is notebook metadata on the wire, so it rides a patch.
        try journal.append(
            .init(
                kind: .notebookPatch, notebookPath: notebook.path,
                payload: ["pages": order.joined(separator: ",")]))
    }

    private func move(notebook: inout Notebook, from oldPath: String, to newPath: String) throws {
        writeLock.lock()
        defer { writeLock.unlock() }
        guard oldPath != newPath else {
            try writeNotebookJSON(notebook)
            let snapshot = notebook
            try database.queue.write { db in try Self.upsertNotebookRow(snapshot, in: db) }
            return
        }
        let destination = paths.url(forVaultPath: newPath)
        try ensureFolderRecords(forAncestorsOf: newPath)
        try FileManager.default.createDirectory(
            at: destination.deletingLastPathComponent(), withIntermediateDirectories: true)
        try FileManager.default.moveItem(at: paths.url(forVaultPath: oldPath), to: destination)
        notebook.path = newPath
        try writeNotebookJSON(notebook)
        let snapshot = notebook
        try database.queue.write { db in
            try Self.upsertNotebookRow(snapshot, in: db)
            // A directory move keeps every text.json's date, so a reconcile
            // would skip these rows and never learn the new path.
            try db.execute(
                sql: """
                    UPDATE page_text SET notebook_path = ?
                    WHERE page_id IN (SELECT id FROM pages WHERE notebook_id = ?)
                    """,
                arguments: [newPath, snapshot.id])
        }
    }

    private func uniqueNotebookPath(folder: String, title: String, excluding id: String? = nil)
        throws -> String
    {
        try uniquePackagePath(
            base: "\(canonicalFolderPath(folder))/\(PaperPaths.slug(title)).paper", title: title,
            excluding: id)
    }

    /// The folder `folder` (`Work/Plans`, unprefixed) names, spelled as the
    /// directories on disk spell it, root kept: each part takes the name of
    /// the folder it matches exactly, else the one it matches after NFC and
    /// case folding -- the server's `canonical_folder` -- and from the first
    /// part that matches nothing, each becomes a new folder's directory name
    /// (`folderSlug`). A folder's directory is not always the slug of its
    /// name (one another client made, one made before the slug rules), and
    /// slugging it again put the notebook in a new twin folder.
    func canonicalFolderPath(_ folder: String) -> String {
        var path = FolderPath.root
        var matching = true
        for part in folder.split(separator: "/").map(String.init) where !part.isEmpty {
            if matching {
                let children = takenFolderNames(in: path)
                if children.contains(part) {
                    path = FolderPath.join(path, part)
                    continue
                }
                if let match = children.first(where: { FolderPath.fold($0) == FolderPath.fold(part) }) {
                    path = FolderPath.join(path, match)
                    continue
                }
                matching = false
            }
            path = FolderPath.join(path, PaperFolderID.folderSlug(part))
        }
        return path
    }

    /// The folder directories in `parent`, as the disk spells them.
    private func takenFolderNames(in parent: String) -> [String] {
        let children =
            (try? FileManager.default.contentsOfDirectory(
                at: paths.url(forVaultPath: parent), includingPropertiesForKeys: [.isDirectoryKey],
                options: [.skipsHiddenFiles])) ?? []
        return children.filter(\.hasDirectoryPath).map(\.lastPathComponent)
            .filter { !$0.hasSuffix(".paper") }.sorted()
    }

    /// `base`, or `base` with " 2", " 3" … before `.paper`: the name the
    /// server would give it. A name is taken by any other entry beside it --
    /// a folder by its name, a notebook by its name without `.paper` --
    /// compared after NFC and case folding, as the server's `_taken` does,
    /// and by anything on disk at that path. The server answers a rename
    /// onto a taken name with 409, so a name it would refuse is never chosen
    /// here. The notebook `id` names keeps its own place.
    private func uniquePackagePath(base: String, title: String, excluding id: String? = nil)
        throws -> String
    {
        guard PaperPaths.isSafeNotebookPath(base) else {
            throw PaperStoreError.unsafePath(base)
        }
        let parent = FolderPath.parent(of: base)
        let (taken, own) = try database.queue.read { db -> (Set<String>, String?) in
            let rows = try Row.fetchAll(db, sql: "SELECT id, path FROM notebooks")
            var taken: Set<String> = []
            var own: String?
            for row in rows {
                let rowID: String? = row["id"]
                guard let path: String = row["path"] else { continue }
                if let id, rowID == id {
                    own = path
                } else {
                    taken.insert(path)
                }
            }
            return (taken, own)
        }
        // Its own name beside it is not taken: a retitle in place, or a
        // change of case, is the notebook itself.
        let ownLeaf = own.flatMap { FolderPath.parent(of: $0) == parent ? FolderPath.leaf(of: $0) : nil }
        var names = takenEntryNames(in: parent, excluding: ownLeaf)
        for path in taken where FolderPath.parent(of: path) == parent {
            names.insert(FolderPath.fold(String(FolderPath.leaf(of: path).dropLast(".paper".count))))
        }
        let manager = FileManager.default
        func isFree(_ path: String) -> Bool {
            if path == own { return true }
            let stem = String(FolderPath.leaf(of: path).dropLast(".paper".count))
            return !taken.contains(path) && !names.contains(FolderPath.fold(stem))
                && !manager.fileExists(
                    atPath: paths.url(forVaultPath: path).path(percentEncoded: false))
        }
        // The server refuses a vault path over 900 bytes (contract 2.2), and
        // a create it refuses is refused for good: the device does not make
        // one.
        func fits(_ path: String) throws -> String {
            guard path.utf8.count <= FolderPath.maximumBytes else {
                throw PaperStoreError.unsafePath(path)
            }
            return path
        }
        if isFree(base) { return try fits(base) }
        let stem = String(base.dropLast(".paper".count))
        for suffix in 2...999 {
            let candidate = "\(stem) \(suffix).paper"
            if isFree(candidate) { return try fits(candidate) }
        }
        throw PaperStoreError.titleInUse(title)
    }

    private func duplicateTitle(of title: String) -> String { "\(title) copy" }

    private static func notebook(from row: Row, in db: Database) throws -> Notebook {
        let id: String = row["id"] ?? ""
        let tagsText: String = row["tags"] ?? "[]"
        let pages = try String.fetchAll(
            db, sql: "SELECT id FROM pages WHERE notebook_id = ? ORDER BY position", arguments: [id])
        return Notebook(
            id: id,
            title: row["title"] ?? "",
            created: Date(timeIntervalSince1970: row["created"] ?? 0),
            modified: Date(timeIntervalSince1970: row["modified"] ?? 0),
            template: Template(rawValue: row["template"] ?? "") ?? .ruled,
            paperSize: PaperSize(rawValue: row["paper_size"] ?? "") ?? .a4,
            cover: row["cover"],
            tags: (try? JSONDecoder().decode([String].self, from: Data(tagsText.utf8))) ?? [],
            favorite: row["favorite"] ?? false,
            pages: pages,
            seq: row["seq"] ?? 0,
            kind: DocumentKind.matching(rawValue: row["kind"] ?? "") ?? .notebook,
            orientation: PageOrientation.matching(rawValue: row["orientation"] ?? "") ?? .portrait,
            paperColor: PaperColor.matching(rawValue: row["paper_color"] ?? "") ?? .white,
            path: row["path"] ?? "")
    }

    private static func upsertNotebookRow(_ notebook: Notebook, in db: Database) throws {
        let tags =
            String(data: (try? JSONEncoder().encode(notebook.tags)) ?? Data("[]".utf8), encoding: .utf8)
            ?? "[]"
        try db.execute(
            sql: """
                INSERT INTO notebooks
                    (id, path, title, folder, template, paper_size, cover, tags, favorite,
                     created, modified, seq, kind, orientation, paper_color)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    path = excluded.path, title = excluded.title, folder = excluded.folder,
                    template = excluded.template, paper_size = excluded.paper_size,
                    cover = excluded.cover, tags = excluded.tags, favorite = excluded.favorite,
                    modified = excluded.modified, seq = excluded.seq, kind = excluded.kind,
                    orientation = excluded.orientation, paper_color = excluded.paper_color
                """,
            arguments: [
                notebook.id, notebook.path, notebook.title, notebook.folder,
                notebook.template.rawValue, notebook.paperSize.rawValue, notebook.cover, tags,
                notebook.favorite, notebook.created.timeIntervalSince1970,
                notebook.modified.timeIntervalSince1970, notebook.seq, notebook.kind.rawValue,
                notebook.orientation.rawValue, notebook.paperColor.rawValue,
            ])
    }

    /// Writes `notebook.json`, keeping the keys this build does not know from
    /// the file it replaces. Most writers start from the index row, which
    /// holds no unknown keys; without this every favourite or reorder would
    /// strip a field a newer build added.
    func writeNotebookJSON(_ notebook: Notebook) throws {
        var notebook = notebook
        let url = paths.notebookJSON(notebook.path)
        if let onDisk = try? AtomicFile.readJSON(Notebook.self, at: url) {
            notebook.additionalFields.merge(onDisk.additionalFields) { mine, _ in mine }
        }
        try AtomicFile.writeJSON(notebook, to: url)
    }

    private static func upsertPageRow(
        _ page: Page, notebookID: String, position: Int, in db: Database
    ) throws {
        try db.execute(
                sql: """
                    INSERT INTO pages
                    (id, notebook_id, position, template, paper_size, title, bookmarked,
                     custom_width, custom_height, created, modified, revision,
                     render_revision, text_revision, conflict_of, seq)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    notebook_id = excluded.notebook_id, position = excluded.position,
                    template = excluded.template, paper_size = excluded.paper_size,
                    title = excluded.title, bookmarked = excluded.bookmarked,
                    custom_width = excluded.custom_width, custom_height = excluded.custom_height,
                    modified = excluded.modified, revision = excluded.revision,
                    render_revision = excluded.render_revision,
                    text_revision = excluded.text_revision, conflict_of = excluded.conflict_of,
                    seq = excluded.seq
                """,
            arguments: [
                page.id, notebookID, position, page.template.rawValue, page.size.rawValue,
                page.title, page.bookmarked, page.customSize?.width, page.customSize?.height,
                page.created.timeIntervalSince1970, page.modified.timeIntervalSince1970,
                page.revision, page.renderRevision, page.textRevision, page.conflictOf, page.seq,
            ])
    }
}

/// What the device trash records beside the moved bytes.
struct TrashManifest: Codable, Sendable {
    enum Kind: String, Codable, Sendable {
        case notebook
        case page
        /// `notebookPath` is the folder's path and `notebookID` its id.
        case folder
    }

    var kind: Kind
    var notebookPath: String
    var notebookID: String
    var pageID: String?
    var position: Int?
    var deletedAt: Date
    /// The unpushed ops the delete dropped from the queue, oldest first. Nil
    /// for a remote delete and for a manifest written before this was kept.
    var droppedOps: [PaperOp]? = nil
}
