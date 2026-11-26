import PaperCore
import UIKit
import XCTest

@testable import PaperUI

/// The library asks for a bootstrap on every switch into Paper, and export and
/// PDF import used to run on the main actor. The work now happens off it; these
/// pin what it still has to get right.
@MainActor
final class PaperControllerIOTests: XCTestCase {
    // MARK: - Bootstrap

    func testBootstrapWaitsForTheTreeBeforeMakingAFirstNotebook() async throws {
        let seed = try makeStore()
        let existing = try seed.createNotebook(title: "Kept")
        // An index that lost its rows while the tree kept the notebook: an empty
        // database over the same files. (This target does not link GRDB, so the
        // rows cannot be deleted in place.)
        let store = try PaperStore(paths: seed.paths, database: try PaperDatabase())
        let controller = PaperController(store: store)

        let created = await controller.bootstrapIfNeeded()

        XCTAssertNil(created)
        XCTAssertEqual(controller.notebooks.map(\.id), [existing.id])
    }

    func testBootstrapOnAnEmptyStoreMakesExactlyOneFirstNotebook() async throws {
        let controller = PaperController(store: try makeStore())

        // Two windows opening Paper at once on a first launch.
        async let first = controller.bootstrapIfNeeded()
        async let second = controller.bootstrapIfNeeded()
        let created = await [first, second].compactMap { $0 }

        XCTAssertEqual(created.count, 1)
        XCTAssertEqual(controller.notebooks.map(\.title), ["Notebook 1"])
        let again = await controller.bootstrapIfNeeded()
        XCTAssertNil(again, "later appearances create nothing")
        XCTAssertNil(controller.bootstrap(), "nor does the synchronous form")
        XCTAssertEqual(controller.notebooks.count, 1)
    }

    // MARK: - Import and export

    func testImportsRunOneAfterAnotherAndKeepEveryPage() async throws {
        let store = try makeStore()
        let notebook = try store.createNotebook(title: "Imports")
        let controller = PaperController(store: store)
        let pdf = try XCTUnwrap(Self.pdf(pages: 3))

        async let first = controller.importPDF(
            data: pdf, name: "a.pdf", into: notebook, after: notebook.pages.first)
        async let second = controller.importPDF(
            data: pdf, name: "b.pdf", into: notebook, after: notebook.pages.first)
        let results = await [first, second]

        XCTAssertEqual(results.compactMap { $0?.pages.count }, [3, 3])
        let fresh = try store.notebook(id: notebook.id)
        XCTAssertEqual(fresh.pages.count, 7)
        let onDisk = try AtomicFile.readJSON(
            Notebook.self, at: store.paths.notebookJSON(notebook.path))
        XCTAssertEqual(onDisk.pages, fresh.pages, "the page order on disk kept every import")
        XCTAssertEqual(controller.notebooks.first?.pages.count, 7, "the library was refreshed")
    }

    func testExportsProduceFiles() async throws {
        let store = try makeStore()
        let notebook = try store.createNotebook(title: "Export", pageCount: 2)
        let controller = PaperController(store: store)

        let pdfURL = await controller.exportPDF(notebook: notebook)
        let pdf = try Data(contentsOf: try XCTUnwrap(pdfURL))
        XCTAssertEqual(pdf.prefix(4), Data("%PDF".utf8))

        let page = try XCTUnwrap(try store.pages(ofNotebook: notebook.id).first)
        let pngURL = await controller.exportPNG(page: page, notebook: notebook)
        let png = try Data(contentsOf: try XCTUnwrap(pngURL))
        XCTAssertEqual(png.prefix(4), Data([0x89, 0x50, 0x4E, 0x47]))
    }

    // MARK: - Helpers

    private func makeStore() throws -> PaperStore {
        let root = FileManager.default.temporaryDirectory
            .appending(path: "PaperControllerIOTests-\(UUID().uuidString)", directoryHint: .isDirectory)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }
        return try PaperStore(root: root)
    }

    private static func pdf(pages: Int) -> Data? {
        let renderer = UIGraphicsPDFRenderer(bounds: CGRect(x: 0, y: 0, width: 300, height: 400))
        let data = renderer.pdfData { context in
            for _ in 0..<pages { context.beginPage() }
        }
        return data.isEmpty ? nil : data
    }
}
