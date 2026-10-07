import PaperCore
import UIKit

/// Objects, assets and export, hung off the controller.
///
/// A separate file so the M3 sync work and this one never touch the same lines:
/// everything here is additive and goes through the same `markPageDirty` seam
/// the ink uses, which is what makes an object edit reach `page.json` on the
/// same one-second debounce as a stroke.
extension PaperController {
    /// Writes a page's objects and marks the page dirty so autosave flushes it.
    public func setObjects(_ objects: [PageObject], on ref: PageRef, source: UUID? = nil) {
        _ = try? store.setObjects(objects, notebookPath: ref.notebookPath, pageID: ref.pageID)
        markPageDirty(ref, source: source)
    }

    /// Stores image or PDF bytes under their content hash and returns the
    /// vault-relative path an object references.
    public func storeAsset(_ data: Data, name: String, notebookPath: String) -> String? {
        try? store.storeAsset(data, preferredName: name, notebookPath: notebookPath)
    }

    /// `notebookPath` is the notebook the page is in now, which finds the
    /// asset again after a rename, move or duplicate.
    public func assetImage(_ assetPath: String, notebookPath: String? = nil) -> UIImage? {
        guard
            let data = store.assets.data(forAssetPath: assetPath, inNotebook: notebookPath)
        else { return nil }
        return UIImage(data: data)
    }

    public func assetURL(_ assetPath: String, notebookPath: String? = nil) -> URL {
        store.assets.url(forAssetPath: assetPath, inNotebook: notebookPath)
    }

    // MARK: - Import

    /// Imports a PDF, appending one page per PDF page after `pageID`. The asset
    /// hash and one page write per PDF page run off the main actor, so a long
    /// document does not stop the pen.
    @discardableResult
    public func importPDF(data: Data, name: String, into notebook: Notebook, after pageID: String?)
        async -> PDFImportResult?
    {
        let notebookID = notebook.id
        return await runImport { importer in
            try importer.importPDF(data: data, name: name, into: notebookID, after: pageID)
        }
    }

    @discardableResult
    public func importPDF(at url: URL, into notebook: Notebook, after pageID: String?)
        async -> PDFImportResult?
    {
        let notebookID = notebook.id
        return await runImport { importer in
            try importer.importPDF(at: url, into: notebookID, after: pageID)
        }
    }

    private func runImport(
        _ work: @escaping @Sendable (PDFImporter) throws -> PDFImportResult
    ) async -> PDFImportResult? {
        let importer = PDFImporter(store: store)
        let previous = pdfImportTail
        let task = Task.detached(priority: .userInitiated) { () -> PDFImportResult? in
            _ = await previous?.value
            return try? work(importer)
        }
        pdfImportTail = task
        let result = await task.value
        refresh()
        return result
    }

    // MARK: - Export

    /// The whole notebook as a PDF in a temporary file, ready for the share
    /// sheet. Everything unsaved is flushed first: an export that misses the
    /// last sentence is worse than no export. The flush reads the live
    /// canvases, so it stays here; rendering every page runs off the main
    /// actor.
    public func exportPDF(notebook: Notebook) async -> URL? {
        autosave.flushAll()
        guard let fresh = try? store.notebook(id: notebook.id) else { return nil }
        let store = store
        return await Task.detached(priority: .userInitiated) {
            guard
                let data = try? PDFExporter.pdf(PDFExporter.jobs(forNotebook: fresh, store: store))
            else { return nil }
            return try? PDFExporter.temporaryFile(named: fresh.title, extension: "pdf", data: data)
        }.value
    }

    /// One page as a PNG, which is what a single-page notebook exports as.
    public func exportPNG(page: Page, notebook: Notebook) async -> URL? {
        autosave.flushAll()
        // Re-read after the flush: the caller's copy predates whatever was just
        // written, and an export that misses the last object is a bug report.
        let fresh = (try? store.page(notebookPath: notebook.path, id: page.id)) ?? page
        let store = store
        return await Task.detached(priority: .userInitiated) {
            guard
                let data = try? PDFExporter.png(
                    PDFExporter.job(for: fresh, notebook: notebook, store: store))
            else { return nil }
            return try? PDFExporter.temporaryFile(
                named: "\(notebook.title) page", extension: "png", data: data)
        }.value
    }
}
