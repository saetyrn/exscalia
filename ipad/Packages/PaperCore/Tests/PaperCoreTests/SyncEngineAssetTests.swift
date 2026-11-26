import Foundation
import XCTest

@testable import PaperCore

/// Images and imported PDFs on the wire: an upload lands under the name the
/// page already holds, a pulled page brings the assets it names, and assets
/// stored before any of this was sent go up once.
final class SyncEngineAssetTests: XCTestCase {
    private var root: URL!
    private var store: PaperStore!
    private var clock: TestClock!

    override func setUpWithError() throws {
        try super.setUpWithError()
        MockPaperServer.state.reset()
        root = FileManager.default.temporaryDirectory
            .appending(path: "SyncEngineAssetTests-\(UUID().uuidString)", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        store = try PaperStore(root: root)
        clock = TestClock()
    }

    override func tearDownWithError() throws {
        store = nil
        if let root { try? FileManager.default.removeItem(at: root) }
        root = nil
        try super.tearDownWithError()
    }

    private func makeEngine(recorder: StateRecorder? = nil) async throws -> SyncEngine {
        let settings = try XCTUnwrap(ServerSettings(urlString: "https://paper.test"))
        let api = PaperAPI(
            settings: settings, auth: AnonymousAuthProvider(),
            session: MockPaperServer.makeSession())
        let engine = SyncEngine(
            store: store, api: api, clientID: "ipad-asset-client", clock: clock, jitter: { $0 })
        if let recorder {
            let sink = recorder
            await engine.setHandlers(onState: { state in sink.record(state) })
        }
        return engine
    }

    private func assetOps() throws -> [PaperOp] {
        try store.journal.pending().filter { $0.kind == .assetPut }
    }

    /// A page JSON value the way `PaperJSON` writes it, for the mock to serve.
    private func wire<T: Encodable>(_ value: T) throws -> Any {
        try JSONSerialization.jsonObject(with: PaperJSON.encode(value))
    }

    // MARK: - Push

    func testAnImportedPDFLandsUnderThePathItsPagesName() async throws {
        let notebook = try store.createNotebook(title: "Scans")
        let pdf = try PDFFixture.threePages()
        let result = try PDFImporter(store: store).importPDF(
            data: pdf, name: "Quarterly Report (Final).PDF", into: notebook.id)
        let engine = try await makeEngine()

        await engine.pushPending()

        XCTAssertEqual(try store.journal.parked(), [])
        XCTAssertEqual(try store.journal.pendingCount(), 0)
        let filename = (result.assetPath as NSString).lastPathComponent
        XCTAssertEqual(MockPaperServer.state.assets(inNotebook: notebook.path), [filename: pdf])
        // Sent once, under the name before hashing, so it is not hashed twice.
        let uploads = MockPaperServer.state.assetUploads
        XCTAssertEqual(uploads.map(\.filename), ["quarterly-report-final.pdf"])
        XCTAssertEqual(uploads.first?.contentType, "application/pdf")
        // The server's pages name exactly the file it holds.
        let page = try XCTUnwrap(result.pages.first)
        let named = MockPaperServer.state.withState {
            $0[notebook.path]?.pages[page.id]?.pdf?["asset"] as? String
        }
        XCTAssertEqual(named, result.assetPath)
    }

    /// Every op queued before this release carries the path and nothing else,
    /// under a stem that kept its case.
    func testAnOpThatCarriesOnlyAPathUploadsToo() async throws {
        let notebook = try store.createNotebook(title: "Scans")
        let png = Data("legacy png bytes".utf8)
        let hash = AssetStore.hash(png)
        let legacy = "\(notebook.path)/assets/Scan-01-\(hash).png"
        try AtomicFile.write(png, to: store.assets.url(forAssetPath: legacy))
        try store.journal.append(
            .init(kind: .assetPut, notebookPath: notebook.path, payload: ["path": legacy]))
        let engine = try await makeEngine()

        await engine.pushPending()

        XCTAssertEqual(try store.journal.parked(), [])
        XCTAssertEqual(try assetOps(), [])
        XCTAssertEqual(MockPaperServer.state.assetUploads.map(\.filename), ["Scan-01.png"])
        XCTAssertEqual(
            MockPaperServer.state.assets(inNotebook: notebook.path), ["scan-01-\(hash).png": png])
        XCTAssertEqual(AssetStore.serverFilename(forAssetPath: legacy), "scan-01-\(hash).png")
    }

    /// The notebook was renamed after the image went in and before it was
    /// sent: the upload goes to the notebook the server still has, with the
    /// bytes from where the package is on this device now.
    func testAnAssetQueuedBeforeARenameIsSentFromWhereItIsNow() async throws {
        let notebook = try store.createNotebook(title: "Scans")
        let engine = try await makeEngine()
        await engine.pushPending()
        let image = Data("image".utf8)
        let path = try store.storeAsset(
            image, preferredName: "photo.png", notebookPath: notebook.path)
        let renamed = try store.rename(notebookID: notebook.id, to: "Receipts")

        await engine.pushPending()

        XCTAssertEqual(try store.journal.parked(), [])
        XCTAssertEqual(MockPaperServer.state.assetUploads.map(\.notebook), [notebook.path])
        // The rename behind it took the package, assets and all.
        XCTAssertEqual(
            MockPaperServer.state.assets(inNotebook: renamed.path),
            [(path as NSString).lastPathComponent: image])
    }

    func testAnAssetWithNoBytesHereIsParkedAndNamed() async throws {
        let notebook = try store.createNotebook(title: "Scans")
        let engine = try await makeEngine()
        await engine.pushPending()
        try store.journal.append(
            .init(
                kind: .assetPut, notebookPath: notebook.path,
                payload: ["path": "\(notebook.path)/assets/gone-0123456789abcdef.png"]))

        await engine.pushPending()

        let parked = try store.journal.parked()
        XCTAssertEqual(parked.map(\.parkedReason), ["attachment missing on this device"])
        XCTAssertTrue(MockPaperServer.state.assetUploads.isEmpty)
        let state = await engine.state
        XCTAssertEqual(
            state,
            .attention(message: "an attachment was not sent — attachment missing on this device"))

        // An op with no path at all is parked too, not completed as if sent.
        try store.journal.append(.init(kind: .assetPut, notebookPath: notebook.path))
        await engine.pushPending()
        XCTAssertEqual(
            try store.journal.parked().map(\.parkedReason),
            ["attachment missing on this device", "attachment has no file to send"])
    }

    func testATypeTheServerRefusesIsParkedWithItsAnswer() async throws {
        let notebook = try store.createNotebook(title: "Scans")
        _ = try store.storeAsset(
            Data("gif".utf8), preferredName: "loop.gif", notebookPath: notebook.path)
        let engine = try await makeEngine()

        await engine.pushPending()

        XCTAssertEqual(
            try store.journal.parked().map(\.parkedReason), ["400: Unsupported asset type: .gif"])
    }

    // MARK: - Pull

    func testAPulledPageBringsTheAssetsItNames() async throws {
        let notebookPath = "paper/Scans.paper"
        let pdf = try PDFFixture.threePages()
        let png = Data("legacy png bytes".utf8)
        let moved = Data("moved image".utf8)
        let pdfName = "report-\(AssetStore.hash(pdf)).pdf"
        // Stored with a case-keeping stem by an older build; the server spells
        // it in lower case.
        let legacyName = "Scan-01-\(AssetStore.hash(png)).png"
        // Written while the notebook had another name.
        let movedName = "photo-\(AssetStore.hash(moved)).png"
        let missingName = "gone-0123456789abcdef.png"

        var page = MockServerState.Page(id: PaperID.make(), revision: 1)
        page.pdf = try wire(PDFBinding(asset: "\(notebookPath)/assets/\(pdfName)", pageIndex: 0))
            as? [String: Any]
        let frame = PageObject.Frame(x: 10, y: 10, width: 100, height: 100)
        page.objects = try [
            "\(notebookPath)/assets/\(legacyName)", "paper/Old.paper/assets/\(movedName)",
            "\(notebookPath)/assets/\(missingName)",
        ].map { asset in
            try XCTUnwrap(wire(PageObject(type: .image, frame: frame, asset: asset)) as? [String: Any])
        }
        MockPaperServer.state.seed(
            notebook: notebookPath, id: PaperID.make(), title: "Scans", pages: [page])
        MockPaperServer.state.seedAsset(notebook: notebookPath, name: pdfName, data: pdf)
        MockPaperServer.state.seedAsset(
            notebook: notebookPath, name: AssetStore.serverFilename(forAssetPath: legacyName),
            data: png)
        MockPaperServer.state.seedAsset(notebook: notebookPath, name: movedName, data: moved)
        let engine = try await makeEngine()

        await engine.pull()

        // The page landed, and the pull finished: an asset that did not come
        // does not hold the cursor back.
        let local = try store.page(notebookPath: notebookPath, id: page.id)
        XCTAssertEqual(local.pdf?.asset, "\(notebookPath)/assets/\(pdfName)")
        XCTAssertNotNil(store.syncValue("changes.seq"))
        let failures = await engine.failureCount
        XCTAssertEqual(failures, 0)

        let assets = store.assets
        XCTAssertEqual(
            try Data(contentsOf: assets.url(forNotebook: notebookPath, filename: pdfName)), pdf)
        // Under the name the page uses, whatever the server calls it.
        XCTAssertEqual(
            try Data(contentsOf: assets.url(forNotebook: notebookPath, filename: legacyName)), png)
        XCTAssertEqual(
            assets.data(forAssetPath: "paper/Old.paper/assets/\(movedName)", inNotebook: notebookPath),
            moved)
        XCTAssertFalse(
            assets.exists(assetPath: "\(notebookPath)/assets/\(missingName)", inNotebook: notebookPath))

        // Asked for again the next time the page comes down.
        let later = Data("arrived later".utf8)
        let laterName = "gone-\(AssetStore.hash(later)).png"
        MockPaperServer.state.seedAsset(notebook: notebookPath, name: laterName, data: later)
        let laterObject = try XCTUnwrap(
            wire(PageObject(type: .image, frame: frame, asset: "\(notebookPath)/assets/\(laterName)"))
                as? [String: Any])
        MockPaperServer.state.updatePage(notebook: notebookPath, id: page.id) {
            $0.revision += 1
            $0.objects.append(laterObject)
        }
        await engine.pull()
        XCTAssertEqual(
            assets.data(forAssetPath: "\(notebookPath)/assets/\(laterName)", inNotebook: notebookPath),
            later)
        // Nothing already here is fetched twice.
        XCTAssertEqual(
            MockPaperServer.state.paths(matching: "/api/paper/assets/\(notebookPath)/\(pdfName)")
                .count, 1)
    }

    // MARK: - Backfill

    func testAssetsStoredBeforeUploadsWorkedGoUpOnce() async throws {
        let notebook = try store.createNotebook(title: "Scans")
        // Their ops were completed unsent long ago: files, and no op.
        let first = try store.assets.store(
            Data("one".utf8), preferredName: "one.png", notebookPath: notebook.path)
        let second = try store.assets.store(
            Data("two".utf8), preferredName: "two.pdf", notebookPath: notebook.path)
        // Still queued: not queued a second time.
        let queued = try store.storeAsset(
            Data("three".utf8), preferredName: "three.jpg", notebookPath: notebook.path)
        let engine = try await makeEngine()
        await engine.setOnline(false)

        await engine.syncNow()
        XCTAssertEqual(
            try assetOps().compactMap { $0.payload["path"] }.sorted(),
            [first, second, queued].sorted())
        XCTAssertEqual(try assetOps().map(\.notebookID), Array(repeating: notebook.id, count: 3))
        await engine.syncNow()
        XCTAssertEqual(try assetOps().count, 3)

        await engine.setOnline(true)
        await engine.syncNow()
        XCTAssertEqual(try assetOps(), [])
        XCTAssertEqual(MockPaperServer.state.assets(inNotebook: notebook.path).count, 3)
        await engine.syncNow()
        XCTAssertEqual(MockPaperServer.state.assetUploads.count, 3)
    }

    // MARK: - Files that arrive without storeAsset

    /// The copy's pages name its own `assets/`, so those files go up too --
    /// after the create and ahead of the pages -- long after the one-time
    /// backfill has run.
    func testADuplicateUploadsTheAssetsItsPagesName() async throws {
        let notebook = try store.createNotebook(title: "Scans")
        let pdf = try PDFFixture.threePages()
        let result = try PDFImporter(store: store).importPDF(
            data: pdf, name: "report.pdf", into: notebook.id)
        let engine = try await makeEngine()
        await engine.syncNow()
        XCTAssertEqual(try store.journal.pendingCount(), 0)

        let copy = try store.duplicate(notebookID: notebook.id)
        let queued = try store.journal.pending()
        let upload = try XCTUnwrap(queued.firstIndex { $0.kind == .assetPut })
        XCTAssertEqual(queued.first?.kind, .notebookCreate)
        XCTAssertLessThan(upload, try XCTUnwrap(queued.firstIndex { $0.createsPage }))
        XCTAssertEqual(queued[upload].notebookID, copy.id)

        await engine.pushPending()

        XCTAssertEqual(try store.journal.parked(), [])
        XCTAssertEqual(try store.journal.pendingCount(), 0)
        let filename = (result.assetPath as NSString).lastPathComponent
        XCTAssertEqual(MockPaperServer.state.assets(inNotebook: copy.path), [filename: pdf])
        let pageID = try XCTUnwrap(copy.pages.first { id in
            (try? store.page(notebookPath: copy.path, id: id))?.pdf != nil
        })
        let named = MockPaperServer.state.withState {
            $0[copy.path]?.pages[pageID]?.pdf?["asset"] as? String
        }
        XCTAssertEqual(named, "\(copy.path)/assets/\(filename)")
    }

    /// The delete reached the server, which trashed the package with its
    /// assets; the undo sends the notebook up as new, and its files with it.
    func testUndoingADeleteThatReachedTheServerUploadsTheAssetsAgain() async throws {
        let notebook = try store.createNotebook(title: "Scans")
        let pdf = try PDFFixture.threePages()
        let result = try PDFImporter(store: store).importPDF(
            data: pdf, name: "report.pdf", into: notebook.id)
        let engine = try await makeEngine()
        await engine.syncNow()
        let filename = (result.assetPath as NSString).lastPathComponent
        XCTAssertEqual(MockPaperServer.state.assets(inNotebook: notebook.path), [filename: pdf])

        let token = try store.delete(notebookID: notebook.id)
        await engine.pushPending()
        XCTAssertEqual(MockPaperServer.state.assets(inNotebook: notebook.path), [:])

        _ = try store.restore(token)
        await engine.pushPending()

        XCTAssertEqual(try store.journal.parked(), [])
        XCTAssertEqual(try store.journal.pendingCount(), 0)
        XCTAssertEqual(MockPaperServer.state.assets(inNotebook: notebook.path), [filename: pdf])
    }
}
