import CoreGraphics
import Foundation
import ImageIO
import PencilKit
import XCTest

@testable import PaperCore

/// Strokes as points in canvas space: the start and the end of each, in
/// drawing order, with the stroke's own transform applied.
private func anchors(_ data: Data?) throws -> [CGPoint] {
    guard let data, !data.isEmpty else { return [] }
    return try PKDrawing(data: data).strokes.flatMap { stroke -> [CGPoint] in
        guard let first = stroke.path.first, let last = stroke.path.last else { return [] }
        return [first.location.applying(stroke.transform), last.location.applying(stroke.transform)]
    }
}

private func moved(_ points: [CGPoint], by translation: CGSize) -> [CGPoint] {
    points.map { CGPoint(x: $0.x + translation.width, y: $0.y + translation.height) }
}

private func assertSame(
    _ actual: [CGPoint], _ expected: [CGPoint], accuracy: CGFloat = 0.01,
    file: StaticString = #filePath, line: UInt = #line
) {
    XCTAssertEqual(actual.count, expected.count, "stroke count", file: file, line: line)
    for (a, e) in zip(actual, expected) {
        XCTAssertEqual(a.x, e.x, accuracy: accuracy, file: file, line: line)
        XCTAssertEqual(a.y, e.y, accuracy: accuracy, file: file, line: line)
    }
}

private func stroke(_ x0: CGFloat, _ y0: CGFloat, _ x1: CGFloat, _ y1: CGFloat) -> PKStroke {
    InkFixtures.stroke(from: CGPoint(x: x0, y: y0), to: CGPoint(x: x1, y: y1), width: 4)
}

/// Convert to Whiteboard through a real store on the Mac: real `PKDrawing`
/// data, real files, the journal and the snapshot ring.
final class ConvertTests: XCTestCase {
    private var root: URL!
    private var store: PaperStore!

    override func setUpWithError() throws {
        try super.setUpWithError()
        root = FileManager.default.temporaryDirectory
            .appending(path: "ConvertTests-\(UUID().uuidString)", directoryHint: .isDirectory)
        store = try PaperStore(root: root)
    }

    override func tearDownWithError() throws {
        store = nil
        try? FileManager.default.removeItem(at: root)
        try super.tearDownWithError()
    }

    // MARK: - Helpers

    /// Every file of a package with its bytes, by path inside the package.
    private func files(of notebookPath: String) -> [String: Data] {
        let base = store.paths.url(forVaultPath: notebookPath)
        var result: [String: Data] = [:]
        let enumerator = FileManager.default.enumerator(
            at: base, includingPropertiesForKeys: [.isRegularFileKey])
        while let url = enumerator?.nextObject() as? URL {
            guard (try? url.resourceValues(forKeys: [.isRegularFileKey]))?.isRegularFile == true
            else { continue }
            let relative = url.path(percentEncoded: false)
                .replacingOccurrences(of: base.path(percentEncoded: false), with: "")
            result[relative] = try? Data(contentsOf: url)
        }
        return result
    }

    private func leftovers(_ notebookPath: String) -> [String] {
        ((try? FileManager.default.contentsOfDirectory(
            atPath: store.paths.pagesDirectory(notebookPath).path(percentEncoded: false))) ?? [])
            .filter { $0.contains("~") }
    }

    private func converts(_ ops: [PaperOp], pageID: String) -> Int {
        ops.filter { $0.kind == .pagePut && $0.pageID == pageID && $0.payload["metadata"] == "convert" }
            .count
    }

    // MARK: - Paged notebooks

