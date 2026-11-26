import Foundation
import XCTest

@testable import PaperCore

/// The real server's answers, decoded the way the app decodes them.
///
/// `Fixtures/*.json` are written by `backend/tests/test_paper_wire_fixtures.py`
/// from the live `/api/paper` routes, so unlike `MockPaperServer` they cannot
/// drift from what the server says. Each one is served through `PaperAPI`'s
/// transport, so the decode is the client's own, and the pulled page and
/// manifest are then written to disk the way a sync pull writes them. The
/// timestamps keep the server's six fractional digits on purpose: that shape
/// once made pulled pages unreadable (HANDOFF §7 Paper).
final class WireFixtureTests: PaperStoreTestCase {
    private static let notebookPath = "paper/Work/Ideas.paper"
    private static let pageID = String(repeating: "a", count: 32)
    private static let otherPageID = String(repeating: "b", count: 32)
    /// `paper/Work`'s legacy id, the contract's vector: the folder came with
    /// the notebook and never had a record of its own.
    private static let workID = "9e7201dacb87ef0d43fb22798ccf4af6"
    private static let archiveID = String(repeating: "a7c4", count: 8)
    /// 2026-09-02T10:00:00.123456Z, as `PaperDate` reads it (milliseconds).
    private static let serverInstant = Date(timeIntervalSince1970: 1_788_343_200.123)

    private func fixture(_ name: String) throws -> Data {
        let url = try XCTUnwrap(
            Bundle.module.url(forResource: name, withExtension: "json", subdirectory: "Fixtures"),
            "missing fixture \(name).json")
        return try Data(contentsOf: url)
    }

    /// A client whose every request is answered with one fixture.
    private func api(serving name: String, status: Int = 200) throws -> PaperAPI {
        let body = try fixture(name)
        let settings = try XCTUnwrap(ServerSettings(urlString: "https://notes.exscalia.art"))
        return PaperAPI(settings: settings, auth: StubAuth(token: nil)) { request in
            guard let url = request.url,
                let response = HTTPURLResponse(
                    url: url, statusCode: status, httpVersion: "HTTP/1.1",
                    headerFields: ["Content-Type": "application/json"])
            else { throw URLError(.badURL) }
            return (body, response as URLResponse)
        }
    }

    private func assertServerTime(
        _ text: String?, file: StaticString = #filePath, line: UInt = #line
    ) throws {
        let date = try XCTUnwrap(text.flatMap(PaperDate.parse), file: file, line: line)
        XCTAssertEqual(
            date.timeIntervalSince1970, Self.serverInstant.timeIntervalSince1970,
            accuracy: 0.0005, file: file, line: line)
    }

    private func put(_ api: PaperAPI) async throws -> PagePutOutcome {
        try await api.putPage(
            notebook: Self.notebookPath, pageID: Self.pageID, baseRevision: 1, pageJSON: nil,
            drawing: Data("ink".utf8), render: nil, text: nil, forRevision: nil, after: nil,
            clientID: "ipad-1")
    }

    func testEveryTimestampTheServerSendsIsOnePaperDateReads() throws {
        let urls = try XCTUnwrap(
            Bundle.module.urls(forResourcesWithExtension: "json", subdirectory: "Fixtures"))
        XCTAssertEqual(
            Set(urls.map { $0.deletingPathExtension().lastPathComponent }),
            [
                "library", "notebook", "page", "save-200", "save-409", "changes",
                "changes-full", "trash", "history", "folders", "folder-rename",
                "changes-folders", "notebook-whiteboard", "page-board",
            ])
        let keys: Set<String> = [
            "created", "modified", "deleted_at", "expires_at", "committed_at", "at",
        ]
        var seen = 0
        func walk(_ value: Any, in name: String) {
            if let object = value as? [String: Any] {
                for (key, child) in object {
                    if keys.contains(key), let text = child as? String {
                        seen += 1
                        XCTAssertNotNil(PaperDate.parse(text), "\(name).json \(key): \(text)")
                    }
                    walk(child, in: name)
                }
            } else if let array = value as? [Any] {
                for child in array { walk(child, in: name) }
            }
        }
        for url in urls {
            walk(
                try JSONSerialization.jsonObject(with: Data(contentsOf: url)),
                in: url.deletingPathExtension().lastPathComponent)
        }
        XCTAssertGreaterThan(seen, 0)
    }

