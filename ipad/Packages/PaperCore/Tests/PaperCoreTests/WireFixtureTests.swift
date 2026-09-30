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
                "changes-full", "trash", "history",
            ])
        let keys: Set<String> = ["created", "modified", "deleted_at", "expires_at", "committed_at"]
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
    }

    func testAFullFeedListsEverythingAndNoDeletes() async throws {
        let feed = try await api(serving: "changes-full").changes(since: 0, epoch: nil)

        XCTAssertTrue(feed.full)
        XCTAssertEqual(feed.deleted, [])
        XCTAssertEqual(feed.notebooks.map(\.path), [Self.notebookPath])
        XCTAssertEqual(feed.pages.count, 3)
        XCTAssertEqual(feed.pages.map(\.seq), feed.pages.map(\.seq).sorted())
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