    func testAPagedNotebookBecomesAWhiteboardKeepingEveryPageAndWhatIsOnIt() throws {
        let notebook = try store.createNotebook(title: "Midterm", template: .ruled, size: .a4, pageCount: 3)
        let ids = notebook.pages
        _ = try store.setTags(notebookID: notebook.id, ["exam", "math"])
        _ = try store.setFavorite(notebookID: notebook.id, true)
        _ = try store.setPageTitle(notebookID: notebook.id, pageID: ids[1], title: "Proofs")
        _ = try store.setPageBookmarked(notebookID: notebook.id, pageID: ids[2], true)
        let note = PageObject(
            type: .text, frame: PageObject.Frame(x: 60, y: 600, width: 200, height: 40), text: "Note")
        _ = try store.setObjects([note], notebookPath: notebook.path, pageID: ids[0])
        let first = InkFixtures.data([stroke(100, 200, 400, 220), stroke(120, 300, 500, 310)])
        let second = InkFixtures.data([stroke(50, 50, 545, 790)])
        try store.writeDrawing(first, notebookPath: notebook.path, pageID: ids[0])
        try store.writeDrawing(second, notebookPath: notebook.path, pageID: ids[1])
        let before = try store.pages(ofNotebook: notebook.id)
        let plans = try store.planWhiteboardConversion(notebookID: notebook.id)
        XCTAssertEqual(plans.map(\.pageID), ids)

        let converted = try store.convertToWhiteboard(notebookID: notebook.id)

        XCTAssertEqual(converted.id, notebook.id)
        XCTAssertEqual(converted.path, notebook.path)
        XCTAssertEqual(converted.kind, .whiteboard)
        XCTAssertEqual(converted.paperSize, .board)
        XCTAssertEqual(converted.template, .blank, "board 1's background: Ruled becomes Blank")
        XCTAssertEqual(converted.tags, ["exam", "math"])
        XCTAssertTrue(converted.favorite)
        XCTAssertEqual(converted.pages, ids)
        let onDisk = try AtomicFile.readJSON(Notebook.self, at: store.paths.notebookJSON(notebook.path))
        XCTAssertEqual(onDisk.kind, .whiteboard)
        XCTAssertEqual(onDisk.paperSize, .board)

        let after = try store.pages(ofNotebook: notebook.id)
        XCTAssertEqual(after.map(\.title), ["Board 1", "Proofs", "Board 3"])
        XCTAssertEqual(after.map(\.bookmarked), [false, false, true])
        for ((old, new), plan) in zip(zip(before, after), plans) {
            XCTAssertEqual(new.id, old.id)
            XCTAssertEqual(new.created, old.created)
            XCTAssertEqual(new.revision, old.revision)
            XCTAssertEqual(new.size, .board)
            XCTAssertEqual(new.boardExtent, plan.extent)
            XCTAssertNil(new.customSize)
            XCTAssertEqual(new.template, .blank)
            XCTAssertEqual(new.objects.map(\.id), old.objects.map(\.id))
            for (o, n) in zip(old.objects, new.objects) {
                XCTAssertEqual(n.frame.x, o.frame.x + Double(plan.translation.width), accuracy: 1e-9)
                XCTAssertEqual(n.frame.y, o.frame.y + Double(plan.translation.height), accuracy: 1e-9)
            }
            assertSame(
                try anchors(store.drawingData(notebookPath: notebook.path, pageID: new.id)),
                moved(
                    try anchors(new.id == ids[0] ? first : new.id == ids[1] ? second : nil),
                    by: plan.translation))
        }
        // The content of page 1 sits round the centre of its board.
        let ink = try XCTUnwrap(
            WhiteboardConversion.inkBounds(of: store.drawingData(notebookPath: notebook.path, pageID: ids[0])))
        let content = try XCTUnwrap(BoardGeometry.contentBounds(ink: ink, objects: after[0].objects))
        XCTAssertEqual(content.midX, 8192, accuracy: 1)
        XCTAssertEqual(content.midY, 8192, accuracy: 1)

        // The index follows the files.
        XCTAssertEqual(try store.notebooks().first { $0.id == notebook.id }?.kind, .whiteboard)

        // One whole-page PUT per page, and the notebook patch with the three fields.
        let queue = try store.journal.queue()
        for id in ids { XCTAssertEqual(converts(queue, pageID: id), 1) }
        let patch = try XCTUnwrap(queue.last)
        XCTAssertEqual(patch.kind, .notebookPatch)
        XCTAssertEqual(patch.payload["kind"], "whiteboard")
        XCTAssertEqual(patch.payload["paperSize"], "board")
        XCTAssertEqual(patch.payload["template"], "blank")
        XCTAssertEqual(patch.payload["favorite"], "true", "the queued favourite rides the same patch")

        // History holds the ink as it was.
        let ring = SnapshotRing(paths: store.paths, database: store.database)
        let snapshot = try XCTUnwrap(try ring.snapshots(pageID: ids[0]).first)
        XCTAssertEqual(try Data(contentsOf: snapshot.url), first)
        XCTAssertEqual(try Data(contentsOf: XCTUnwrap(try ring.snapshots(pageID: ids[1]).first).url), second)

        // It opens on the page that was showing, centred, at 100 %.
        let state = store.viewState(notebookID: notebook.id)
        XCTAssertEqual(state.lastPageID, ids[0])
        XCTAssertEqual(state.zoom, 1)
        XCTAssertEqual(state.offset, CGPoint(x: 8192, y: 8192))
        XCTAssertEqual(leftovers(notebook.path), [])
    }

    func testTheStaleRenderGoesAndTheTranscriptBoxesMoveWithTheInk() throws {
        let notebook = try store.createNotebook(title: "Read", template: .blank, size: .a4, pageCount: 1)
        let id = notebook.pages[0]
        try store.writeDrawing(
            InkFixtures.data([stroke(100, 100, 300, 120)]), notebookPath: notebook.path, pageID: id)
        try AtomicFile.write(Data("png".utf8), to: store.paths.render(notebook.path, id))
        try store.writePageText(
            PageText(
                lines: [
                    RecognizedLine(
                        text: "hello", box: PageObject.Frame(x: 100, y: 95, width: 200, height: 30),
                        confidence: 0.9)
                ], transcript: "hello"),
            notebookPath: notebook.path, pageID: id)
        let plan = try XCTUnwrap(try store.planWhiteboardConversion(notebookID: notebook.id).first)

        try store.convertToWhiteboard(notebookID: notebook.id)

        XCTAssertFalse(
            FileManager.default.fileExists(
                atPath: store.paths.render(notebook.path, id).path(percentEncoded: false)),
            "a render of the page is not a render of the board")
        let text = try XCTUnwrap(store.pageText(notebookPath: notebook.path, pageID: id))
        XCTAssertEqual(text.transcript, "hello")
        XCTAssertEqual(text.lines.first?.box.x ?? 0, 100 + Double(plan.translation.width), accuracy: 1e-9)
        XCTAssertEqual(text.lines.first?.box.y ?? 0, 95 + Double(plan.translation.height), accuracy: 1e-9)
        XCTAssertFalse(
            store.transcriptPredatesInk(notebookPath: notebook.path, pageID: id),
            "a reading of the ink stays one, and goes up with the board")
        // Search still finds the page by its id.
        XCTAssertEqual(store.textIndex.search("hello").first?.pages.first?.pageID, id)
    }

