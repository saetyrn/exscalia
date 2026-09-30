import CoreGraphics
import Foundation
import XCTest

@testable import PaperCore

/// A correction belongs to the ink it was made for. New ink sets it aside as
/// stale rather than rebinding or dropping it, "Use original" clears it on the
/// wire, and a reading of older ink never rides the push of newer ink.
final class TranscriptRevisionTests: PaperStoreTestCase {
    override func setUpWithError() throws {
        try super.setUpWithError()
        MockPaperServer.state.reset()
    }

    // MARK: - Fixtures

    private func reading(_ text: String, forRevision: Int?) -> PageText {
        PageText(
            lines: text.split(separator: "\n").enumerated().map { index, line in
                RecognizedLine(
                    text: String(line),
                    box: PageObject.Frame(
                        x: 60, y: 100 + Double(index) * 30, width: 200, height: 24),
                    confidence: 0.9)
            },
            forRevision: forRevision)
    }

    private let correction = TextCorrection(
        before: "recieve budget", after: "receive budget", start: 0, end: 14)

    /// Old ink is told from new by the files' dates; the next write has to
    /// land visibly later.
    private func tick() { Thread.sleep(forTimeInterval: 0.02) }

    private func object(_ notebook: Notebook, _ pageID: String) throws -> [String: Any] {
        let data = try Data(contentsOf: store.paths.text(notebook.path, pageID))
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    /// Ink, a reading of it, and the user's correction of that reading.
    private func correctedPage() throws -> (Notebook, String) {
        let notebook = try store.createNotebook(title: "Budget")
        let pageID = try XCTUnwrap(notebook.pages.first)
        try store.writeDrawing(Data("ink one".utf8), notebookPath: notebook.path, pageID: pageID)
        tick()
        try store.writePageText(
            reading("recieve budget", forRevision: 1), notebookPath: notebook.path,
            pageID: pageID)
        try store.correctPageText(
            notebookID: notebook.id, pageID: pageID, correctedTranscript: "receive budget",
            corrections: [correction])
        return (notebook, pageID)
    }

    // MARK: - Store

    func testNewInkSetsTheCorrectionAsideInsteadOfLosingIt() throws {
        let (notebook, pageID) = try correctedPage()
        XCTAssertEqual(try object(notebook, pageID)["corrections_revision"] as? Int, 1)
        XCTAssertNotNil(store.textData(notebookPath: notebook.path, pageID: pageID))

        tick()
        try store.writeDrawing(Data("ink two".utf8), notebookPath: notebook.path, pageID: pageID)
        XCTAssertNil(
            store.textData(notebookPath: notebook.path, pageID: pageID),
            "a reading of the old ink must not ride the new ink's push")

        try store.writePageText(
            reading("recieve budgte\ndentist tuesday", forRevision: 2),
            notebookPath: notebook.path, pageID: pageID)
        let text = try XCTUnwrap(store.pageText(notebookPath: notebook.path, pageID: pageID))
        XCTAssertNil(text.correctedTranscript)
        XCTAssertTrue(text.corrections.isEmpty)
        XCTAssertNil(text.correctionsRevision)
        XCTAssertEqual(text.forRevision, 2)
        XCTAssertEqual(text.staleCorrection?.correctedTranscript, "receive budget")
        XCTAssertEqual(text.staleCorrection?.corrections, [correction])
        XCTAssertEqual(text.staleCorrection?.correctionsRevision, 1)
        XCTAssertEqual(text.searchableTranscript, "recieve budgte\ndentist tuesday")
        XCTAssertNotNil(store.textData(notebookPath: notebook.path, pageID: pageID))

        let stale = try XCTUnwrap(try object(notebook, pageID)["stale_correction"] as? [String: Any])
        XCTAssertEqual(stale["corrected_transcript"] as? String, "receive budget")
        XCTAssertEqual(stale["corrections_revision"] as? Int, 1)

        // Still there after the page moves on again.
        tick()
        try store.writeDrawing(Data("ink three".utf8), notebookPath: notebook.path, pageID: pageID)
        try store.writePageText(
            reading("dentist tuesday", forRevision: 3), notebookPath: notebook.path,
            pageID: pageID)
        XCTAssertEqual(
            store.pageText(notebookPath: notebook.path, pageID: pageID)?.staleCorrection?
                .correctedTranscript, "receive budget")
    }

    /// A pull after a title or bookmark edit brings the same ink back. It is
    /// not new ink: the transcript still describes it and still goes up.
    func testAPullOfTheSameInkLeavesTheTranscriptCurrent() throws {
        let (notebook, pageID) = try correctedPage()
        let remote = Data(
            #"{"id":"\#(pageID)","title":"Renamed","revision":2,"size":{"width":595,"height":842},"objects":[]}"#
                .utf8)

        tick()
        try store.applyRemotePage(
            rawPageJSON: remote, notebookPath: notebook.path, pageID: pageID,
            drawing: Data("ink one".utf8), render: nil)
        XCTAssertFalse(store.transcriptPredatesInk(notebookPath: notebook.path, pageID: pageID))
        XCTAssertNotNil(store.textData(notebookPath: notebook.path, pageID: pageID))

        tick()
        try store.applyRemotePage(
            rawPageJSON: remote, notebookPath: notebook.path, pageID: pageID,
            drawing: Data("ink two".utf8), render: nil)
        XCTAssertTrue(store.transcriptPredatesInk(notebookPath: notebook.path, pageID: pageID))
        XCTAssertEqual(
            store.drawingData(notebookPath: notebook.path, pageID: pageID), Data("ink two".utf8))
    }

    func testReadingTheSameInkAgainKeepsTheCorrectionLive() throws {
        let (notebook, pageID) = try correctedPage()
        tick()
        try store.writePageText(
            reading("recieve budget", forRevision: 2), notebookPath: notebook.path,
            pageID: pageID)

        let text = try XCTUnwrap(store.pageText(notebookPath: notebook.path, pageID: pageID))
        XCTAssertEqual(text.correctedTranscript, "receive budget")
        XCTAssertEqual(text.corrections, [correction])
        XCTAssertEqual(text.correctionsRevision, 2)
        XCTAssertNil(text.staleCorrection)
    }

    func testUseOriginalIsAnExplicitClearOnTheWire() throws {
        let (notebook, pageID) = try correctedPage()
        try store.correctPageText(
            notebookID: notebook.id, pageID: pageID, correctedTranscript: nil, corrections: [])

        let wire = try XCTUnwrap(store.textData(notebookPath: notebook.path, pageID: pageID))
        let sent = try XCTUnwrap(JSONSerialization.jsonObject(with: wire) as? [String: Any])
        XCTAssertTrue(sent["corrected_transcript"] is NSNull)
        XCTAssertEqual((sent["corrections"] as? [Any])?.count, 0)
        XCTAssertNil(sent["corrections_revision"])
        XCTAssertEqual(
            store.pageText(notebookPath: notebook.path, pageID: pageID)?.searchableTranscript,
            "recieve budget")
        XCTAssertEqual(store.textIndex.text(pageID: pageID)?.searchableTranscript, "recieve budget")
    }

    func testUseOriginalAlsoSettlesAStaleCorrection() throws {
        let (notebook, pageID) = try correctedPage()
        tick()
        try store.writeDrawing(Data("ink two".utf8), notebookPath: notebook.path, pageID: pageID)
        try store.writePageText(
            reading("dentist tuesday", forRevision: 2), notebookPath: notebook.path,
            pageID: pageID)
        XCTAssertNotNil(
            store.pageText(notebookPath: notebook.path, pageID: pageID)?.staleCorrection)

        try store.correctPageText(
            notebookID: notebook.id, pageID: pageID, correctedTranscript: nil, corrections: [])
        XCTAssertNil(try object(notebook, pageID)["stale_correction"])
    }

    func testAMetadataChangeStillSendsTheReading() throws {
        let (notebook, pageID) = try correctedPage()
        tick()
        _ = try store.setPageTitle(notebookID: notebook.id, pageID: pageID, title: "Groceries")
        _ = try store.setPageBookmarked(notebookID: notebook.id, pageID: pageID, true)
        XCTAssertNotNil(store.textData(notebookPath: notebook.path, pageID: pageID))
    }

    /// The sheet can be opened in the seconds between a stroke and the
    /// reading of it. Correcting the old reading does not make it current.
    func testCorrectingAReadingOfOlderInkKeepsItHeldBack() throws {
        let notebook = try store.createNotebook(title: "Budget")
        let pageID = try XCTUnwrap(notebook.pages.first)
        try store.writeDrawing(Data("ink one".utf8), notebookPath: notebook.path, pageID: pageID)
        tick()
        try store.writePageText(
            reading("recieve budget", forRevision: 1), notebookPath: notebook.path,
            pageID: pageID)
        tick()
        try store.writeDrawing(Data("ink two".utf8), notebookPath: notebook.path, pageID: pageID)

        try store.correctPageText(
            notebookID: notebook.id, pageID: pageID, correctedTranscript: "receive budget",
            corrections: [correction])
        XCTAssertNil(store.textData(notebookPath: notebook.path, pageID: pageID))

        try store.writePageText(
            reading("dentist tuesday", forRevision: 2), notebookPath: notebook.path,
            pageID: pageID)
        let text = try XCTUnwrap(store.pageText(notebookPath: notebook.path, pageID: pageID))
        XCTAssertNil(text.correctedTranscript)
        XCTAssertEqual(text.staleCorrection?.correctedTranscript, "receive budget")
    }

    // MARK: - Recognizer

    private struct FixedReading: HandwritingRecognitionBackend {
        let text: String
        func recognize(image: CGImage, pageSize: CGSize, languages: [String]) throws -> PageText {
            PageText(
                lines: text.split(separator: "\n").map {
                    RecognizedLine(
                        text: String($0), box: PageObject.Frame(x: 0, y: 0, width: 10, height: 10),
                        confidence: 0.9)
                })
        }
    }

    private func recognize(_ text: String, _ notebook: Notebook, _ pageID: String, revision: Int)
        async throws
    {
        let image = try RecognitionTests.fixture(
            "x", size: CGSize(width: 40, height: 40), scale: 1, origin: CGPoint(x: 2, y: 30))
        try RecognitionTests.writePNG(image, to: store.paths.render(notebook.path, pageID))
        let recognizer = HandwritingRecognizer(
            store: store, idleDelay: 0, backend: FixedReading(text: text))
        let read = await recognizer.recognizeNow(
            RecognitionJob(
                notebookPath: notebook.path, pageID: pageID,
                pageSize: CGSize(width: 40, height: 40), forRevision: revision))
        XCTAssertNotNil(read)
    }

    func testTheRecognizerStampsItsReadingAndSearchesWhatItFiled() async throws {
        let (notebook, pageID) = try correctedPage()
        XCTAssertEqual(store.textIndex.search("receive").first?.pages.first?.pageID, pageID)

        // Read again without new ink: the correction is still the page's text.
        tick()
        try await recognize("recieve budget", notebook, pageID, revision: 1)
        XCTAssertEqual(store.textIndex.search("receive").first?.pages.first?.pageID, pageID)

        tick()
        try store.writeDrawing(Data("ink two".utf8), notebookPath: notebook.path, pageID: pageID)
        try await recognize("recieve budgte\ndentist tuesday", notebook, pageID, revision: 2)

        let text = try XCTUnwrap(store.pageText(notebookPath: notebook.path, pageID: pageID))
        XCTAssertEqual(text.forRevision, 2)
        XCTAssertEqual(text.staleCorrection?.correctedTranscript, "receive budget")
        XCTAssertEqual(store.textIndex.search("dentist").first?.pages.first?.pageID, pageID)
        XCTAssertTrue(
            store.textIndex.search("receive").isEmpty, "a stale correction is not searched")
    }

    // MARK: - Sync

    private func makeEngine() throws -> SyncEngine {
        let settings = try XCTUnwrap(ServerSettings(urlString: "https://paper.test"))
        let api = PaperAPI(
            settings: settings, auth: AnonymousAuthProvider(),
            session: MockPaperServer.makeSession())
        return SyncEngine(
            store: store, api: api, clientID: "ipad-test-client", clock: TestClock(),
            jitter: { $0 })
    }

    /// The sequence the audit replayed: correct, write more, read again. The
    /// ink goes up without the old reading, and the fresh reading carries the
    /// correction as stale rather than as the page's text.
    func testNewInkGoesUpWithoutTheOldReading() async throws {
        let notebook = try store.createNotebook(title: "Ideas", folder: "Work")
        let pageID = try XCTUnwrap(notebook.pages.first)
        try store.writeDrawing(Data("ink one".utf8), notebookPath: notebook.path, pageID: pageID)
        try store.journal.append(
            .init(kind: .pagePut, notebookPath: notebook.path, pageID: pageID))
        let engine = try makeEngine()
        await engine.pushPending()
        let first = store.localRevision(notebookPath: notebook.path, pageID: pageID)

        tick()
        try store.writePageText(
            reading("recieve budget", forRevision: first), notebookPath: notebook.path,
            pageID: pageID)
        try store.correctPageText(
            notebookID: notebook.id, pageID: pageID, correctedTranscript: "receive budget",
            corrections: [correction])
        await engine.pushPending()
        func stored() throws -> MockServerState.Page {
            try XCTUnwrap(
                MockPaperServer.state.withState { $0["paper/Work/Ideas.paper"]?.pages[pageID] })
        }
        func uploaded() throws -> [String: Any] {
            try XCTUnwrap(
                JSONSerialization.jsonObject(with: XCTUnwrap(try stored().text)) as? [String: Any])
        }
        XCTAssertEqual(try uploaded()["corrected_transcript"] as? String, "receive budget")
        XCTAssertEqual(try uploaded()["corrections_revision"] as? Int, first)

        tick()
        try store.writeDrawing(Data("ink two".utf8), notebookPath: notebook.path, pageID: pageID)
        try store.journal.append(
            .init(kind: .pagePut, notebookPath: notebook.path, pageID: pageID))
        await engine.pushPending()
        XCTAssertEqual(try stored().revision, first + 1)
        XCTAssertFalse(
            try stored().lastPutParts.contains("text"),
            "the old reading must not be filed against the new ink")
        XCTAssertEqual(try stored().textRevision, first)

        tick()
        try store.writePageText(
            reading("recieve budgte\ndentist tuesday", forRevision: first + 1),
            notebookPath: notebook.path, pageID: pageID)
        try store.journal.append(
            .init(kind: .pageText, notebookPath: notebook.path, pageID: pageID))
        await engine.pushPending()
        XCTAssertEqual(try stored().lastPutParts, ["text"])
        XCTAssertEqual(try stored().lastPutForRevision, first + 1)
        XCTAssertTrue(try uploaded()["corrected_transcript"] is NSNull)
        XCTAssertEqual(
            (try uploaded()["stale_correction"] as? [String: Any])?["corrected_transcript"]
                as? String, "receive budget")
    }
}
