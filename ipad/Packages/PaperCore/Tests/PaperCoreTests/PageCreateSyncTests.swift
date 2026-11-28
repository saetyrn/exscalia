import Foundation
import XCTest

@testable import PaperCore

/// A duplicate and a PDF import go up as page-creating PUTs: every page with
/// its ink and its binding, in the order the iPad shows, even when one of them
/// is written on before the queue drains.
final class PageCreateSyncTests: XCTestCase {
    private var root: URL!
    private var store: PaperStore!

    override func setUpWithError() throws {
        try super.setUpWithError()
        MockPaperServer.state.reset()
        root = FileManager.default.temporaryDirectory
            .appending(path: "PageCreateSyncTests-\(UUID().uuidString)", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        store = try PaperStore(root: root)
    }

    override func tearDownWithError() throws {
        store = nil
        if let root { try? FileManager.default.removeItem(at: root) }
        root = nil
        try super.tearDownWithError()
    }

    private func drain() async throws {
        let settings = try XCTUnwrap(ServerSettings(urlString: "https://paper.test"))
        let api = PaperAPI(
            settings: settings, auth: AnonymousAuthProvider(),
            session: MockPaperServer.makeSession())
        let engine = SyncEngine(
            store: store, api: api, clientID: "ipad-test-client", clock: TestClock(),
            jitter: { $0 })
        await engine.pushPending()
        XCTAssertEqual(try store.journal.pendingCount(), 0)
    }

    /// What autosave leaves behind after a stroke.
    private func write(_ ink: String, notebookPath: String, pageID: String) throws {
        try store.writeDrawing(Data(ink.utf8), notebookPath: notebookPath, pageID: pageID)
        try store.journal.append(
            .init(kind: .pagePut, notebookPath: notebookPath, pageID: pageID))
    }

    func testADuplicateArrivesWholeAndInOrderWhenItsFirstPageIsWrittenOnFirst() async throws {
        let source = try store.createNotebook(title: "Ideas", pageCount: 3)
        for (index, pageID) in source.pages.enumerated() {
            try store.writeDrawing(
                Data("ink \(index)".utf8), notebookPath: source.path, pageID: pageID)
        }
        let copy = try store.duplicate(notebookID: source.id)
        try write("ink 0 and more", notebookPath: copy.path, pageID: copy.pages[0])

        try await drain()

        let server = try XCTUnwrap(MockPaperServer.state.withState { $0[copy.path] })
        XCTAssertEqual(server.order, copy.pages)
        XCTAssertEqual(server.pages[copy.pages[0]]?.drawing, Data("ink 0 and more".utf8))
        XCTAssertEqual(server.pages[copy.pages[1]]?.drawing, Data("ink 1".utf8))
        XCTAssertEqual(server.pages[copy.pages[2]]?.drawing, Data("ink 2".utf8))
        XCTAssertFalse(server.pages.keys.contains { $0.contains(".conflict-") })
    }

    func testImportedPDFPagesArriveWithTheirPagePartWhereTheyWereInserted() async throws {
        let notebook = try store.createNotebook(title: "Reading", pageCount: 2)
        let result = try PDFImporter(store: store).importPDF(
            data: try PDFFixture.threePages(), name: "paper.pdf", into: notebook.id,
            after: notebook.pages[0])
        try write("note", notebookPath: notebook.path, pageID: result.pages[0].id)

        try await drain()

        let local = try store.notebook(id: notebook.id).pages
        XCTAssertEqual(
            local, [notebook.pages[0]] + result.pages.map(\.id) + [notebook.pages[1]])
        let server = try XCTUnwrap(MockPaperServer.state.withState { $0[notebook.path] })
        XCTAssertEqual(server.order, local)
        for page in result.pages {
            // `page` is the part that carries the PDF binding and the media box.
            XCTAssertEqual(server.pages[page.id]?.lastPutParts.contains("page"), true)
        }
        XCTAssertEqual(server.pages[result.pages[0].id]?.drawing, Data("note".utf8))
    }
}