    /// A reading of older ink is waiting to be read again; converting does
    /// not make it look like a reading of the board.
    func testATranscriptOlderThanTheInkStaysOlder() throws {
        let notebook = try store.createNotebook(title: "Stale", template: .blank, size: .a4, pageCount: 1)
        let id = notebook.pages[0]
        try store.writePageText(
            PageText(
                lines: [
                    RecognizedLine(
                        text: "old", box: PageObject.Frame(x: 10, y: 10, width: 50, height: 20),
                        confidence: 0.9)
                ], transcript: "old"),
            notebookPath: notebook.path, pageID: id)
        try store.writeDrawing(
            InkFixtures.data([stroke(100, 100, 300, 120)]), notebookPath: notebook.path, pageID: id)
        XCTAssertTrue(store.transcriptPredatesInk(notebookPath: notebook.path, pageID: id))

        try store.convertToWhiteboard(notebookID: notebook.id)

        XCTAssertTrue(store.transcriptPredatesInk(notebookPath: notebook.path, pageID: id))
        XCTAssertNil(store.textData(notebookPath: notebook.path, pageID: id), "not sent with the board")
    }

    // MARK: - Infinite pages

    /// The acceptance test: a page that grew left and up several times keeps
    /// every stroke the same distance from every other, to the point.
    func testAnInfinitePageGrownLeftAndUpKeepsEveryStrokeTheSameDistanceFromEveryOther() throws {
        let notebook = try store.createNotebook(title: "Ideas", template: .grid, size: .infinite, pageCount: 2)
        let id = notebook.pages[0]
        var extent = InfiniteCanvas.baseExtent
        var drawing = PKDrawing()
        var objects = [
            PageObject(
                type: .text, frame: PageObject.Frame(x: 400, y: 500, width: 220, height: 40), text: "Plan")
        ]
        var grewLeftOrUp = 0
        // Each write lands near the top or the left edge, as when writing on
        // past the start of the page; the page grows there and moves
        // everything already on it, as the canvas does.
        let writes: [PKStroke] = [
            stroke(300, 300, 700, 340), stroke(40, 60, 260, 80), stroke(20, 30, 180, 200),
            stroke(900, 10, 1100, 40), stroke(5, 700, 90, 900), stroke(12, 14, 400, 20),
            stroke(1500, 2500, 1900, 2600),
        ]
        for write in writes {
            drawing.strokes.append(write)
            let content = InfiniteCanvas.contentBounds(ink: drawing.bounds, objects: objects)
            guard
                let growth = InfiniteCanvas.growth(
                    extent: extent, content: content, viewport: CGSize(width: 1024, height: 1366),
                    quantum: TemplateGeometry.growthQuantum(for: .grid))
            else { continue }
            extent = growth.extent
            guard !growth.isPureExtension else { continue }
            grewLeftOrUp += 1
            drawing = drawing.transformed(
                using: CGAffineTransform(
                    translationX: growth.translation.width, y: growth.translation.height))
            objects = objects.map { WhiteboardConversion.translated($0, by: growth.translation) }
        }
        XCTAssertGreaterThanOrEqual(grewLeftOrUp, 3, "the page grew left or up several times")
        try store.writeDrawing(drawing.dataRepresentation(), notebookPath: notebook.path, pageID: id)
        _ = try store.setObjects(objects, notebookPath: notebook.path, pageID: id)
        _ = try store.setPageExtent(extent, notebookPath: notebook.path, pageID: id)
        let before = try anchors(store.drawingData(notebookPath: notebook.path, pageID: id))
        let objectBefore = CGPoint(x: objects[0].frame.x, y: objects[0].frame.y)

        try store.convertToWhiteboard(notebookID: notebook.id)

        let after = try anchors(store.drawingData(notebookPath: notebook.path, pageID: id))
        XCTAssertEqual(after.count, before.count)
        func distance(_ a: CGPoint, _ b: CGPoint) -> CGFloat { hypot(a.x - b.x, a.y - b.y) }
        for i in before.indices {
            for j in before.indices where j > i {
                XCTAssertEqual(
                    distance(after[i], after[j]), distance(before[i], before[j]), accuracy: 0.01,
                    "strokes \(i / 2) and \(j / 2) moved apart")
            }
        }
        // The text keeps its place beside the ink.
        let board = try store.page(notebookPath: notebook.path, id: id)
        let objectAfter = CGPoint(x: board.objects[0].frame.x, y: board.objects[0].frame.y)
        for i in before.indices {
            XCTAssertEqual(after[i].x - objectAfter.x, before[i].x - objectBefore.x, accuracy: 0.01)
            XCTAssertEqual(after[i].y - objectAfter.y, before[i].y - objectBefore.y, accuracy: 0.01)
        }
        // Centred on a board that kept its square grid and lost `size_points`.
        XCTAssertEqual(board.size, .board)
        XCTAssertNil(board.customSize)
        XCTAssertEqual(board.template, .grid)
        let ink = try XCTUnwrap(
            WhiteboardConversion.inkBounds(of: store.drawingData(notebookPath: notebook.path, pageID: id)))
        let content = try XCTUnwrap(BoardGeometry.contentBounds(ink: ink, objects: board.objects))
        let center = BoardGeometry.center(of: board.pointSize)
        XCTAssertEqual(content.midX, center.x, accuracy: 1)
        XCTAssertEqual(content.midY, center.y, accuracy: 1)
        // The empty second page is centred by its page rect.
        let empty = try store.page(notebookPath: notebook.path, id: notebook.pages[1])
        XCTAssertEqual(empty.size, .board)
        XCTAssertEqual(empty.title, "Board 2")
    }