    func testLibrary() async throws {
        let library = try await api(serving: "library").library()

        XCTAssertEqual(library.folders, ["paper/Work"])
        let summary = try XCTUnwrap(library.notebooks.first)
        XCTAssertEqual(summary.path, Self.notebookPath)
        XCTAssertEqual(summary.notebookID, String(repeating: "5eed", count: 8))
        XCTAssertEqual(summary.cover, Self.pageID)
        XCTAssertEqual(summary.pageCount, 3)
        // The server also stamps the render's hash (`&v=`) for cache busting.
        XCTAssertTrue(summary.coverURL?.contains("/render.png?r=2") == true)
        try assertServerTime(summary.modified)
        XCTAssertEqual(summary.folder, "paper/Work")
        XCTAssertEqual(summary.folderID, Self.workID)
        XCTAssertEqual(library.folderRecords.map(\.id), [Self.workID])
        XCTAssertEqual(library.folderRecords.map(\.path), library.folders)
    }

    func testNotebookManifestCarriesEveryPageAndAppliesLocally() async throws {
        let manifest = try await api(serving: "notebook").notebook(path: Self.notebookPath)

        let pages = try XCTUnwrap(manifest.pages)
        XCTAssertEqual(pages.count, 3)
        XCTAssertEqual(manifest.pagesDetail?.map(\.id), pages)
        let sibling = try XCTUnwrap(manifest.pagesDetail?.first { $0.conflictOf != nil })
        XCTAssertEqual(sibling.conflictOf, Self.pageID)
        XCTAssertTrue(sibling.id.hasPrefix(Self.pageID + ".conflict-"))

        try store.applyRemoteNotebook(manifest)
        let local = try AtomicFile.readJSON(
            Notebook.self, at: store.paths.notebookJSON(Self.notebookPath))
        XCTAssertEqual(local.pages, pages)
        XCTAssertEqual(
            local.modified.timeIntervalSince1970, Self.serverInstant.timeIntervalSince1970,
            accuracy: 0.0005)
    }

    func testPageDecodesAndSurvivesTheWriteToDisk() async throws {
        let response = try await api(serving: "page").page(
            notebook: Self.notebookPath, pageID: Self.pageID)

        let page = response.page
        XCTAssertEqual(page.id, Self.pageID)
        XCTAssertEqual(page.revision, 2)
        XCTAssertTrue(page.bookmarked)
        XCTAssertEqual(page.size, RemoteSize(width: 1024, height: 1366))
        XCTAssertEqual(page.sizePoints, RemoteSize(width: 1024, height: 2048.5))
        XCTAssertNotNil(page.drawingURL)
        XCTAssertNotNil(page.renderURL)
        XCTAssertNotNil(page.textURL)

        // The raw bytes are what a pull writes; the page must read back.
        let applied = try XCTUnwrap(
            store.applyRemotePage(
                rawPageJSON: response.raw, notebookPath: Self.notebookPath, pageID: Self.pageID,
                drawing: nil, render: nil))
        XCTAssertEqual(applied.customSize, SizePoints(width: 1024, height: 2048.5))
        XCTAssertEqual(applied.title, "Sketch")
        XCTAssertEqual(
            applied.modified.timeIntervalSince1970, Self.serverInstant.timeIntervalSince1970,
            accuracy: 0.0005)
    }

    func testSaveAnswers200WithTheNewRevision() async throws {
        guard case .saved(let result) = try await put(api(serving: "save-200")) else {
            return XCTFail("a 200 must decode as a save")
        }
        XCTAssertEqual(result.revision, 2)
        XCTAssertEqual(result.stale, false)
        XCTAssertEqual(result.page.revision, 2)
        XCTAssertEqual(result.page.renderRevision, 2)
        XCTAssertEqual(result.page.textRevision, 2)
        XCTAssertEqual(result.page.sizePoints, RemoteSize(width: 1024, height: 2048.5))
    }

