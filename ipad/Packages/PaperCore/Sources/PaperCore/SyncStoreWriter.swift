import Foundation
import GRDB

/// The writes a pull makes, and the small amount of bookkeeping a push leaves
/// behind.
///
/// Kept apart from `PaperStore` proper because everything here has one property
/// the rest of the store does not: it applies somebody else's decision. None of
/// it appends to the op journal -- a change that came from the server must not
/// be pushed straight back to it -- and all of it reindexes through
/// `indexNotebook`, so the SQLite rows follow the files rather than being
/// written twice.
extension PaperStore {

    // MARK: - Sync cursor

    public func syncValue(_ key: String) -> String? {
        try? database.queue.read { db in
            try String.fetchOne(
                db, sql: "SELECT value FROM sync_state WHERE key = ?", arguments: [key])
        }
    }

    public func setSyncValue(_ value: String?, for key: String) throws {
        try database.queue.write { db in
            guard let value else {
                try db.execute(sql: "DELETE FROM sync_state WHERE key = ?", arguments: [key])
                return
            }
            try db.execute(
                sql: """
                    INSERT INTO sync_state (key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                arguments: [key, value])
        }
    }

    // MARK: - Reading what the pusher needs

    /// The page's local revision, or 0 when the server has never seen it.
    public func localRevision(notebookPath: String, pageID: String) -> Int {
        (try? page(notebookPath: notebookPath, id: pageID))?.revision ?? 0
    }

    /// Raw `page.json` bytes, reshaped for the wire. The server owns `id`,
    /// `revision` and derived revisions; all other known and unknown metadata
    /// is retained so an older client cannot silently drop a newer field.
    public func pagePartJSON(notebookPath: String, pageID: String) -> Data? {
        guard let page = try? page(notebookPath: notebookPath, id: pageID) else { return nil }
        let existing = (try? Data(contentsOf: paths.pageJSON(notebookPath, pageID)))
            .flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
        var body: [String: Any] = existing ?? [:]
        let size = page.pointSize
        let objects = (try? PaperJSON.encode(page.objects)).flatMap {
            try? JSONSerialization.jsonObject(with: $0)
        }
        body["template"] = page.template.rawValue
        body["size"] = ["width": Double(size.width), "height": Double(size.height)]
        // The dimensions cannot say `infinite` -- an unused infinite page
        // measures exactly like an iPad page -- so the preset goes by name.
        body["size_name"] = page.size.rawValue
        body["objects"] = objects ?? []
        body["bookmarked"] = page.bookmarked
        if let title = page.title { body["title"] = title } else { body.removeValue(forKey: "title") }
        if let custom = page.customSize {
            body["size_points"] = ["width": custom.width, "height": custom.height]
        } else {
            body.removeValue(forKey: "size_points")
        }
        if let pdf = page.pdf,
            let encoded = try? PaperJSON.encode(pdf),
            let value = try? JSONSerialization.jsonObject(with: encoded)
        {
            body["pdf"] = value
        } else {
            body.removeValue(forKey: "pdf")
        }
        // Server-owned fields are not client patch inputs. Removing them also
        // avoids forwarding stale values from a page pulled from the wire.
        for key in ["id", "revision", "render_revision", "text_revision", "conflict_of", "seq",
                    "path", "notebook", "drawing_url", "render_url", "text_url",
                    "is_bookmarked", "isBookmarked"] {
            body.removeValue(forKey: key)
        }
        return try? JSONSerialization.data(withJSONObject: body, options: [.sortedKeys])
    }

    public func renderData(notebookPath: String, pageID: String) -> Data? {
        try? Data(contentsOf: paths.render(notebookPath, pageID))
    }

    /// `text.json` as `HandwritingRecognizer` wrote it. Absent until a page
    /// has been read; sent with every page PUT from the moment it is not --
    /// except while ink written since makes it a reading of strokes that are
    /// gone. A PUT stamps its text with the revision it is about to create,
    /// so sending that one would file the old reading, and any correction of
    /// it, against ink it never saw. The recognizer reads the new render and
    /// its own derived push carries the result.
    public func textData(notebookPath: String, pageID: String) -> Data? {
        guard !transcriptPredatesInk(notebookPath: notebookPath, pageID: pageID) else {
            return nil
        }
        return try? Data(contentsOf: paths.text(notebookPath, pageID))
    }

    /// The revision the local transcript was made for, or 0 when there is
    /// none. What decides whether a pulled `text.json` is worth fetching.
    public func localTextRevision(notebookPath: String, pageID: String) -> Int {
        (try? page(notebookPath: notebookPath, id: pageID))?.textRevision ?? 0
    }

    /// Where a page sits in its notebook, 1-based -- what the pill names.
    public func pageNumber(notebookPath: String, pageID: String) -> Int? {
        guard let notebook = try? notebook(path: notebookPath),
            let index = notebook.pages.firstIndex(of: pageID)
        else { return nil }
        return index + 1
    }

    public func notebookExists(path: String) -> Bool {
        FileManager.default.fileExists(
            atPath: paths.notebookJSON(path).path(percentEncoded: false))
    }

    // MARK: - Applying a successful push

    /// Records the revisions the server assigned, without touching `modified`:
    /// the ink did not change, only what the server now calls it.
    ///
    /// A field-level merge: the page is read again under `writeLock` and only
    /// the four server-owned fields change, so an edit saved on the main actor
    /// while the push was out survives, and no save can land between this
    /// read and this write. A `nil` leaves that field as it is.
    public func recordServerRevisions(
        notebookPath: String, pageID: String, revision: Int?, renderRevision: Int? = nil,
        textRevision: Int? = nil, seq: Int? = nil
    ) throws {
        writeLock.lock()
        defer { writeLock.unlock() }
        guard var page = try? page(notebookPath: notebookPath, id: pageID) else { return }
        if let revision { page.revision = revision }
        if let renderRevision { page.renderRevision = renderRevision }
        if let textRevision { page.textRevision = textRevision }
        if let seq { page.seq = seq }
        try AtomicFile.writeJSON(page, to: paths.pageJSON(notebookPath, pageID))
        try database.queue.write { db in
            try db.execute(
                sql: """
                    UPDATE pages SET revision = ?, render_revision = ?, text_revision = ?, seq = ?
                    WHERE id = ?
                    """,
                arguments: [
                    page.revision, page.renderRevision, page.textRevision, page.seq, pageID,
                ])
        }
    }

    /// Takes the seq a notebook rename re-stamped a page with, when this
    /// device held the page at the seq it had before (`previousSeq`): the
    /// move was this device's own, and the page is exactly what it has. A page
    /// that had moved on upstream keeps its old seq, so the pull fetches it.
    public func recordMovedPage(
        notebookPath: String, pageID: String, seq: Int, previousSeq: Int
    ) throws {
        writeLock.lock()
        defer { writeLock.unlock() }
        guard var page = try? page(notebookPath: notebookPath, id: pageID),
            page.seq == previousSeq, seq > page.seq
        else { return }
        page.seq = seq
        try AtomicFile.writeJSON(page, to: paths.pageJSON(notebookPath, pageID))
        try database.queue.write { db in
            try db.execute(sql: "UPDATE pages SET seq = ? WHERE id = ?", arguments: [seq, pageID])
        }
    }

    // MARK: - Applying a pull

    /// Writes the server's `page.json` for a page this device has, creating the
    /// page directory when it does not.
    ///
    /// The server's bytes are reshaped rather than re-modelled: fields this
    /// milestone does not know about (M4's `objects` and `pdf`) are copied
    /// across untouched, so a device running older code cannot quietly delete
    /// what a newer one wrote.
    @discardableResult
    public func applyRemotePage(
        rawPageJSON: Data, notebookPath: String, pageID: String, drawing: Data?, render: Data?,
        text: Data? = nil
    ) throws -> Page? {
        writeLock.lock()
        defer { writeLock.unlock() }
        let directory = paths.pageDirectory(notebookPath, pageID)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)

        // Ink first, exactly as autosave does: metadata that claims ink which
        // is not there is the one ordering a crash must not be able to produce.
        // Bytes already on disk are left alone: a title or bookmark edit
        // resends the same ink, and a newer date on it would make the
        // transcript look like a reading of strokes that are gone.
        let drawingURL = paths.drawing(notebookPath, pageID)
        if let drawing, (try? Data(contentsOf: drawingURL)) != drawing {
            try AtomicFile.write(drawing, to: drawingURL)
        }
        if let render {
            try AtomicFile.write(render, to: paths.render(notebookPath, pageID))
        }
        if let text {
            try AtomicFile.write(text, to: paths.text(notebookPath, pageID))
        }

        guard var object = try JSONSerialization.jsonObject(with: rawPageJSON) as? [String: Any]
        else { return nil }
        for key in ["path", "notebook", "drawing_url", "render_url", "text_url"] {
            object.removeValue(forKey: key)
        }
        object["id"] = pageID
        // The preset the server names wins: it is the only thing that tells an
        // infinite page from an iPad or a PDF page of the same dimensions. A
        // page no client has named yet stays infinite when this device already
        // holds it as infinite, or when its notebook makes infinite pages and
        // it could be one; only then do the dimensions pick a preset. Local
        // `size` is the name, so `size_name` itself is not kept.
        let wireName = (object.removeValue(forKey: "size_name") as? String)
            .flatMap(PaperSize.matching(rawValue:))
        let wasInfinite =
            (try? page(notebookPath: notebookPath, id: pageID))?.size == .infinite
        if let size = object["size"] as? [String: Any] {
            let width = (size["width"] as? NSNumber)?.doubleValue ?? (size["width"] as? Double) ?? 0
            let height = (size["height"] as? NSNumber)?.doubleValue ?? (size["height"] as? Double) ?? 0
            let preset =
                wireName
                ?? (wasInfinite
                    || notebookMakesInfinitePages(
                        notebookPath, width: width, height: height, page: object)
                    ? PaperSize.infinite : RemoteSize(width: width, height: height).named)
            object["size"] = preset.rawValue
            if width > 0, height > 0,
                preset == .infinite
                    || !PaperSize.allCases.contains(where: {
                        abs($0.points.width - width) < 1 && abs($0.points.height - height) < 1
                    })
            {
                // Preserve the exact imported/PDF media box -- or an infinite
                // page's extent -- alongside the nearest preset for older
                // clients.
                object["size_points"] = ["width": width, "height": height]
            }
        } else if let wireName {
            object["size"] = wireName.rawValue
        } else if !(object["size"] is String) {
            object["size"] = (wasInfinite ? PaperSize.infinite : PaperSize.a4).rawValue
        }
        for key in ["created", "modified"] {
            if let text = object[key] as? String {
                object[key] = PaperDate.normalize(text)
            }
        }
        if object["objects"] == nil { object["objects"] = [] }
        let data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
        try AtomicFile.write(data, to: paths.pageJSON(notebookPath, pageID))
        return try? PaperJSON.decode(Page.self, from: data)
    }

    /// Whether a page the server has no size name for is one of its notebook's
    /// infinite pages: the notebook makes infinite pages (its `notebook.json`,
    /// which a pull writes before any of its pages), the page is not bound to
    /// a PDF, and it is at least as big as an infinite page starts. An A4 or a
    /// PDF page in such a notebook stays what its dimensions say.
    private func notebookMakesInfinitePages(
        _ notebookPath: String, width: Double, height: Double, page object: [String: Any]
    ) -> Bool {
        guard !(object["pdf"] is [String: Any]) else { return false }
        let base = PaperSize.infinite.points
        guard width > Double(base.width) - 1, height > Double(base.height) - 1 else { return false }
        let notebook = try? AtomicFile.readJSON(Notebook.self, at: paths.notebookJSON(notebookPath))
        return notebook?.paperSize == .infinite
    }

    /// Creates or updates the local `notebook.json` from a server manifest, and
    /// reindexes. Page order comes from the server; page content does not.
    public func applyRemoteNotebook(_ manifest: NotebookManifest) throws {
        writeLock.lock()
        defer { writeLock.unlock() }
        let path = manifest.path
        guard PaperPaths.isSafeNotebookPath(path) else {
            throw PaperStoreError.unsafePath(path)
        }
        try FileManager.default.createDirectory(
            at: paths.url(forVaultPath: path), withIntermediateDirectories: true)

        var notebook =
            (try? AtomicFile.readJSON(Notebook.self, at: paths.notebookJSON(path)))
            ?? Notebook(
                id: manifest.id ?? PaperID.make(),
                title: manifest.title ?? PaperStore.title(ofNotebookPath: path), path: path)
        notebook.path = path
        if let id = manifest.id { notebook.id = id }
        if let title = manifest.title { notebook.title = title }
        if let template = manifest.template.flatMap(Template.init(rawValue:)) {
            notebook.template = template
        }
        if let size = manifest.paperSize.flatMap(PaperSize.init(rawValue:)) {
            notebook.paperSize = size
        }
        notebook.cover = manifest.cover
        if let tags = manifest.tags { notebook.tags = tags }
        if let favorite = manifest.favorite { notebook.favorite = favorite }
        if let seq = manifest.seq { notebook.seq = seq }
        if let modified = manifest.modified.flatMap(PaperDate.parse) { notebook.modified = modified }
        if let created = manifest.created.flatMap(PaperDate.parse) { notebook.created = created }

        // Never drop a page that exists here: the same rule the server applies
        // to a client's ordering, applied in the other direction.
        if let order = manifest.pages {
            var pages = order
            for id in notebook.pages where !pages.contains(id) { pages.append(id) }
            notebook.pages = pages
        }
        try AtomicFile.writeJSON(notebook, to: paths.notebookJSON(path))
    }

    /// Reindexes one notebook after a pull touched its files. Safe to call for
    /// a path that no longer exists.
    public func reindex(notebookPath: String) {
        try? indexNotebook(at: notebookPath)
    }

    /// The page this device wrote lost a race, so it becomes the sibling the
    /// server made for it: the directory is renamed, the record renamed with
    /// it, and it is listed straight after the page it forked from.
    ///
    /// Nothing is deleted. The original comes back from the server separately.
    public func adoptConflictSibling(
        notebookPath: String, originalID: String, sibling: RemotePage
    ) throws {
        writeLock.lock()
        defer { writeLock.unlock() }
        let manager = FileManager.default
        let source = paths.pageDirectory(notebookPath, originalID)
        let destination = paths.pageDirectory(notebookPath, sibling.id)
        guard PaperID.isValid(sibling.id) else {
            throw PaperStoreError.unsafePath(sibling.id)
        }
        if manager.fileExists(atPath: destination.path(percentEncoded: false)) {
            try manager.removeItem(at: destination)
        }
        if manager.fileExists(atPath: source.path(percentEncoded: false)) {
            try manager.moveItem(at: source, to: destination)
        } else {
            try manager.createDirectory(at: destination, withIntermediateDirectories: true)
        }

        var record =
            (try? AtomicFile.readJSON(Page.self, at: paths.pageJSON(notebookPath, sibling.id)))
            ?? Page(id: sibling.id)
        record.id = sibling.id
        record.conflictOf = originalID
        record.revision = sibling.revision
        record.renderRevision = sibling.renderRevision
        record.textRevision = sibling.textRevision
        if let seq = sibling.seq { record.seq = seq }
        try AtomicFile.writeJSON(record, to: paths.pageJSON(notebookPath, sibling.id))

        if var notebook = try? notebook(path: notebookPath) {
            var order = notebook.pages.filter { $0 != sibling.id }
            if let index = order.firstIndex(of: originalID) {
                order.insert(sibling.id, at: index + 1)
            } else {
                order.append(sibling.id)
            }
            notebook.pages = order
            if notebook.cover == originalID { notebook.cover = nil }
            try AtomicFile.writeJSON(notebook, to: paths.notebookJSON(notebookPath))
        }
        // Deliberately no reindex here: the original's directory has just been
        // renamed away and its replacement has not arrived from the server yet,
        // so a reconcile at this instant would see the original as a page that
        // no longer exists and reorder the notebook around its absence. The
        // caller reindexes once both halves are on disk.
    }

    // MARK: - Applying a remote delete

    /// Mirrors a server-side delete into the device trash.
    ///
    /// The bytes are moved, not unlinked, and no op is journalled: this delete
    /// already happened upstream, and pushing it back would be an echo. The
    /// mirror is what makes "it vanished from my iPad" recoverable for the
    /// thirty days the server keeps its own copy.
    @discardableResult
    public func applyRemoteDelete(path: String, kind: TrashToken.Kind) throws -> TrashToken? {
        writeLock.lock()
        defer { writeLock.unlock() }
        let manager = FileManager.default
        let notebookPath: String
        let pageID: String?
        switch kind {
        case .notebook:
            notebookPath = path
            pageID = nil
        case .page:
            let parts = path.components(separatedBy: "/pages/")
            guard parts.count == 2 else { return nil }
            notebookPath = parts[0]
            pageID = parts[1]
        }

        let source =
            pageID.map { paths.pageDirectory(notebookPath, $0) }
            ?? paths.url(forVaultPath: notebookPath)
        guard manager.fileExists(atPath: source.path(percentEncoded: false)) else { return nil }

        let notebook = try? notebook(path: notebookPath)
        let position = pageID.flatMap { id in notebook?.pages.firstIndex(of: id) }
        let token = TrashToken(
            id: PaperID.make(), kind: kind,
            title: notebook?.title ?? PaperStore.title(ofNotebookPath: notebookPath))
        let entry = paths.trash.appending(path: token.id, directoryHint: .isDirectory)
        try manager.createDirectory(at: entry, withIntermediateDirectories: true)
        try AtomicFile.writeJSON(
            TrashManifest(
                kind: kind == .notebook ? .notebook : .page, notebookPath: notebookPath,
                notebookID: notebook?.id ?? "", pageID: pageID, position: position,
                deletedAt: Date()),
            to: entry.appending(path: "manifest.json", directoryHint: .notDirectory))
        try manager.moveItem(
            at: source,
            to: entry.appending(
                path: pageID == nil ? "package" : "page", directoryHint: .isDirectory))

        if let pageID, var notebook {
            notebook.pages.removeAll { $0 == pageID }
            if notebook.cover == pageID { notebook.cover = nil }
            try AtomicFile.writeJSON(notebook, to: paths.notebookJSON(notebookPath))
            try? indexNotebook(at: notebookPath)
        } else {
            try database.queue.write { db in
                try db.execute(
                    sql: "DELETE FROM notebooks WHERE path = ?", arguments: [notebookPath])
            }
        }
        return token
    }

    /// `paper/Work/Ideas.paper` -> `Ideas`.
    static func title(ofNotebookPath path: String) -> String {
        let stem = path.split(separator: "/").last.map(String.init) ?? path
        return stem.hasSuffix(".paper") ? String(stem.dropLast(".paper".count)) : stem
    }
}

/// The one place that knows the server writes ISO timestamps with microsecond
/// precision while `PaperJSON` writes them with milliseconds.
///
/// `ISO8601DateFormatter` refuses six fractional digits, so a page pulled from
/// the server and written verbatim would be a page this device could no longer
/// read. Normalising on the way in costs one string edit and removes a whole
/// class of "the notebook came back empty".
public enum PaperDate {
    nonisolated(unsafe) private static let fractional: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    nonisolated(unsafe) private static let whole: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter
    }()

    public static func parse(_ text: String) -> Date? {
        let normalized = normalize(text)
        return fractional.date(from: normalized) ?? whole.date(from: normalized)
    }

    public static func normalize(_ text: String) -> String {
        guard let dot = text.firstIndex(of: ".") else { return text }
        let after = text.index(after: dot)
        var digits = after
        while digits < text.endIndex, text[digits].isNumber {
            digits = text.index(after: digits)
        }
        let fraction = text[after..<digits]
        guard fraction.count != 3 else { return text }
        let trimmed = String(fraction.prefix(3)).padding(
            toLength: 3, withPad: "0", startingAt: 0)
        return String(text[text.startIndex..<after]) + trimmed + String(text[digits...])
    }
}