    // MARK: - PDF pages, covers, colours

    func testAPDFPageBecomesItsPictureUnderItsInk() throws {
        let notebook = try store.createNotebook(title: "Reading", pageCount: 0)
        let imported = try PDFImporter(store: store).importPDF(
            data: PDFFixture.threePages(), name: "fixture.pdf", into: notebook.id)
        let page = imported.pages[0]
        let ink = InkFixtures.data([stroke(60, 300, 400, 320)])
        try store.writeDrawing(ink, notebookPath: notebook.path, pageID: page.id)
        let plan = try XCTUnwrap(try store.planWhiteboardConversion(notebookID: notebook.id).first)

        try store.convertToWhiteboard(notebookID: notebook.id)

        let board = try store.page(notebookPath: notebook.path, id: page.id)
        XCTAssertNil(board.pdf)
        XCTAssertEqual(board.size, .board)
        let picture = try XCTUnwrap(board.objects.first)
        XCTAssertEqual(picture.type, .image)
        XCTAssertEqual(picture.frame, plan.pdfImageFrame)
        let path = try XCTUnwrap(picture.asset)
        XCTAssertTrue(path.hasPrefix("\(notebook.path)/assets/page-1-"), path)
        XCTAssertTrue(path.hasSuffix(".png"))
        // The page at 2x, the text near its top, the rest paper.
        let png = try XCTUnwrap(store.assets.data(forAssetPath: path, inNotebook: notebook.path))
        let pixels = try RenderedPixels(png: png)
        XCTAssertEqual(pixels.width, 1190)
        XCTAssertEqual(pixels.height, 1684)
        var darkest = 255
        for y in stride(from: 80, to: 320, by: 2) {
            for x in stride(from: 80, to: 700, by: 2) { darkest = min(darkest, pixels.brightness(x: x, y: y)) }
        }
        XCTAssertLessThan(darkest, 100, "the PDF's text is in the picture, the right way up")
        XCTAssertGreaterThan(pixels.brightness(x: 600, y: 1600), 240)
        // The PDF itself is kept, and the picture goes up before the page.
        XCTAssertTrue(store.assets.exists(assetPath: imported.assetPath, inNotebook: notebook.path))
        // The whole queue: the notebook patch folded older ones into itself.
        let queued = try store.journal.queue()
        let upload = try XCTUnwrap(
            queued.firstIndex { $0.kind == .assetPut && $0.payload["path"] == path })
        let put = try XCTUnwrap(
            queued.firstIndex { $0.kind == .pagePut && $0.pageID == page.id && $0.payload["metadata"] == "convert" })
        XCTAssertLessThan(upload, put)
        // Ink still over the page's text, moved with it.
        assertSame(
            try anchors(store.drawingData(notebookPath: notebook.path, pageID: page.id)),
            moved(try anchors(ink), by: plan.translation))
        // The other pages' pictures are their own pages.
        let third = try store.page(notebookPath: notebook.path, id: imported.pages[2].id)
        XCTAssertTrue(third.objects.first?.asset?.hasPrefix("\(notebook.path)/assets/page-3-") == true)
    }

    func testACoverBecomesAWhiteBlankBoardAndADarkPageStaysDark() throws {
        var options = NewDocumentOptions.notebookFactory
        options.cover = true
        options.coverColor = .navy
        options.template = .grid
        options.paperColor = .dark
        let notebook = try store.createDocument(
            title: "Term", folderPath: "paper", kind: .notebook, options: options)
        XCTAssertEqual(notebook.pages.count, 2)

        let converted = try store.convertToWhiteboard(notebookID: notebook.id)

        let pages = try store.pages(ofNotebook: notebook.id)
        XCTAssertEqual(pages.map(\.template), [.blank, .grid])
        XCTAssertEqual(pages.map(\.paperColor), [.white, .dark])
        XCTAssertEqual(pages.map(\.size), [.board, .board])
        XCTAssertEqual(converted.template, .blank)
        XCTAssertEqual(converted.paperColor, .dark, "new boards keep the notebook's colour")
    }

    func testAConflictSiblingConvertsBesideItsOriginalAndKeepsConflictOf() throws {
        let notebook = try store.createNotebook(title: "Twice", template: .blank, size: .a4, pageCount: 2)
        let original = notebook.pages[0]
        let siblingID = "\(original).conflict-20261005120000"
        _ = try store.insertPage(
            Page(id: siblingID, template: .blank, size: .a4, conflictOf: original),
            notebookID: notebook.id, after: original)
        try store.writeDrawing(
            InkFixtures.data([stroke(10, 10, 200, 40)]), notebookPath: notebook.path, pageID: original)
        try store.writeDrawing(
            InkFixtures.data([stroke(30, 30, 220, 60)]), notebookPath: notebook.path, pageID: siblingID)

        let converted = try store.convertToWhiteboard(notebookID: notebook.id)

        XCTAssertEqual(converted.pages, [original, siblingID, notebook.pages[1]])
        let sibling = try store.page(notebookPath: notebook.path, id: siblingID)
        XCTAssertEqual(sibling.size, .board)
        XCTAssertEqual(sibling.conflictOf, original)
        XCTAssertEqual(sibling.title, "Board 2")
    }

