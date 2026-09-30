import CoreGraphics
import Foundation
import PDFKit

public enum PDFImportError: Error, Equatable, Sendable {
    case unreadable
    case empty
    case notFound(String)
}

/// What an import produced: the stored PDF and the pages that now annotate it.
public struct PDFImportResult: Equatable, Sendable {
    /// Vault-relative path of the stored PDF, whose bytes are never rewritten.
    public let assetPath: String
    public let pages: [Page]

    public init(assetPath: String, pages: [Page]) {
        self.assetPath = assetPath
        self.pages = pages
    }
}

/// Turns a PDF into pages.
///
/// The document itself is stored untouched as an asset and every page becomes a
/// `Page` bound to it by index, so annotation is ink in the page's own
/// `PKDrawing` and the original file keeps its hash for as long as it exists.
public struct PDFImporter: Sendable {
    private let store: PaperStore

    public init(store: PaperStore) {
        self.store = store
    }

    public func importPDF(at url: URL, into notebookID: String, after pageID: String? = nil) throws
        -> PDFImportResult
    {
        // A document picked from another app arrives security-scoped.
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        guard let data = try? Data(contentsOf: url) else { throw PDFImportError.unreadable }
        return try importPDF(
            data: data, name: url.lastPathComponent, into: notebookID, after: pageID)
    }

    public func importPDF(
        data: Data, name: String, into notebookID: String, after pageID: String? = nil
    ) throws -> PDFImportResult {
        guard let document = PDFDocument(data: data), document.pageCount > 0 else {
            throw PDFImportError.empty
        }
        let notebook = try store.notebook(id: notebookID)
        let assetPath = try store.storeAsset(
            data, preferredName: name.isEmpty ? "document.pdf" : name,
            notebookPath: notebook.path)

        var created: [Page] = []
        var after = pageID
        for index in 0..<document.pageCount {
            let size = Self.pageSize(document.page(at: index))
            var page = Page(
                template: .blank,
                size: PaperSize.nearest(to: size),
                objects: [],
                pdf: PDFBinding(asset: assetPath, pageIndex: index),
                customSize: SizePoints(size))
            page = try store.insertPage(page, notebookID: notebookID, after: after)
            after = page.id
            created.append(page)
        }
        return PDFImportResult(assetPath: assetPath, pages: created)
    }

    /// The media box, turned the way the page is rotated. A quarter-turned page
    /// imported at its unrotated size would be written on sideways.
    public static func pageSize(_ page: PDFPage?) -> CGSize {
        guard let page else { return PaperSize.a4.points }
        let bounds = page.bounds(for: .mediaBox)
        guard bounds.width > 1, bounds.height > 1 else { return PaperSize.a4.points }
        let size = page.rotation % 180 == 0
            ? bounds.size
            : CGSize(width: bounds.height, height: bounds.width)
        return CGSize(width: size.width.rounded(), height: size.height.rounded())
    }
}

extension PaperSize {
    /// The preset an arbitrary size is closest to, used as the compatibility
    /// value beside a page's exact media box.
    public static func nearest(to size: CGSize) -> PaperSize {
        allCases.min(by: { first, second in
            distance(first.points, size) < distance(second.points, size)
        }) ?? .a4
    }

    private static func distance(_ lhs: CGSize, _ rhs: CGSize) -> CGFloat {
        abs(lhs.width - rhs.width) + abs(lhs.height - rhs.height)
    }
}
