import Foundation
import PencilKit

extension PaperStore {
    /// A copy of a page or a board, placed right after it.
    ///
    /// Everything the page holds comes along -- ink, objects, the PDF page it
    /// annotates, template, colour, size and extent -- under a new id, with no
    /// revision and no bookmark. A title becomes "<title> copy". The render and
    /// the recognised text are copied too, so the copy has a thumbnail and is
    /// searchable before anyone writes on it. One create-PUT is journalled,
    /// which carries the ink with it.
    @discardableResult
    public func duplicatePage(notebookID: String, pageID: String) throws -> Page {
        writeLock.lock()
        defer { writeLock.unlock() }
        let notebook = try notebook(id: notebookID)
        guard notebook.pages.contains(pageID) else { throw PaperStoreError.notFound(pageID) }
        let source = try page(notebookPath: notebook.path, id: pageID)

        var copy = source
        copy.id = PaperID.make()
        copy.title = source.title.map { "\($0) copy" }
        copy.bookmarked = false
        copy.created = Date()
        copy.modified = copy.created
        copy.revision = 0
        copy.renderRevision = 0
        copy.textRevision = 0
        copy.conflictOf = nil
        copy.seq = 0

        // The ink first: the create-PUT that `insertPage` journals reads it
        // off the disk when it is pushed.
        if let ink = drawingData(notebookPath: notebook.path, pageID: pageID) {
            try writeDrawing(ink, notebookPath: notebook.path, pageID: copy.id)
        }
        for derived in [paths.render, paths.text] {
            let from = derived(notebook.path, pageID)
            guard let data = try? Data(contentsOf: from) else { continue }
            try AtomicFile.write(data, to: derived(notebook.path, copy.id))
        }
        let inserted = try insertPage(copy, notebookID: notebookID, after: pageID)
        indexPageText(pageID: inserted.id, notebookPath: notebook.path)
        return inserted
    }

    /// Takes every stroke off a page. Objects, the template and the colour
    /// stay. The ink that was there goes into the page's snapshot ring first,
    /// so History can bring it back.
    ///
    /// For a page that has no live canvas. A canvas clears its own ink as an
    /// undo step and lets autosave write it; writing under a live canvas would
    /// be undone by its next flush.
    @discardableResult
    public func clearPage(notebookID: String, pageID: String) throws -> Page {
        writeLock.lock()
        defer { writeLock.unlock() }
        let notebook = try notebook(id: notebookID)
        guard notebook.pages.contains(pageID) else { throw PaperStoreError.notFound(pageID) }
        // A page with nothing on it is left alone: no snapshot, no write, no op.
        guard let ink = drawingData(notebookPath: notebook.path, pageID: pageID), !ink.isEmpty,
            (try? PKDrawing(data: ink))?.strokes.isEmpty != true
        else {
            return try page(notebookPath: notebook.path, id: pageID)
        }
        try SnapshotRing(paths: paths, database: database).push(ink, pageID: pageID)
        // An empty drawing rather than no bytes: every reader, here and on
        // the server, expects a drawing in the page's drawing part.
        try writeDrawing(PKDrawing().dataRepresentation(), notebookPath: notebook.path, pageID: pageID)
        let page = try updatePage(notebookPath: notebook.path, pageID: pageID) { page in
            page.modified = Date()
        }
        try journal.append(
            .init(kind: .pagePut, notebookPath: notebook.path, pageID: pageID,
                payload: ["metadata": "clear"]))
        return page
    }
}