    func testAWhiteboardIsLeftAsItIs() throws {
        let whiteboard = try store.createDocument(
            title: "Plan", folderPath: "paper", kind: .whiteboard,
            options: NewDocumentOptions.whiteboardFactory)
        let filesBefore = files(of: whiteboard.path)
        let opsBefore = try store.journal.queue()

        let result = try store.convertToWhiteboard(notebookID: whiteboard.id)

        XCTAssertEqual(result.kind, .whiteboard)
        XCTAssertEqual(files(of: whiteboard.path), filesBefore)
        XCTAssertEqual(try store.journal.queue(), opsBefore)
    }

    // MARK: - Refusals

    func testAPageTooLargeIsRefusedAndNothingChanges() throws {
        let notebook = try store.createNotebook(title: "Wall", template: .blank, size: .infinite, pageCount: 2)
        let wide = notebook.pages[1]
        try store.writeDrawing(
            InkFixtures.data([stroke(100, 300, 400, 320)]), notebookPath: notebook.path,
            pageID: notebook.pages[0])
        try store.writeDrawing(
            InkFixtures.data([stroke(100, 100, 31_200, 140)]), notebookPath: notebook.path, pageID: wide)
        _ = try store.setPageExtent(
            CGSize(width: 31_400, height: 2_000), notebookPath: notebook.path, pageID: wide)
        let filesBefore = files(of: notebook.path)
        let opsBefore = try store.journal.queue()

        for attempt in [
            { _ = try self.store.planWhiteboardConversion(notebookID: notebook.id) },
            { _ = try self.store.convertToWhiteboard(notebookID: notebook.id) },
        ] {
            XCTAssertThrowsError(try attempt()) { error in
                XCTAssertEqual(error as? WhiteboardConversionError, .tooLarge(pageID: wide))
            }
        }
        XCTAssertEqual(files(of: notebook.path), filesBefore)
        XCTAssertEqual(try store.journal.queue(), opsBefore)
        let ring = SnapshotRing(paths: store.paths, database: store.database)
        XCTAssertEqual(try ring.snapshots(pageID: notebook.pages[0]), [], "nothing was snapshotted")
        XCTAssertEqual(try store.notebook(id: notebook.id).kind, .notebook)
    }

    func testInkThatDoesNotDecodeIsRefusedAndNothingChanges() throws {
        let notebook = try store.createNotebook(title: "Bad", template: .blank, size: .a4, pageCount: 2)
        try store.writeDrawing(
            InkFixtures.data([stroke(100, 300, 400, 320)]), notebookPath: notebook.path,
            pageID: notebook.pages[0])
        try store.writeDrawing(Data("not a drawing".utf8), notebookPath: notebook.path, pageID: notebook.pages[1])
        let filesBefore = files(of: notebook.path)
        let opsBefore = try store.journal.queue()

        XCTAssertThrowsError(try store.convertToWhiteboard(notebookID: notebook.id))

        XCTAssertEqual(files(of: notebook.path), filesBefore)
        XCTAssertEqual(try store.journal.queue(), opsBefore)
    }

    func testAPDFPageWhoseDocumentIsMissingIsRefusedAndNothingChanges() throws {
        let notebook = try store.createNotebook(title: "Gone", pageCount: 0)
        let imported = try PDFImporter(store: store).importPDF(
            data: PDFFixture.threePages(), name: "fixture.pdf", into: notebook.id)
        try FileManager.default.removeItem(
            at: store.assets.url(forAssetPath: imported.assetPath, inNotebook: notebook.path))
        let filesBefore = files(of: notebook.path)
        let opsBefore = try store.journal.queue()

        XCTAssertThrowsError(try store.convertToWhiteboard(notebookID: notebook.id)) { error in
            XCTAssertEqual(error as? PaperStoreError, .notFound(imported.assetPath))
        }

        XCTAssertEqual(files(of: notebook.path), filesBefore)
        XCTAssertEqual(try store.journal.queue(), opsBefore)
    }

    // MARK: - Interrupted

    private struct Fixture {
        let notebook: Notebook
        let pages: [Page]
        let inks: [String: Data]
        let plans: [String: BoardPlan]
    }

    /// Five pages: an infinite page with ink, a text object, a title, a
    /// bookmark and a transcript; three PDF pages, the first written on; an
    /// empty page.
    private func interruptible() throws -> Fixture {
        let notebook = try store.createNotebook(title: "Course", template: .grid, size: .infinite, pageCount: 1)
        let first = notebook.pages[0]
        let ink = InkFixtures.data([stroke(100, 200, 500, 240), stroke(200, 900, 700, 980)])
        try store.writeDrawing(ink, notebookPath: notebook.path, pageID: first)
        _ = try store.setObjects(
            [PageObject(type: .text, frame: PageObject.Frame(x: 80, y: 40, width: 200, height: 30), text: "Week 1")],
            notebookPath: notebook.path, pageID: first)
        _ = try store.setPageTitle(notebookID: notebook.id, pageID: first, title: "Week 1")
        _ = try store.setPageBookmarked(notebookID: notebook.id, pageID: first, true)
        try store.writePageText(
            PageText(
                lines: [
                    RecognizedLine(
                        text: "week", box: PageObject.Frame(x: 100, y: 200, width: 400, height: 40),
                        confidence: 0.9)
                ], transcript: "week"),
            notebookPath: notebook.path, pageID: first)
        let imported = try PDFImporter(store: store).importPDF(
            data: PDFFixture.threePages(), name: "fixture.pdf", into: notebook.id, after: first)
        let pdfInk = InkFixtures.data([stroke(60, 300, 400, 320)])
        try store.writeDrawing(pdfInk, notebookPath: notebook.path, pageID: imported.pages[0].id)
        _ = try store.createPage(notebookID: notebook.id)
        let pages = try store.pages(ofNotebook: notebook.id)
        XCTAssertEqual(pages.count, 5)
        let plans = try store.planWhiteboardConversion(notebookID: notebook.id)
        return Fixture(
            notebook: try store.notebook(id: notebook.id), pages: pages,
            inks: [first: ink, imported.pages[0].id: pdfInk],
            plans: Dictionary(uniqueKeysWithValues: plans.map { ($0.pageID, $0) }))
    }

