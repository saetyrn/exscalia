import CoreGraphics
import Foundation
import PencilKit
import XCTest

@testable import PaperCore

/// A render or a reading that finishes after its notebook moved -- another
/// device renamed its folder, and the pull took the directory along -- lands
/// where the notebook is now. It used to write at the old path, which made
/// the old directories again: a ghost folder in the library, and a picture
/// and a transcript the page never got.
final class LateWriteTests: XCTestCase {
    private var root: URL!
    private var store: PaperStore!

    override func setUpWithError() throws {
        try super.setUpWithError()
        root = FileManager.default.temporaryDirectory
            .appending(path: "LateWriteTests-\(UUID().uuidString)", directoryHint: .isDirectory)
        store = try PaperStore(root: root)
    }

    override func tearDownWithError() throws {
        store = nil
        try? FileManager.default.removeItem(at: root)
        try super.tearDownWithError()
    }

    private struct Reading: HandwritingRecognitionBackend {
        func recognize(image: CGImage, pageSize: CGSize, languages: [String]) throws -> PageText {
            PageText(
                lines: [
                    RecognizedLine(
                        text: "moved", box: PageObject.Frame(x: 0, y: 0, width: 10, height: 10),
                        confidence: 0.9)
                ])
        }
    }

    private func exists(_ path: String) -> Bool {
        FileManager.default.fileExists(atPath: store.paths.url(forVaultPath: path).path(percentEncoded: false))
    }

    private func notebookInWork() throws -> (Notebook, Page) {
        let notebook = try store.createNotebook(title: "Ideas", folder: "Work", pageCount: 1)
        let page = try store.page(notebookPath: notebook.path, id: notebook.pages[0])
        try store.writeDrawing(
            InkFixtures.data([InkFixtures.stroke(from: CGPoint(x: 60, y: 80), to: CGPoint(x: 300, y: 120))]),
            notebookPath: notebook.path, pageID: page.id)
        return (notebook, page)
    }

    func testARenderFinishingAfterAFolderMoveLandsWhereTheNotebookIsNow() async throws {
        let (notebook, page) = try notebookInWork()
        let job = PageRenderJob(
            notebookPath: notebook.path, page: page,
            drawing: store.drawingData(notebookPath: notebook.path, pageID: page.id))
        // As a pull applies another device's rename of the folder.
        _ = try store.relocateTree(from: "paper/Work", to: "paper/Jobs")

        await PageRenderer(store: store).render(job)

        XCTAssertTrue(
            FileManager.default.fileExists(
                atPath: store.paths.render("paper/Jobs/Ideas.paper", page.id).path(percentEncoded: false)))
        XCTAssertFalse(exists("paper/Work"), "the old folder was made again")
        XCTAssertEqual(
            store.located(PageRef(notebookID: notebook.id, notebookPath: notebook.path, pageID: page.id))
                .notebookPath,
            "paper/Jobs/Ideas.paper")
    }

    func testAReadingFinishingAfterAFolderMoveIsFiledWhereTheNotebookIsNow() async throws {
        let (notebook, page) = try notebookInWork()
        let image = try RecognitionTests.fixture(
            "moved", size: CGSize(width: 40, height: 40), scale: 1, origin: CGPoint(x: 2, y: 30))
        try RecognitionTests.writePNG(image, to: store.paths.render(notebook.path, page.id))
        let recognizer = HandwritingRecognizer(store: store, idleDelay: 0, backend: Reading())
        let filed = FiledJobs()
        await recognizer.setOnRecognized { filed.append($0) }
        _ = try store.relocateTree(from: "paper/Work", to: "paper/Jobs")

        let read = await recognizer.recognizeNow(RecognitionJob(notebookPath: notebook.path, page: page))

        XCTAssertEqual(read?.transcript, "moved")
        XCTAssertEqual(store.pageText(notebookPath: "paper/Jobs/Ideas.paper", pageID: page.id)?.transcript, "moved")
        XCTAssertFalse(exists("paper/Work"), "the old folder was made again")
        XCTAssertEqual(filed.jobs.map(\.notebookPath), ["paper/Jobs/Ideas.paper"], "journalled at the old path")
        XCTAssertEqual(
            store.textIndex.search("moved").first?.pages.first?.notebookPath, "paper/Jobs/Ideas.paper")
    }

    func testNothingIsWrittenForAPageThatIsNowhere() async throws {
        let (notebook, page) = try notebookInWork()
        let job = PageRenderJob(
            notebookPath: notebook.path, page: page,
            drawing: store.drawingData(notebookPath: notebook.path, pageID: page.id))
        _ = try store.delete(notebookID: notebook.id)

        await PageRenderer(store: store).render(job)
        XCTAssertThrowsError(
            try store.writePageText(PageText(lines: []), notebookPath: notebook.path, pageID: page.id))

        XCTAssertFalse(exists(notebook.path))
    }
}

private final class FiledJobs: @unchecked Sendable {
    private let lock = NSLock()
    private var filed: [RecognitionJob] = []

    var jobs: [RecognitionJob] {
        lock.lock()
        defer { lock.unlock() }
        return filed
    }

    func append(_ job: RecognitionJob) {
        lock.lock()
        filed.append(job)
        lock.unlock()
    }
}
