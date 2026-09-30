import CoreGraphics
import Foundation
import UniformTypeIdentifiers

public enum PDFExportError: Error, Equatable, Sendable {
    case noPages
    case contextUnavailable
}

/// Export: a notebook, or one page, as a file someone else can open.
///
/// The composition is `PageRenderer.compose`, the same function the on-screen
/// render uses, so an export is what the page looks like rather than a second
/// interpretation of it. An imported PDF background is drawn as vectors, which
/// keeps its text selectable in the exported file; ink is rasterised at 3x,
/// within the renderer's pixel budget, because `PKDrawing` has no vector
/// output, and the `.pkdrawing` source is never touched by any of this.
public enum PDFExporter {
    /// Ink is composited at this scale. Three times is past the point where an
    /// A4 page printed at 300dpi shows the raster.
    public static let inkScale: CGFloat = 3

    /// One PDF holding every page in order.
    public static func pdf(_ jobs: [PageRenderJob]) throws -> Data {
        guard !jobs.isEmpty else { throw PDFExportError.noPages }
        let data = NSMutableData()
        guard let consumer = CGDataConsumer(data: data as CFMutableData) else {
            throw PDFExportError.contextUnavailable
        }
        var firstBox = CGRect(origin: .zero, size: jobs[0].pointSize)
        guard let context = CGContext(consumer: consumer, mediaBox: &firstBox, nil) else {
            throw PDFExportError.contextUnavailable
        }

        for job in jobs {
            let size = job.pointSize
            guard size.width > 0, size.height > 0 else { continue }
            // Each page's ink raster goes before the next one is made, rather
            // than every page's piling up until the export returns.
            autoreleasepool {
                var box = CGRect(origin: .zero, size: size)
                // The documented representation of a per-page media box is a
                // CFData holding the CGRect itself, which also keeps this file
                // free of the NSValue spelling differences between the platforms.
                let boxData = withUnsafeBytes(of: &box) { Data($0) } as CFData
                context.beginPDFPage([kCGPDFContextMediaBox as String: boxData] as CFDictionary)
                context.saveGState()
                // Page space is y-down from the top-left; PDF space is y-up.
                context.translateBy(x: 0, y: size.height)
                context.scaleBy(x: 1, y: -1)
                // The ink is a raster like any render, so it keeps the same
                // budget: 3x of a long infinite page is hundreds of megapixels.
                PageRenderer.compose(
                    job, in: context, inkScale: PageRenderer.cappedScale(inkScale, for: size))
                context.restoreGState()
                context.endPDFPage()
            }
        }
        context.closePDF()
        return data as Data
    }

    /// A single page as a PNG, which is what "export this page" means when the
    /// notebook has one page or the user picked one.
    public static func png(_ job: PageRenderJob, scale: CGFloat = inkScale) throws -> Data {
        // Never uploaded, so the server's byte cap does not apply.
        try PageRenderer.renderPNG(job, scale: scale, maximumBytes: nil)
    }

    // MARK: - From the store

    /// Render jobs for a whole notebook, in page order.
    public static func jobs(forNotebook notebook: Notebook, store: PaperStore) -> [PageRenderJob] {
        let pages = (try? store.pages(ofNotebook: notebook.id)) ?? []
        return pages.map { job(for: $0, notebook: notebook, store: store) }
    }

    public static func job(for page: Page, notebook: Notebook, store: PaperStore) -> PageRenderJob {
        PageRenderJob(
            notebookPath: notebook.path, page: page,
            drawing: store.drawingData(notebookPath: notebook.path, pageID: page.id),
            vaultURL: store.paths.vault)
    }

    /// Writes `data` into a temporary file named after the notebook, which is
    /// what the share sheet hands to Files, Mail or another app.
    public static func temporaryFile(named name: String, extension ext: String, data: Data) throws
        -> URL
    {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "PaperExport", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let stem = PaperPaths.slug(name)
        let url = directory.appending(
            path: "\(stem.isEmpty ? "Paper" : stem).\(ext)", directoryHint: .notDirectory)
        try data.write(to: url, options: .atomic)
        return url
    }
}
