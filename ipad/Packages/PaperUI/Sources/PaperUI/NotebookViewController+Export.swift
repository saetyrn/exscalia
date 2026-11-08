import PaperCore
import UIKit

// MARK: - Export

extension NotebookViewController {
    /// Shares the document as a PDF -- every page, or only the one showing --
    /// or a one-page notebook's page as a PNG, as Export always has.
    func presentExport(_ scope: ExportScope = .all, from source: UIView) {
        guard !isExporting else { return }
        let notebook = session.notebook
        let current = session.pages.indices.contains(session.currentIndex)
            ? session.pages[session.currentIndex] : nil
        if scope == .currentPage, current == nil { return }
        isExporting = true
        UIAccessibility.post(notification: .announcement, argument: "Exporting")
        let single = session.pages.count == 1 ? session.pages.first : nil
        Task { @MainActor [weak self] in
            guard let self else { return }
            let url: URL?
            switch scope {
            case .currentPage:
                if let current {
                    url = await self.exportPagePDF(page: current, notebook: notebook)
                } else {
                    url = nil
                }
            case .all:
                if let single {
                    url = await self.controller.exportPNG(page: single, notebook: notebook)
                } else {
                    url = await self.controller.exportPDF(notebook: notebook)
                }
            }
            self.isExporting = false
            guard let url else {
                UIAccessibility.post(notification: .announcement, argument: "Export failed")
                return
            }
            // The notebook may have been closed while the pages rendered.
            guard self.viewIfLoaded?.window != nil, self.presentedViewController == nil else {
                return
            }
            let activity = UIActivityViewController(
                activityItems: [url], applicationActivities: nil)
            activity.popoverPresentationController?.sourceView = source
            activity.popoverPresentationController?.sourceRect = source.bounds
            self.present(activity, animated: !UIAccessibility.isReduceMotionEnabled)
        }
    }

    /// One page as a one-page PDF, named after the document and the page. The
    /// flush comes first so the export has the last stroke; rendering runs off
    /// the main actor.
    private func exportPagePDF(page: Page, notebook: Notebook) async -> URL? {
        controller.autosave.flushAll()
        let store = controller.store
        let fresh = (try? store.page(notebookPath: notebook.path, id: page.id)) ?? page
        let number = (session.pages.firstIndex(where: { $0.id == page.id }) ?? 0) + 1
        let label = (fresh.title ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let name =
            "\(notebook.title) "
            + (label.isEmpty
                ? (notebook.kind == .whiteboard ? "board \(number)" : "page \(number)") : label)
        return await Task.detached(priority: .userInitiated) {
            guard
                let data = try? PDFExporter.pdf([
                    PDFExporter.job(for: fresh, notebook: notebook, store: store)
                ])
            else { return nil }
            return try? PDFExporter.temporaryFile(named: name, extension: "pdf", data: data)
        }.value
    }
}
