import Foundation
import PaperCore

extension PaperController {
    // MARK: - Derived files

    /// Settings' "Reset local Paper cache". Removes every render, PDF page
    /// cache, cover and search row for this server and rebuilds them: the rows
    /// straight away from `text.json`, the renders in the background with each
    /// notebook's cover page first. Ink and transcripts are kept. Returns how
    /// many files were removed.
    @discardableResult
    public func resetDerivedFiles() throws -> Int {
        // Ink still held by a canvas has to be on disk before it is redrawn
        // from the disk.
        autosave.flushAll()
        let removed = try store.clearDerivedFiles(alsoPageFiles: [PDFBackgroundLayer.cacheName])

        var covers: [PageRenderJob] = []
        var others: [PageRenderJob] = []
        for notebook in (try? store.notebooks()) ?? [] {
            let pages = (try? store.pages(ofNotebook: notebook.id)) ?? []
            // The thumbnail is the first page, as `coverPage` has it.
            let coverID = pages.first?.id
            for page in pages {
                store.indexPageText(
                    pageID: page.id, notebookPath: notebook.path, textRevision: page.textRevision)
                let job = PageRenderJob(notebookPath: notebook.path, page: page)
                if page.id == coverID { covers.append(job) } else { others.append(job) }
            }
        }
        // The ink did not change, so nothing is journalled here: a page's
        // next flush sends its new render.
        let jobs = covers + others
        let store = store
        let renderer = renderer
        Task.detached(priority: .utility) {
            for var job in jobs {
                job.drawing = store.drawingData(notebookPath: job.notebookPath, pageID: job.pageID)
                await renderer.render(job)
            }
        }
        // The library's decoded thumbnails were of the files just removed.
        coverCache.removeAllObjects()
        refresh()
        return removed
    }

    // MARK: - Credentials

    /// The web view's cookies changed, or the server said the session is good.
    /// A sync engine that stopped at a 401 starts again now rather than at the
    /// next poll; a healthy one is left alone.
    public func credentialsMayHaveChanged() {
        guard let sync else { return }
        Task {
            if await sync.signedIn() { await sync.syncNow() }
        }
    }

    // MARK: - Trash

    /// The server's Paper trash. Nil when no server is configured; a request
    /// that fails throws, so an unreachable server is not mistaken for none.
    public func serverTrashItems() async throws -> [PaperTrashItem]? {
        guard let api else { return nil }
        return try await api.trash().items
    }
}