    func testSaveAnswers409WithBothVersions() async throws {
        guard case .conflict(let conflict) = try await put(api(serving: "save-409", status: 409))
        else {
            return XCTFail("a 409 must decode as a conflict")
        }
        let original = conflict.conflict.original
        let sibling = conflict.conflict.sibling
        XCTAssertEqual(original.id, Self.pageID)
        XCTAssertEqual(original.revision, 2)
        XCTAssertNil(original.conflictOf)
        XCTAssertEqual(sibling.conflictOf, Self.pageID)
        XCTAssertTrue(sibling.id.hasPrefix(Self.pageID + ".conflict-"))
        XCTAssertEqual(sibling.revision, 1)
        XCTAssertNotNil(conflict.detail)
    }

    func testChangesAfterACursorCarryTheTombstone() async throws {
        let feed = try await api(serving: "changes").changes(since: 8, epoch: "e0e0e0e0e0e0")

        XCTAssertFalse(feed.full)
        XCTAssertEqual(feed.epoch, "e0e0e0e0e0e0")
        XCTAssertEqual(
            feed.deleted.map(\.path), ["\(Self.notebookPath)/pages/\(Self.otherPageID)"])
        XCTAssertEqual(feed.deleted.map(\.kind), ["page"])
        let moved = try XCTUnwrap(feed.pages.first)
        XCTAssertEqual(moved.id, Self.pageID)
        XCTAssertEqual(moved.revision, 3)
        try assertServerTime(moved.modified)
        XCTAssertEqual(feed.folders, [])
        XCTAssertEqual(feed.folderMoves, [])
    }

    func testAFullFeedListsEverythingAndNoDeletes() async throws {
        let feed = try await api(serving: "changes-full").changes(since: 0, epoch: nil)

        XCTAssertTrue(feed.full)
        XCTAssertEqual(feed.deleted, [])
        XCTAssertEqual(feed.notebooks.map(\.path), [Self.notebookPath])
        XCTAssertEqual(feed.pages.count, 3)
        XCTAssertEqual(feed.pages.map(\.seq), feed.pages.map(\.seq).sorted())
        XCTAssertEqual(feed.folders.map(\.path), ["paper/Work"])
        XCTAssertEqual(feed.folderMoves, [])
    }

    // MARK: Folders

    func testFolderRecordsDecodeWithTheirLegacyAndChosenIds() throws {
        let list = try JSONDecoder().decode([String: [FolderRecord]].self, from: fixture("folders"))
        let folders = try XCTUnwrap(list["folders"])

        XCTAssertEqual(folders.map(\.path), ["paper/Archive", "paper/Work"])
        let archive = folders[0]
        XCTAssertEqual(archive.id, Self.archiveID)
        XCTAssertEqual(archive.name, "Archive")
        XCTAssertEqual(archive.color, "orange")
        XCTAssertEqual(archive.icon, "symbol:briefcase")
        XCTAssertEqual(archive.parent, "paper")
        XCTAssertFalse(archive.favorite)
        let work = folders[1]
        XCTAssertEqual(work.id, Self.workID)
        XCTAssertNil(work.icon)
        XCTAssertEqual(work.color, "blue")
        XCTAssertGreaterThan(archive.seq, work.seq)
        try assertServerTime(work.created)
        try assertServerTime(work.modified)
    }

    func testACreateAnswerIsTheRecordPlusExisting() throws {
        let list = try XCTUnwrap(
            JSONSerialization.jsonObject(with: fixture("folders")) as? [String: Any])
        var record = try XCTUnwrap((list["folders"] as? [[String: Any]])?.first)
        record["existing"] = true
        let result = try JSONDecoder().decode(
            FolderCreateResult.self, from: JSONSerialization.data(withJSONObject: record))

        XCTAssertTrue(result.existing)
        XCTAssertEqual(result.folder.id, Self.archiveID)
        XCTAssertEqual(result.folder.path, "paper/Archive")
        let again = try JSONDecoder().decode(
            FolderCreateResult.self, from: JSONEncoder().encode(result))
        XCTAssertEqual(again, result)
    }

    func testARenameAnswersWhereTheFolderLanded() throws {
        let result = try JSONDecoder().decode(FolderRenameResult.self, from: fixture("folder-rename"))

        XCTAssertEqual(result.from, "paper/Work")
        XCTAssertEqual(result.path, "paper/Work 2026")
        XCTAssertEqual(result.folder.path, result.path)
        XCTAssertEqual(result.folder.id, Self.workID)
        XCTAssertEqual(result.folder.name, "Work 2026")
    }