    /// Each page is wholly the page it was or wholly its board.
    private func assertWhole(
        _ fixture: Fixture, boards expected: Int, file: StaticString = #filePath, line: UInt = #line
    ) throws {
        let path = fixture.notebook.path
        for (index, original) in fixture.pages.enumerated() {
            let current = try store.page(notebookPath: path, id: original.id)
            let ink = store.drawingData(notebookPath: path, pageID: original.id)
            let plan = try XCTUnwrap(fixture.plans[original.id])
            if index < expected {
                XCTAssertEqual(current.size, .board, "page \(index)", file: file, line: line)
                XCTAssertNil(current.pdf, file: file, line: line)
                assertSame(
                    try anchors(ink), moved(try anchors(fixture.inks[original.id]), by: plan.translation),
                    file: file, line: line)
                let objects = original.objects.map { WhiteboardConversion.translated($0, by: plan.translation) }
                XCTAssertEqual(
                    current.objects.filter { $0.type == .text }, objects.filter { $0.type == .text },
                    file: file, line: line)
            } else {
                XCTAssertEqual(current, original, "page \(index) changed", file: file, line: line)
                XCTAssertEqual(ink, fixture.inks[original.id], "page \(index)'s ink changed", file: file, line: line)
            }
        }
        XCTAssertEqual(leftovers(path), [], file: file, line: line)
    }

    /// The conversion's end state, however many runs it took.
    private func assertConverted(_ fixture: Fixture, file: StaticString = #filePath, line: UInt = #line) throws {
        let path = fixture.notebook.path
        try assertWhole(fixture, boards: fixture.pages.count, file: file, line: line)
        let notebook = try store.notebook(id: fixture.notebook.id)
        XCTAssertEqual(notebook.kind, .whiteboard, file: file, line: line)
        XCTAssertEqual(notebook.pages, fixture.pages.map(\.id), file: file, line: line)
        XCTAssertEqual(
            try AtomicFile.readJSON(Notebook.self, at: store.paths.notebookJSON(path)).kind, .whiteboard,
            file: file, line: line)
        let pdfBoard = try store.page(notebookPath: path, id: fixture.pages[1].id)
        XCTAssertEqual(pdfBoard.objects.filter { $0.type == .image }.count, 1, "one picture", file: file, line: line)
        let pictureExists = pdfBoard.objects.first?.asset.map {
            store.assets.exists(assetPath: $0, inNotebook: path)
        }
        XCTAssertEqual(pictureExists, true, file: file, line: line)
        let first = try store.page(notebookPath: path, id: fixture.pages[0].id)
        XCTAssertEqual(first.title, "Week 1", file: file, line: line)
        XCTAssertTrue(first.bookmarked, file: file, line: line)
        let queue = try store.journal.queue()
        for page in fixture.pages {
            XCTAssertEqual(converts(queue, pageID: page.id), 1, "PUT for \(page.id)", file: file, line: line)
        }
        XCTAssertTrue(
            queue.contains { $0.kind == .notebookPatch && $0.payload["kind"] == "whiteboard" },
            file: file, line: line)
        let state = store.viewState(notebookID: notebook.id)
        XCTAssertEqual(state.zoom, 1, file: file, line: line)
        XCTAssertEqual(state.offset, CGPoint(x: 8192, y: 8192), "centred", file: file, line: line)
    }

    func testAnUninterruptedRunOfTheFixtureConverts() throws {
        let fixture = try interruptible()
        try store.convertToWhiteboard(notebookID: fixture.notebook.id)
        try assertConverted(fixture)
    }

    /// Stopped after each step -- what an app killed there leaves -- every
    /// page is whole, the notebook is still a notebook until its file says
    /// otherwise, and after a relaunch a second run finishes the job without
    /// moving anything twice.
    func testAConversionStoppedAfterAnyStepLeavesWholePagesAndFinishesWhenRunAgain() throws {
        let cases: [(WhiteboardConversionStep, Int)] = [
            (.snapshotsTaken, 0), (.picturesStored, 0),
            (.pageSwapped(0), 1), (.pageJournalled(0), 1),
            (.pageSwapped(1), 2), (.pageJournalled(1), 2),
            (.pageSwapped(2), 3), (.pageJournalled(3), 4),
            (.pageSwapped(4), 5), (.pageJournalled(4), 5),
            (.notebookJournalled, 5), (.notebookWritten, 5),
        ]
        for (step, boards) in cases {
            try? FileManager.default.removeItem(at: root)
            store = try PaperStore(root: root)
            let fixture = try interruptible()

            XCTAssertThrowsError(
                try store.convertToWhiteboard(notebookID: fixture.notebook.id, stoppingAt: step)
            ) { error in
                XCTAssertEqual(error as? WhiteboardConversionInterrupted, .init(step: step))
            }
            try assertWhole(fixture, boards: boards)
            let written = try AtomicFile.readJSON(
                Notebook.self, at: store.paths.notebookJSON(fixture.notebook.path))
            XCTAssertEqual(written.kind, step == .notebookWritten ? .whiteboard : .notebook, "\(step)")

            // Relaunch: a new store over the same files, reconciled.
            store = try PaperStore(root: root)
            _ = try store.reconcile()
            try store.convertToWhiteboard(notebookID: fixture.notebook.id)
            try assertConverted(fixture)
        }
    }
}

