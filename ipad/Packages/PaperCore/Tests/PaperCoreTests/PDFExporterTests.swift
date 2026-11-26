import CoreGraphics
import Foundation
import PDFKit
import XCTest

@testable import PaperCore

final class PDFExporterTests: XCTestCase {
    private var root: URL!
    private var store: PaperStore!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory
            .appending(path: "PDFExporterTests-\(UUID().uuidString)", directoryHint: .isDirectory)
        store = try PaperStore(root: root)
    }

    override func tearDownWithError() throws {
        store = nil
        try? FileManager.default.removeItem(at: root)
    }

    func testEveryPageIsExportedAtItsOwnSize() throws {
        let notebook = try store.createNotebook(title: "Export", pageCount: 2)
        let jobs = PDFExporter.jobs(forNotebook: try store.notebook(id: notebook.id), store: store)
        XCTAssertEqual(jobs.count, 2)

        let pdf = try PDFExporter.pdf(jobs)
        let document = try XCTUnwrap(PDFDocument(data: pdf))
        XCTAssertEqual(document.pageCount, 2)
        for index in 0..<document.pageCount {
            let bounds = try XCTUnwrap(document.page(at: index)).bounds(for: .mediaBox)
            XCTAssertEqual(bounds.width, PaperSize.a4.points.width, accuracy: 1)
            XCTAssertEqual(bounds.height, PaperSize.a4.points.height, accuracy: 1)
        }
    }

    func testTextObjectsStayTextInTheExportedFile() throws {
        let notebook = try store.createNotebook(title: "Export")
        let pageID = try XCTUnwrap(notebook.pages.first)
        _ = try store.setObjects(
            [
                PageObject(
                    type: .text, frame: PageObject.Frame(x: 60, y: 80, width: 400, height: 120),
                    text: "Quarterly review", fontSize: 22)
            ], notebookPath: notebook.path, pageID: pageID)

        let pdf = try PDFExporter.pdf(
            PDFExporter.jobs(forNotebook: try store.notebook(id: notebook.id), store: store))
        let document = try XCTUnwrap(PDFDocument(data: pdf))
        let text = try XCTUnwrap(document.page(at: 0)?.string)
        XCTAssertTrue(text.contains("Quarterly review"), text)
    }

    func testAnImportedPDFKeepsItsSelectableTextThroughTheExport() throws {
        let notebook = try store.createNotebook(title: "Export")
        // Generated PDFs carry a creation date, so the byte-for-byte check
        // below compares against the bytes that were actually imported.
        let source = try PDFFixture.threePages()
        let result = try PDFImporter(store: store).importPDF(
            data: source, name: "source.pdf", into: notebook.id)
        // Annotate the second imported page.
        let annotated = result.pages[1]
        try store.writeDrawing(
            PageRendererObjectFixtures.strokeData(), notebookPath: notebook.path,
            pageID: annotated.id)

        let refreshed = try store.notebook(id: notebook.id)
        let pdf = try PDFExporter.pdf(PDFExporter.jobs(forNotebook: refreshed, store: store))
        let document = try XCTUnwrap(PDFDocument(data: pdf))
        // One blank page from `createNotebook`, plus the three imported ones.
        XCTAssertEqual(document.pageCount, 4)
        let text = try XCTUnwrap(document.page(at: 2)?.string)
        XCTAssertTrue(text.contains("Beta"), text)

        // The stored PDF is still byte-for-byte what was imported.
        XCTAssertEqual(store.assets.data(forAssetPath: result.assetPath), source)
    }

    func testASinglePageExportsAsAPNGAtThreeTimesSize() throws {
        let notebook = try store.createNotebook(title: "Export")
        let page = try store.page(
            notebookPath: notebook.path, id: try XCTUnwrap(notebook.pages.first))
        let png = try PDFExporter.png(
            PDFExporter.job(for: page, notebook: notebook, store: store))
        let source = try XCTUnwrap(CGImageSourceCreateWithData(png as CFData, nil))
        let image = try XCTUnwrap(CGImageSourceCreateImageAtIndex(source, 0, nil))
        XCTAssertEqual(image.width, Int(PaperSize.a4.points.width * PDFExporter.inkScale))
        XCTAssertEqual(image.height, Int(PaperSize.a4.points.height * PDFExporter.inkScale))
    }

    /// 3x of a long infinite page would ask PencilKit for a 3072x72000 picture.
    /// The ink raster keeps the renderer's budget instead, and the page keeps
    /// its real size.
    func testALongInfinitePageExportsItsInkWithinThePixelBudget() throws {
        let size = CGSize(width: 1024, height: 24000)
        let job = PageRenderJob(
            notebookPath: "paper/A.paper", pageID: "p1", template: .ruled, size: .infinite,
            drawing: PageRendererObjectFixtures.strokeData(), customSize: SizePoints(size))

        let pdf = try PDFExporter.pdf([job])
        let document = try XCTUnwrap(PDFDocument(data: pdf))
        XCTAssertEqual(document.pageCount, 1)
        let bounds = try XCTUnwrap(document.page(at: 0)).bounds(for: .mediaBox)
        XCTAssertEqual(bounds.width, size.width, accuracy: 1)
        XCTAssertEqual(bounds.height, size.height, accuracy: 1)

        // The ink is the page's one raster image.
        let images = try imageSizes(onFirstPageOf: pdf)
        XCTAssertFalse(images.isEmpty)
        for image in images {
            XCTAssertLessThanOrEqual(max(image.width, image.height), PageRenderer.maximumPixelEdge)
            XCTAssertLessThanOrEqual(image.width * image.height, PageRenderer.maximumPixelArea)
        }
    }

    /// Pixel sizes of the image XObjects the first page draws.
    private func imageSizes(onFirstPageOf pdf: Data) throws -> [CGSize] {
        let provider = try XCTUnwrap(CGDataProvider(data: pdf as CFData))
        let page = try XCTUnwrap(CGPDFDocument(provider)?.page(at: 1))
        let dictionary = try XCTUnwrap(page.dictionary)
        var resources: CGPDFDictionaryRef?
        var xobjects: CGPDFDictionaryRef?
        guard CGPDFDictionaryGetDictionary(dictionary, "Resources", &resources), let resources,
            CGPDFDictionaryGetDictionary(resources, "XObject", &xobjects), let xobjects
        else { return [] }
        final class Found { var sizes: [CGSize] = [] }
        let found = Found()
        CGPDFDictionaryApplyBlock(
            xobjects,
            { _, object, _ in
                var stream: CGPDFStreamRef?
                var subtype: UnsafePointer<CChar>?
                var width: CGPDFInteger = 0
                var height: CGPDFInteger = 0
                if CGPDFObjectGetValue(object, .stream, &stream), let stream,
                    let info = CGPDFStreamGetDictionary(stream),
                    CGPDFDictionaryGetName(info, "Subtype", &subtype), let subtype,
                    String(cString: subtype) == "Image",
                    CGPDFDictionaryGetInteger(info, "Width", &width),
                    CGPDFDictionaryGetInteger(info, "Height", &height)
                {
                    found.sizes.append(CGSize(width: width, height: height))
                }
                return true
            }, nil)
        return found.sizes
    }

    func testAnExportWithNoPagesIsRefusedRatherThanEmpty() {
        XCTAssertThrowsError(try PDFExporter.pdf([])) { error in
            XCTAssertEqual(error as? PDFExportError, .noPages)
        }
    }

    func testTheTemporaryFileIsNamedAfterTheNotebook() throws {
        let url = try PDFExporter.temporaryFile(
            named: "Work / Plans", extension: "pdf", data: Data("%PDF-1.7".utf8))
        defer { try? FileManager.default.removeItem(at: url) }
        XCTAssertEqual(url.pathExtension, "pdf")
        XCTAssertFalse(url.lastPathComponent.contains("/"))
        XCTAssertTrue(FileManager.default.fileExists(atPath: url.path(percentEncoded: false)))
    }
}
