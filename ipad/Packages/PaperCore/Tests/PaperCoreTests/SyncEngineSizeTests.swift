import CoreGraphics
import Foundation
import XCTest

@testable import PaperCore

/// A page's preset has to survive the wire. An infinite page measures exactly
/// like an iPad page until it grows and like nothing in particular after, so
/// the dimensions alone bring it back as a fixed iPad or A4 sheet that no
/// longer grows: the name travels beside them as `size_name`.
final class SyncEngineSizeTests: XCTestCase {
    private var roots: [URL] = []
    private var clock: TestClock!
    private let clientID = "ipad-test-client"

    override func setUpWithError() throws {
        try super.setUpWithError()
        MockPaperServer.state.reset()
        clock = TestClock()
    }

    override func tearDownWithError() throws {
        MockPaperServer.state.reset()
        for root in roots { try? FileManager.default.removeItem(at: root) }
        roots = []
        try super.tearDownWithError()
    }

    // MARK: - Fixtures

    /// A store over a root of its own: a second one is a second device, or the
    /// same iPad after the app was deleted and installed again.
    private func makeStore() throws -> PaperStore {
        let root = FileManager.default.temporaryDirectory
            .appending(
                path: "SyncEngineSizeTests-\(UUID().uuidString)", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        roots.append(root)
        return try PaperStore(root: root)
    }

    private func makeEngine(_ store: PaperStore) throws -> SyncEngine {
        let settings = try XCTUnwrap(ServerSettings(urlString: "https://paper.test"))
        let api = PaperAPI(
            settings: settings, auth: AnonymousAuthProvider(),
            session: MockPaperServer.makeSession())
        return SyncEngine(
            store: store, api: api, clientID: clientID, clock: clock, jitter: { $0 })
    }

    private func serverPage(_ path: String, _ id: String) -> MockServerState.Page? {
        MockPaperServer.state.withState { $0[path]?.pages[id] }
    }

    /// A notebook of infinite pages whose first page has grown to twice the
    /// iPad's height and carries ink, pushed.
    private func pushGrownInfinitePage(from store: PaperStore) async throws -> (String, String) {
        let notebook = try store.createNotebook(title: "Sketch", size: .infinite)
        let pageID = try XCTUnwrap(notebook.pages.first)
        let engine = try makeEngine(store)
        _ = await engine.pushPending()
        // The create alone already says what the page is.
        XCTAssertEqual(serverPage(notebook.path, pageID)?.sizeName, "infinite")

        try store.writeDrawing(Data("stroke".utf8), notebookPath: notebook.path, pageID: pageID)
        try store.setPageExtent(
            CGSize(width: 1024, height: 2732), notebookPath: notebook.path, pageID: pageID)
        _ = await engine.pushPending()
        return (notebook.path, pageID)
    }

    // MARK: - Push and a fresh pull

    func testAGrownInfinitePageIsStillInfiniteAfterAFreshPull() async throws {
        let (path, pageID) = try await pushGrownInfinitePage(from: try makeStore())
        let stored = try XCTUnwrap(serverPage(path, pageID))
        XCTAssertEqual(stored.sizeName, "infinite")
        XCTAssertEqual(stored.width, 1024)
        XCTAssertEqual(stored.height, 2732, "size keeps the extent for the web viewer")

        let fresh = try makeStore()
        try await makeEngine(fresh).pull()

        let page = try fresh.page(notebookPath: path, id: pageID)
        XCTAssertEqual(page.size, .infinite)
        XCTAssertEqual(page.customSize, SizePoints(width: 1024, height: 2732))
        XCTAssertEqual(page.pointSize, CGSize(width: 1024, height: 2732))
        XCTAssertEqual(fresh.drawingData(notebookPath: path, pageID: pageID), Data("stroke".utf8))
        let raw = try Data(contentsOf: fresh.paths.pageJSON(path, pageID))
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: raw) as? [String: Any])
        XCTAssertNil(json["size_name"], "on disk the name is `size` itself")
    }

    func testTheOriginalOfAConflictIsStillInfiniteWhenItComesBack() async throws {
        // Adopting a conflict moves this device's copy to the sibling id and
        // pulls the original with nothing local to remember it by.
        let store = try makeStore()
        let (path, pageID) = try await pushGrownInfinitePage(from: store)
        MockPaperServer.state.updatePage(notebook: path, id: pageID) { $0.revision += 1 }

        try store.setPageExtent(
            CGSize(width: 1536, height: 2732), notebookPath: path, pageID: pageID)
        _ = try await makeEngine(store).pushPending()

        let original = try store.page(notebookPath: path, id: pageID)
        XCTAssertEqual(original.size, .infinite)
        XCTAssertEqual(original.customSize, SizePoints(width: 1024, height: 2732))
        let siblingID = try XCTUnwrap(
            store.notebook(path: path)?.pages.first { $0.hasPrefix("\(pageID).conflict-") })
        XCTAssertEqual(try store.page(notebookPath: path, id: siblingID).size, .infinite)
    }

    // MARK: - Which preset a pulled page gets

    func testTheNameWinsOverTheNotebookAndTheDimensions() async throws {
        // An iPad page in a notebook of infinite pages, and an unused infinite
        // page in an A4 notebook: neither the notebook nor the dimensions can
        // tell these apart, only the name.
        MockPaperServer.state.seed(
            notebook: "paper/Endless.paper", id: "nb-endless", title: "Endless",
            paperSize: "infinite",
            pages: [.init(id: "ipadpage", revision: 1, width: 1024, height: 1366, sizeName: "iPad")])
        MockPaperServer.state.seed(
            notebook: "paper/Sheets.paper", id: "nb-sheets", title: "Sheets",
            pages: [
                .init(id: "endlesspage", revision: 1, width: 1024, height: 1366, sizeName: "infinite")
            ])

        let store = try makeStore()
        try await makeEngine(store).pull()

        XCTAssertEqual(try store.page(notebookPath: "paper/Endless.paper", id: "ipadpage").size, .iPad)
        let endless = try store.page(notebookPath: "paper/Sheets.paper", id: "endlesspage")
        XCTAssertEqual(endless.size, .infinite)
        XCTAssertEqual(endless.pointSize, CGSize(width: 1024, height: 1366))
    }

    func testAnUnnamedPageFollowsANotebookOfInfinitePagesWhenItCouldBeOne() async throws {
        // What a server older than `size_name` holds for every page.
        MockPaperServer.state.seed(
            notebook: "paper/Old.paper", id: "nb-old", title: "Old", paperSize: "infinite",
            pages: [
                .init(id: "grown", revision: 1, width: 1024, height: 2732),
                .init(id: "unused", revision: 1, width: 1024, height: 1366),
                .init(id: "sheet", revision: 1, width: 595, height: 842),
            ])

        let store = try makeStore()
        try await makeEngine(store).pull()

        let grown = try store.page(notebookPath: "paper/Old.paper", id: "grown")
        XCTAssertEqual(grown.size, .infinite)
        XCTAssertEqual(grown.customSize, SizePoints(width: 1024, height: 2732))
        XCTAssertEqual(try store.page(notebookPath: "paper/Old.paper", id: "unused").size, .infinite)
        // Smaller than an infinite page ever is: an A4 page from before the
        // notebook's default changed stays A4.
        XCTAssertEqual(try store.page(notebookPath: "paper/Old.paper", id: "sheet").size, .a4)
    }

    func testAnUnnamedPageInAFixedNotebookStillGoesByItsDimensions() async throws {
        MockPaperServer.state.seed(
            notebook: "paper/Fixed.paper", id: "nb-fixed", title: "Fixed", paperSize: "a4",
            pages: [
                .init(id: "ipadpage", revision: 1, width: 1024, height: 1366),
                .init(id: "tall", revision: 1, width: 1024, height: 2732),
            ])

        let store = try makeStore()
        try await makeEngine(store).pull()

        XCTAssertEqual(try store.page(notebookPath: "paper/Fixed.paper", id: "ipadpage").size, .iPad)
        let tall = try store.page(notebookPath: "paper/Fixed.paper", id: "tall")
        XCTAssertEqual(tall.size, .a4)
        XCTAssertEqual(tall.customSize, SizePoints(width: 1024, height: 2732))
    }

    func testAPDFPageInANotebookOfInfinitePagesKeepsItsMediaBox() throws {
        let store = try makeStore()
        let notebook = try store.createNotebook(title: "Scans", size: .infinite)
        let remote = Data(
            #"{"id":"pdfpage","template":"blank","size":{"width":1224,"height":1584},"size_points":{"width":1224,"height":1584},"pdf":{"asset":"paper/Scans.paper/assets/scan-0123456789abcdef.pdf","pageIndex":0},"objects":[],"revision":1}"#
                .utf8)
        let page = try XCTUnwrap(
            store.applyRemotePage(
                rawPageJSON: remote, notebookPath: notebook.path, pageID: "pdfpage",
                drawing: nil, render: nil))
        XCTAssertEqual(page.size, .a4, "what the dimensions say, not a page that grows")
        XCTAssertEqual(page.customSize, SizePoints(width: 1224, height: 1584))
    }

    // MARK: - What goes up

    func testThePagePartNamesThePreset() throws {
        let store = try makeStore()
        let notebook = try store.createNotebook(title: "Plain", size: .letter)
        let pageID = try XCTUnwrap(notebook.pages.first)
        let wire = try XCTUnwrap(store.pagePartJSON(notebookPath: notebook.path, pageID: pageID))
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: wire) as? [String: Any])
        XCTAssertEqual(object["size_name"] as? String, "letter")
        XCTAssertEqual(object["size"] as? [String: Double], ["width": 612, "height": 792])
    }
}