// MARK: - Sync

/// A converted notebook through `MockPaperServer`: what goes up, what a second
/// device pulls, and what happens to work queued before it and written
/// elsewhere meanwhile.
final class ConvertSyncTests: XCTestCase {
    private var roots: [URL] = []

    private struct Device {
        let store: PaperStore
        let engine: SyncEngine
    }

    override func setUpWithError() throws {
        try super.setUpWithError()
        MockPaperServer.state.reset()
    }

    override func tearDownWithError() throws {
        MockPaperServer.state.reset()
        for root in roots { try? FileManager.default.removeItem(at: root) }
        roots = []
        try super.tearDownWithError()
    }

    private func device(_ name: String) async throws -> Device {
        let root = FileManager.default.temporaryDirectory
            .appending(path: "ConvertSyncTests-\(name)-\(UUID().uuidString)", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        roots.append(root)
        let store = try PaperStore(root: root)
        let settings = try XCTUnwrap(ServerSettings(urlString: "https://paper.test"))
        let api = PaperAPI(
            settings: settings, auth: AnonymousAuthProvider(), session: MockPaperServer.makeSession())
        let engine = SyncEngine(store: store, api: api, clientID: name, clock: TestClock(), jitter: { $0 })
        return Device(store: store, engine: engine)
    }

    /// Ink written and queued as autosave leaves it.
    private func write(_ strokes: [PKStroke], _ notebook: Notebook, _ pageID: String, on device: Device) throws {
        try device.store.writeDrawing(
            InkFixtures.data(strokes), notebookPath: notebook.path, pageID: pageID)
        try device.store.journal.append(.init(kind: .pagePut, notebookPath: notebook.path, pageID: pageID))
    }

    private func assertDrained(_ device: Device, file: StaticString = #filePath, line: UInt = #line) throws {
        XCTAssertEqual(try device.store.journal.pendingCount(), 0, file: file, line: line)
        XCTAssertEqual(try device.store.journal.parked(), [], file: file, line: line)
    }

    func testQueuedWorkGoesUpWithTheConversionAndASecondDevicePullsAWhiteboard() async throws {
        let a = try await device("A")
        let b = try await device("B")
        let notebook = try a.store.createNotebook(title: "Ideas", template: .grid, size: .infinite, pageCount: 2)
        try write([stroke(100, 100, 400, 140)], notebook, notebook.pages[0], on: a)
        try write([stroke(300, 500, 600, 520)], notebook, notebook.pages[1], on: a)
        await a.engine.syncNow()
        try assertDrained(a)
        await b.engine.syncNow()
        XCTAssertEqual(try b.store.notebook(id: notebook.id).kind, .notebook)

        // Work A has not pushed: more ink, a favourite, a new page with ink.
        try write([stroke(100, 100, 400, 140), stroke(120, 200, 420, 210)], notebook, notebook.pages[0], on: a)
        _ = try a.store.setFavorite(notebookID: notebook.id, true)
        let added = try a.store.createPage(notebookID: notebook.id)
        try write([stroke(50, 60, 250, 90)], notebook, added.id, on: a)

        let converted = try a.store.convertToWhiteboard(notebookID: notebook.id)
        let ids = converted.pages
        XCTAssertEqual(ids, notebook.pages + [added.id])
        // The conversion's PUT replaced the PUT queued for the same page; the
        // new page's create stays ahead of it.
        let puts = try a.store.journal.pending().filter { $0.kind == .pagePut }
        for id in ids {
            XCTAssertEqual(puts.filter { $0.pageID == id }.count, 1, "one PUT for \(id)")
            XCTAssertEqual(puts.first { $0.pageID == id }?.payload["metadata"], "convert")
        }
        XCTAssertTrue(try a.store.journal.pending().contains { $0.kind == .pageCreate && $0.pageID == added.id })

        await a.engine.syncNow()
        try assertDrained(a)

        // The server holds a whiteboard of boards.
        let server = try XCTUnwrap(MockPaperServer.state.withState { $0[notebook.path] })
        XCTAssertEqual(server.kind, "whiteboard")
        XCTAssertEqual(server.paperSize, "board")
        XCTAssertEqual(server.template, "grid")
        XCTAssertTrue(server.favorite)
        XCTAssertEqual(server.order, ids)
        for (index, id) in ids.enumerated() {
            let page = try XCTUnwrap(server.pages[id])
            XCTAssertEqual(page.sizeName, "board")
            XCTAssertNil(page.sizePoints, "a board sends no size_points")
            XCTAssertEqual(page.boardExtent?["width"] as? Double, 16_384)
            XCTAssertEqual(page.title, "Board \(index + 1)")
            XCTAssertEqual(page.drawing, a.store.drawingData(notebookPath: notebook.path, pageID: id))
        }

        // B pulls it as a whiteboard with the same page ids.
        await b.engine.syncNow()
        try assertDrained(b)
        let pulled = try b.store.notebook(id: notebook.id)
        XCTAssertEqual(pulled.kind, .whiteboard)
        XCTAssertEqual(pulled.paperSize, .board)
        XCTAssertEqual(pulled.pages, ids)
        for (index, id) in ids.enumerated() {
            let page = try b.store.page(notebookPath: notebook.path, id: id)
            XCTAssertEqual(page.size, .board)
            XCTAssertEqual(page.boardExtent, SizePoints(width: 16_384, height: 16_384))
            XCTAssertNil(page.customSize)
            XCTAssertEqual(page.template, .grid)
            XCTAssertEqual(page.title, "Board \(index + 1)")
            XCTAssertEqual(
                b.store.drawingData(notebookPath: notebook.path, pageID: id),
                a.store.drawingData(notebookPath: notebook.path, pageID: id))
        }
        let ink = try XCTUnwrap(
            WhiteboardConversion.inkBounds(of: b.store.drawingData(notebookPath: notebook.path, pageID: ids[0])))
        XCTAssertEqual(ink.midX, 8192, accuracy: 1)
        XCTAssertEqual(ink.midY, 8192, accuracy: 1)
    }

    /// The conversion ran offline while another device wrote on a page. The
    /// page conflict rule keeps both: the other device's page as the
    /// original, this device's board beside it. Nothing is lost.
    func testAPageWrittenElsewhereDuringAnOfflineConversionKeepsBothVersions() async throws {
        let a = try await device("A")
        let b = try await device("B")
        let notebook = try a.store.createNotebook(title: "Shared", template: .blank, size: .infinite, pageCount: 2)
        try write([stroke(100, 100, 400, 140)], notebook, notebook.pages[0], on: a)
        await a.engine.syncNow()
        await b.engine.syncNow()
        let page = notebook.pages[0]

        try a.store.convertToWhiteboard(notebookID: notebook.id)
        let board = try XCTUnwrap(a.store.drawingData(notebookPath: notebook.path, pageID: page))
        try write([stroke(100, 100, 400, 140), stroke(500, 600, 700, 640)], notebook, page, on: b)
        await b.engine.syncNow()
        let theirs = try XCTUnwrap(b.store.drawingData(notebookPath: notebook.path, pageID: page))

        await a.engine.syncNow()
        try assertDrained(a)

        let pages = try a.store.notebook(id: notebook.id).pages
        XCTAssertEqual(pages.count, 3)
        let sibling = try XCTUnwrap(pages.first { $0.hasPrefix("\(page).conflict-") })
        XCTAssertEqual(pages, [page, sibling, notebook.pages[1]])
        XCTAssertEqual(a.store.drawingData(notebookPath: notebook.path, pageID: page), theirs)
        XCTAssertEqual(a.store.drawingData(notebookPath: notebook.path, pageID: sibling), board)
        let mine = try a.store.page(notebookPath: notebook.path, id: sibling)
        XCTAssertEqual(mine.size, .board)
        XCTAssertEqual(mine.conflictOf, page)
        XCTAssertEqual(try a.store.notebook(id: notebook.id).kind, .whiteboard)
        let server = try XCTUnwrap(MockPaperServer.state.withState { $0[notebook.path] })
        XCTAssertEqual(server.kind, "whiteboard")
        XCTAssertEqual(server.pages[page]?.drawing, theirs)
        XCTAssertEqual(server.pages[sibling]?.drawing, board)
    }

    /// (B6) A converted PDF page's binding goes from the server too: the
    /// board's page part says `"pdf": null`, which the page patch honours.
    /// It used to leave the key out, the patch kept the server's binding,
    /// and every device that fetched the board got the PDF under its own
    /// picture of it.
    func testAConvertedPDFPageLosesItsBindingUpstreamAndComesDownWithout() async throws {
        let a = try await device("A")
        let b = try await device("B")
        let notebook = try a.store.createNotebook(title: "Slides", pageCount: 0)
        let imported = try PDFImporter(store: a.store).importPDF(
            data: PDFFixture.threePages(), name: "fixture.pdf", into: notebook.id)
        let pageID = imported.pages[0].id
        try write([stroke(60, 300, 400, 320)], notebook, pageID, on: a)
        await a.engine.syncNow()
        try assertDrained(a)
        XCTAssertNotNil(MockPaperServer.state.withState { $0[notebook.path]?.pages[pageID]?.pdf })

        try a.store.convertToWhiteboard(notebookID: notebook.id)
        await a.engine.syncNow()
        try assertDrained(a)

        XCTAssertNil(
            MockPaperServer.state.withState { $0[notebook.path]?.pages[pageID]?.pdf },
            "the server's board still names the PDF")
        await b.engine.syncNow()
        let pulled = try b.store.page(notebookPath: notebook.path, id: pageID)
        XCTAssertEqual(pulled.size, .board)
        XCTAssertEqual(pulled.objects.first?.type, .image, "the PDF's picture")
        XCTAssertNil(pulled.pdf)
    }

    /// A page that has no binding says so.
    func testThePagePartOfAPageWithoutABindingClearsOne() throws {
        let root = FileManager.default.temporaryDirectory
            .appending(path: "ConvertSyncTests-part-\(UUID().uuidString)", directoryHint: .isDirectory)
        roots.append(root)
        let store = try PaperStore(root: root)
        let notebook = try store.createNotebook(title: "Parts", pageCount: 1)
        let part = try XCTUnwrap(
            JSONSerialization.jsonObject(
                with: XCTUnwrap(store.pagePartJSON(notebookPath: notebook.path, pageID: notebook.pages[0])))
                as? [String: Any])
        XCTAssertTrue(part["pdf"] is NSNull)
    }
}
