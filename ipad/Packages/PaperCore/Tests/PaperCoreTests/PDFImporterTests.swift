import CoreGraphics
import Foundation
import PDFKit
import XCTest

@testable import PaperCore

final class PDFImporterTests: XCTestCase {
    private var root: URL!
    private var store: PaperStore!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory
            .appending(path: "PDFImporterTests-\(UUID().uuidString)", directoryHint: .isDirectory)
        store = try PaperStore(root: root)
    }

    override func tearDownWithError() throws {
        store = nil
        try? FileManager.default.removeItem(at: root)
    }

    func testAThreePageDocumentBecomesThreePagesAtItsOwnSizes() throws {
        let notebook = try store.createNotebook(title: "Imports")
        let data = try PDFFixture.threePages()
        let result = try PDFImporter(store: store).importPDF(
            data: data, name: "Report 2026.pdf", into: notebook.id,
            after: notebook.pages.first)

        XCTAssertEqual(result.pages.count, 3)
        XCTAssertEqual(
            result.pages.map(\.pointSize),
            [
                CGSize(width: 595, height: 842), CGSize(width: 612, height: 792),
                CGSize(width: 400, height: 400),
            ])
        for (index, page) in result.pages.enumerated() {
            XCTAssertEqual(page.pdf?.asset, result.assetPath)
            XCTAssertEqual(page.pdf?.pageIndex, index)
            XCTAssertEqual(page.template, .blank)
        }

        // Imported pages land after the page that was current, in order.
        let refreshed = try store.notebook(id: notebook.id)
        XCTAssertEqual(
            refreshed.pages, [notebook.pages[0]] + result.pages.map(\.id))
    }

    func testTheDocumentIsStoredVerbatimUnderItsHash() throws {
        let notebook = try store.createNotebook(title: "Imports")
        let data = try PDFFixture.threePages()
        let result = try PDFImporter(store: store).importPDF(
            data: data, name: "Report 2026.pdf", into: notebook.id)

        XCTAssertTrue(result.assetPath.hasPrefix("\(notebook.path)/assets/report-2026-"))
        XCTAssertEqual(store.assets.data(forAssetPath: result.assetPath), data)
        XCTAssertEqual(
            AssetStore.hash(try XCTUnwrap(store.assets.data(forAssetPath: result.assetPath))),
            AssetStore.hash(data))
    }

    func testAnnotatingAnImportedPageLeavesTheAssetUntouched() throws {
        let notebook = try store.createNotebook(title: "Imports")
        let data = try PDFFixture.threePages()
        let result = try PDFImporter(store: store).importPDF(
            data: data, name: "report.pdf", into: notebook.id)
        let before = try XCTUnwrap(store.assets.data(forAssetPath: result.assetPath))

        let page = try XCTUnwrap(result.pages.first)
        try store.writeDrawing(
            PageRendererObjectFixtures.strokeData(), notebookPath: notebook.path, pageID: page.id)

        let after = try XCTUnwrap(store.assets.data(forAssetPath: result.assetPath))
        XCTAssertEqual(AssetStore.hash(before), AssetStore.hash(after))
    }

    func testImportingTheSameFileTwiceReusesTheOneAsset() throws {
        let notebook = try store.createNotebook(title: "Imports")
        let data = try PDFFixture.threePages()
        let importer = PDFImporter(store: store)
        let first = try importer.importPDF(data: data, name: "report.pdf", into: notebook.id)
        let second = try importer.importPDF(data: data, name: "report.pdf", into: notebook.id)

        XCTAssertEqual(first.assetPath, second.assetPath)
        XCTAssertEqual(store.assets.assetPaths(forNotebook: notebook.path).count, 1)
        XCTAssertEqual(try store.notebook(id: notebook.id).pages.count, 7)
    }

    func testSomethingThatIsNotAPDFIsRefused() throws {
        let notebook = try store.createNotebook(title: "Imports")
        XCTAssertThrowsError(
            try PDFImporter(store: store).importPDF(
                data: Data("not a pdf".utf8), name: "x.pdf", into: notebook.id))
        XCTAssertEqual(try store.notebook(id: notebook.id).pages.count, 1)
    }

    func testTheNearestPresetIsRecordedBesideTheMediaBox() {
        XCTAssertEqual(PaperSize.nearest(to: CGSize(width: 596, height: 840)), .a4)
        XCTAssertEqual(PaperSize.nearest(to: CGSize(width: 612, height: 792)), .letter)
        XCTAssertEqual(PaperSize.nearest(to: CGSize(width: 1000, height: 1400)), .iPad)
    }
}