    func testAFolderRenameReachesTheFeedAsAMoveWithNoPages() async throws {
        let feed = try await api(serving: "changes-folders").changes(since: 12, epoch: "e0e0e0e0e0e0")

        XCTAssertFalse(feed.full)
        // Nothing inside was restamped, so a device downloads nothing.
        XCTAssertEqual(feed.pages, [])
        XCTAssertEqual(feed.notebooks, [])
        XCTAssertEqual(feed.deleted, [])
        let move = try XCTUnwrap(feed.folderMoves.first)
        XCTAssertEqual(feed.folderMoves.count, 1)
        XCTAssertEqual(move.id, Self.workID)
        XCTAssertEqual(move.from, "paper/Work")
        XCTAssertEqual(move.to, "paper/Work 2026")
        XCTAssertEqual(move.seq, feed.seq)
        try assertServerTime(move.at)
        XCTAssertEqual(feed.folders.map(\.path), ["paper/Work 2026"])
        XCTAssertEqual(feed.folders.first?.seq, move.seq)
    }

    func testAnOlderServersAnswersStillDecode() throws {
        let feed = try JSONDecoder().decode(
            ChangesFeed.self,
            from: Data(
                #"{"epoch":"e","seq":3,"full":false,"notebooks":[],"pages":[],"deleted":[{"path":"paper/A.paper","kind":"notebook","seq":3}]}"#
                    .utf8))
        XCTAssertEqual(feed.folders, [])
        XCTAssertEqual(feed.folderMoves, [])
        XCTAssertNil(feed.deleted.first?.id)
        let library = try JSONDecoder().decode(
            PaperLibrary.self, from: Data(#"{"folders":["paper/A"],"notebooks":[]}"#.utf8))
        XCTAssertEqual(library.folderRecords, [])
        let record = try JSONDecoder().decode(
            FolderRecord.self, from: Data(#"{"id":"x","path":"paper/A/B"}"#.utf8))
        XCTAssertEqual(record.name, "B")
        XCTAssertEqual(record.parent, "paper/A")
        XCTAssertEqual(record.color, "blue")
        XCTAssertEqual(record.seq, 0)
    }

    func testTheNewDocumentFieldsDecodeWhenPresent() throws {
        let page = try JSONDecoder().decode(
            RemotePage.self,
            from: Data(
                #"{"id":"p","paper_color":"cream","board_extent":{"width":16384,"height":16384},"render_rect":{"x":10,"y":20,"width":300,"height":400}}"#
                    .utf8))
        XCTAssertEqual(page.paperColor, "cream")
        XCTAssertEqual(page.boardExtent, RemoteSize(width: 16384, height: 16384))
        XCTAssertEqual(page.renderRect, PageObject.Frame(x: 10, y: 20, width: 300, height: 400))
        let again = try JSONDecoder().decode(RemotePage.self, from: JSONEncoder().encode(page))
        XCTAssertEqual(again, page)
        let summary = try JSONDecoder().decode(
            NotebookSummary.self,
            from: Data(
                #"{"path":"paper/B.paper","kind":"whiteboard","orientation":"landscape","paperColor":"dark"}"#
                    .utf8))
        XCTAssertEqual(summary.kind, "whiteboard")
        XCTAssertEqual(summary.orientation, "landscape")
        XCTAssertEqual(summary.paperColor, "dark")
        let manifest = try JSONDecoder().decode(
            NotebookManifest.self,
            from: Data(#"{"path":"paper/B.paper","kind":"whiteboard","paperColor":"cream"}"#.utf8))
        XCTAssertEqual(manifest.kind, "whiteboard")
        XCTAssertEqual(manifest.paperColor, "cream")
        XCTAssertNil(manifest.orientation)
    }

    // MARK: Documents and boards

    func testANotebookFromAClientThatNamesNoKindReadsAsANotebook() async throws {
        let library = try await api(serving: "library").library()
        let summary = try XCTUnwrap(library.notebooks.first)
        XCTAssertEqual(summary.kind, "notebook")
        XCTAssertEqual(summary.orientation, "portrait")
        XCTAssertEqual(summary.paperColor, "white")
        // The cover is the first page.
        let manifest = try await api(serving: "notebook").notebook(path: Self.notebookPath)
        XCTAssertEqual(manifest.cover, manifest.pages?.first)
        XCTAssertEqual(manifest.kind, "notebook")
    }

    func testAWhiteboardManifest() async throws {
        let path = "paper/Plans.paper"
        let manifest = try await api(serving: "notebook-whiteboard").notebook(path: path)

        XCTAssertEqual(manifest.path, path)
        XCTAssertEqual(manifest.id, String(repeating: "b0a4", count: 8))
        XCTAssertEqual(manifest.kind, "whiteboard")
        XCTAssertEqual(manifest.paperSize, "board")
        XCTAssertEqual(manifest.paperColor, "cream")
        XCTAssertEqual(manifest.template, "grid")
        let boardID = String(repeating: "c", count: 32)
        XCTAssertEqual(manifest.pages, [boardID])
        XCTAssertEqual(manifest.cover, boardID)
        let board = try XCTUnwrap(manifest.pagesDetail?.first)
        XCTAssertEqual(board.sizeName, "board")
        XCTAssertEqual(board.title, "Board 1")
    }

    func testABoardPageCarriesItsExtentAndTheAreaItsRenderShows() async throws {
        let path = "paper/Plans.paper"
        let boardID = String(repeating: "c", count: 32)
        let response = try await api(serving: "page-board").page(notebook: path, pageID: boardID)

        let page = response.page
        XCTAssertEqual(page.sizeName, "board")
        XCTAssertEqual(page.paperColor, "cream")
        XCTAssertEqual(page.boardExtent, RemoteSize(width: 16384, height: 16384))
        let rect = PageObject.Frame(x: 7509, y: 7680, width: 1366, height: 1024)
        XCTAssertEqual(page.renderRect, rect)
        // `size` is the render's area, for viewers that size the image by it;
        // a board never carries `size_points`.
        XCTAssertEqual(page.size, RemoteSize(width: 1366, height: 1024))
        XCTAssertNil(page.sizePoints)
        XCTAssertNotNil(page.renderURL)

        // A pull keeps the extent and ignores the wire `size`.
        let applied = try XCTUnwrap(
            store.applyRemotePage(
                rawPageJSON: response.raw, notebookPath: path, pageID: boardID,
                drawing: nil, render: nil))
        XCTAssertEqual(applied.size, .board)
        XCTAssertEqual(applied.boardExtent, SizePoints(width: 16384, height: 16384))
        XCTAssertNil(applied.customSize)
        XCTAssertEqual(applied.paperColor, .cream)
        XCTAssertEqual(applied.renderRect, rect)
        XCTAssertEqual(applied.title, "Board 1")
    }

    func testAFolderTrashItem() throws {
        let item = try JSONDecoder().decode(
            PaperTrashItem.self,
            from: Data(
                #"{"id":"t","kind":"paper_folder","original_path":"paper/Work/Accounting","note_count":0,"folder_id":"f","notebook_count":2}"#
                    .utf8))
        XCTAssertEqual(item.folderID, "f")
        XCTAssertEqual(item.notebookCount, 2)
        XCTAssertEqual(item.displayTitle, "Accounting")
    }

    func testTrash() async throws {
        let trash = try await api(serving: "trash").trash()

        let item = try XCTUnwrap(trash.items.first)
        XCTAssertEqual(item.kind, "page")
        XCTAssertEqual(item.pageID, Self.otherPageID)
        XCTAssertEqual(item.notebook, Self.notebookPath)
        XCTAssertEqual(item.position, 2)
        XCTAssertEqual(item.displayTitle, "Ideas · page")
        try assertServerTime(item.deletedAt)
    }

    func testHistory() async throws {
        let history = try await api(serving: "history").history(
            notebook: Self.notebookPath, pageID: Self.pageID)

        XCTAssertEqual(history.path, "\(Self.notebookPath)/pages/\(Self.pageID)")
        XCTAssertEqual(history.versions.map(\.message), ["paper: second", "paper: first"])
        for version in history.versions {
            XCTAssertEqual(version.sha.count, 40)
            XCTAssertTrue(version.sha.hasPrefix(try XCTUnwrap(version.shortSHA)))
            XCTAssertNotNil(version.committedAt)
        }
    }
}
