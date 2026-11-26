import Foundation
import GRDB
import XCTest

@testable import PaperCore

/// Documents: kinds, paper colours and the dark rule, covers, orientation,
/// boards, the new-document defaults, and how all of it reads, writes and
/// travels. Existing notebooks must read exactly as before.
final class DocumentModelTests: PaperStoreTestCase {
    private func object(_ data: Data?) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: XCTUnwrap(data)) as? [String: Any])
    }

    private func ops(_ notebook: Notebook) throws -> [PaperOp] {
        try store.journal.pending().filter { $0.notebookPath == notebook.path }
    }

    private func pages(_ notebook: Notebook) throws -> [Page] {
        try store.pages(ofNotebook: notebook.id)
    }

    private var scratch: ScratchDefaults?
    private var scratchDefaults: UserDefaults!

    override func setUpWithError() throws {
        try super.setUpWithError()
        let scratch = try ScratchDefaults("DocumentModelTests")
        self.scratch = scratch
        scratchDefaults = scratch.defaults
    }

    override func tearDownWithError() throws {
        scratch?.remove()
        scratch = nil
        scratchDefaults = nil
        try super.tearDownWithError()
    }

    // MARK: - Reading what is already on disk

    private let legacyNotebook = #"""
        {"id":"n1","title":"Old","created":"2026-01-01T00:00:00.000Z","modified":"2026-01-02T00:00:00.000Z","template":"grid","paperSize":"infinite","tags":["a"],"favorite":true,"pages":["p1"],"seq":4}
        """#

    func testANotebookWrittenBeforeDocumentsReadsAsAPortraitWhiteNotebook() throws {
        let notebook = try PaperJSON.decode(Notebook.self, from: Data(legacyNotebook.utf8))
        XCTAssertEqual(notebook.kind, .notebook)
        XCTAssertEqual(notebook.orientation, .portrait)
        XCTAssertEqual(notebook.paperColor, .white)
        XCTAssertEqual(notebook.template, .grid)
        XCTAssertEqual(notebook.paperSize, .infinite)
        XCTAssertEqual(notebook.pages, ["p1"])
        XCTAssertTrue(notebook.additionalFields.isEmpty)
    }

    func testUnknownNotebookValuesFallBackAndUnknownKeysSurvive() throws {
        let json = #"""
            {"id":"n1","title":"New","created":"2026-01-01T00:00:00.000Z","modified":"2026-01-01T00:00:00.000Z","template":"hexagonal","paperSize":"tabloid","tags":[],"favorite":false,"pages":[],"seq":0,"kind":"slideshow","orientation":"diagonal","paperColor":"mauve","locked":true,"theme":{"accent":"#ff0000"}}
            """#
        let notebook = try PaperJSON.decode(Notebook.self, from: Data(json.utf8))
        XCTAssertEqual(notebook.kind, .notebook)
        XCTAssertEqual(notebook.orientation, .portrait)
        XCTAssertEqual(notebook.paperColor, .white)
        XCTAssertEqual(notebook.template, .ruled)
        XCTAssertEqual(notebook.paperSize, .a4)
        XCTAssertEqual(Set(notebook.additionalFields.keys), ["locked", "theme"])

        let encoded = try object(PaperJSON.encode(notebook))
        XCTAssertEqual(encoded["locked"] as? Bool, true)
        XCTAssertNotNil(encoded["theme"] as? [String: Any])
        XCTAssertEqual(encoded["kind"] as? String, "notebook")
        XCTAssertNil(encoded["path"])
        XCTAssertEqual(try PaperJSON.decode(Notebook.self, from: PaperJSON.encode(notebook)), notebook)
    }

    func testAStoreWriteKeepsTheKeysANewerBuildAddedToNotebookJSON() throws {
        let notebook = try store.createNotebook(title: "Kept")
        let url = store.paths.notebookJSON(notebook.path)
        var raw = try object(Data(contentsOf: url))
        raw["locked"] = true
        try JSONSerialization.data(withJSONObject: raw).write(to: url)

        _ = try store.setFavorite(notebookID: notebook.id, true)
        _ = try store.createPage(notebookID: notebook.id)
        _ = try store.rename(notebookID: notebook.id, to: "Still kept")
        let renamed = try store.notebook(id: notebook.id)
        let after = try object(Data(contentsOf: store.paths.notebookJSON(renamed.path)))
        XCTAssertEqual(after["locked"] as? Bool, true)
        XCTAssertEqual(after["favorite"] as? Bool, true)
    }

    func testPageFieldsAreOptionalAndFallBack() throws {
        func page(_ fields: String) throws -> Page {
            try PaperJSON.decode(
                Page.self,
                from: Data(#"{"id":"p1","created":"2026-01-01T00:00:00.000Z",\#(fields)}"#.utf8))
        }
        let plain = try page(#""template":"ruled","size":"a4""#)
        XCTAssertEqual(plain.paperColor, .white)
        XCTAssertNil(plain.renderRect)
        XCTAssertNil(plain.boardExtent)
        XCTAssertEqual(try page(#""paper_color":"ultraviolet""#).paperColor, .white)
        XCTAssertEqual(try page(#""paper_color":"Dark""#).paperColor, .dark)
        let misshapen = try page(#""board_extent":"big","render_rect":[1,2]"#)
        XCTAssertNil(misshapen.boardExtent)
        XCTAssertNil(misshapen.renderRect)
        XCTAssertTrue(misshapen.additionalFields.isEmpty, "known keys never become unknown ones")

        let board = try page(
            #""template":"grid","size":"board","paper_color":"cream","board_extent":{"width":8192,"height":4096},"render_rect":{"x":10,"y":20,"width":300,"height":200}"#
        )
        XCTAssertEqual(board.size, .board)
        XCTAssertEqual(board.boardExtent, SizePoints(width: 8192, height: 4096))
        XCTAssertEqual(board.pointSize, CGSize(width: 8192, height: 4096))
        XCTAssertEqual(board.renderRect, PageObject.Frame(x: 10, y: 20, width: 300, height: 200))
        XCTAssertEqual(try PaperJSON.decode(Page.self, from: PaperJSON.encode(board)), board)
        let encoded = try object(PaperJSON.encode(board))
        XCTAssertEqual(encoded["paper_color"] as? String, "cream")
        XCTAssertNotNil(encoded["board_extent"])
        XCTAssertNotNil(encoded["render_rect"])
    }

    func testABoardExtentIsHeldToItsLimits() throws {
        var page = Page(template: .grid, size: .board)
        XCTAssertEqual(page.pointSize, CGSize(width: 16_384, height: 16_384), "absent means default")
        page.boardExtent = SizePoints(width: 100, height: 100_000)
        XCTAssertEqual(page.pointSize, CGSize(width: 1_024, height: 32_768))
        page.customSize = SizePoints(width: 595, height: 842)
        XCTAssertEqual(page.pointSize, CGSize(width: 1_024, height: 32_768), "a board ignores size_points")
    }

    func testShapeObjectsAreAKnownKindAndKeepTheirPayload() throws {
        let json = ##"{"id":"s1","type":"shape","frame":{"x":0,"y":0,"width":160,"height":120},"shape":"rectangle","stroke":"#1c1c1e","strokeWidth":2.0,"fill":null}"##
        let object = try PaperJSON.decode(PageObject.self, from: Data(json.utf8))
        XCTAssertEqual(object.type, .shape)
        XCTAssertEqual(object.additionalFields["shape"], .string("rectangle"))
        XCTAssertEqual(object.additionalFields["fill"], .null)
        let encoded = try self.object(PaperJSON.encode(object))
        XCTAssertEqual(encoded["type"] as? String, "shape")
        XCTAssertEqual(encoded["stroke"] as? String, "#1c1c1e")
        XCTAssertEqual(PageObject.Kind(rawValue: "shape"), .shape)
    }

    // MARK: - Names and presets

    func testSizesAndTemplates() {
        XCTAssertEqual(PaperSize.defaultForNewNotebooks, .iPad)
        XCTAssertEqual(PaperSize.newNotebookChoices, [.a4, .letter, .iPad])
        XCTAssertEqual(PaperSize.allCases.last, .board, "declared last")
        XCTAssertEqual(PaperSize.board.points, CGSize(width: 16_384, height: 16_384))
        XCTAssertEqual(PaperSize.a4.points(in: .landscape), CGSize(width: 842, height: 595))
        XCTAssertEqual(PaperSize.iPad.points(in: .landscape), CGSize(width: 1366, height: 1024))
        XCTAssertEqual(PaperSize.infinite.points(in: .landscape), PaperSize.infinite.points)
        XCTAssertEqual(PaperSize.board.points(in: .landscape), PaperSize.board.points)
        // A board is never what a measurement or a PDF page turns into.
        XCTAssertEqual(PaperSize.nearest(to: CGSize(width: 15_000, height: 15_000)), .iPad)

        XCTAssertEqual(Template.paperTemplates, [.blank, .ruled, .grid, .dotted])
        XCTAssertEqual(Template.boardTemplates, [.blank, .dotted, .grid])
        XCTAssertEqual(Template.paperTemplates.map(\.displayName), ["Blank", "Ruled", "Squared", "Dotted"])
        XCTAssertEqual(
            Template.boardTemplates.map { $0.displayName(for: .whiteboard) },
            ["Blank", "Dot Grid", "Square Grid"])
        XCTAssertEqual(Template.cover.spacing, 0)
        XCTAssertEqual(TemplateGeometry.lines(template: .cover, size: PaperSize.a4.points).count, 0)
        XCTAssertEqual(DocumentKind.whiteboard.displayName, "Whiteboard")
    }

    // MARK: - Paper colours and the dark rule

    func testPaperColoursAndWhichAreDark() {
        XCTAssertEqual(PaperColor.paperChoices, [.white, .cream, .dark])
        XCTAssertEqual(
            PaperColor.coverChoices, [.navy, .graphite, .forest, .oxblood, .slate, .ochre, .sage, .sand])
        let cream = PaperColor.cream.rgb
        XCTAssertEqual(cream.red, 0xF7 / 255, accuracy: 1e-9)
        XCTAssertEqual(cream.green, 0xF7 / 255, accuracy: 1e-9)
        XCTAssertEqual(cream.blue, 0xEA / 255, accuracy: 1e-9)
        XCTAssertEqual(PaperColor.white.hex, 0xFDFCFA, "today's paper")
        // Relative luminance below 0.4. Sage (0.34) and ochre (0.27) are
        // mid-tones on the dark side; sand (0.61) is the one light cover.
        let light: Set<PaperColor> = [.white, .cream, .sand]
        for color in PaperColor.allCases {
            XCTAssertEqual(color.isDark, !light.contains(color), color.rawValue)
        }
        XCTAssertEqual(PaperColor.sage.relativeLuminance, 0.339, accuracy: 0.005)
        XCTAssertEqual(PaperColor.ochre.relativeLuminance, 0.268, accuracy: 0.005)
    }

    func testTheDarkRuleReadsThePagesColour() {
        XCTAssertFalse(PaperAppearance.isDark(page: Page()))
        XCTAssertTrue(PaperAppearance.isDark(page: Page(paperColor: .dark)))
        XCTAssertTrue(PaperAppearance.isDark(page: Page(template: .cover, paperColor: .navy)))
        XCTAssertFalse(PaperAppearance.isDark(page: Page(template: .cover, paperColor: .sand)))
        XCTAssertEqual(PaperAppearance.ruleComponents(on: .white).alpha, 0.12)
        XCTAssertEqual(PaperAppearance.ruleComponents(on: .dark).red, 1)
        XCTAssertEqual(PaperAppearance.ruleComponents(on: .dark).alpha, 0.14)
        XCTAssertEqual(PaperAppearance.coverFrameComponents(on: .navy).alpha, 0.35)
        XCTAssertEqual(PaperAppearance.coverFrameComponents(on: .sand).red, 0)
        XCTAssertEqual(PaperAppearance.defaultTextComponents(on: .white).red, 0x1C / 255, accuracy: 1e-9)
        XCTAssertEqual(PaperAppearance.defaultTextComponents(on: .dark).blue, 0xF7 / 255, accuracy: 1e-9)
    }

    // MARK: - createDocument

    func testANotebookWithTheFactoryOptions() throws {
        let notebook = try store.createDocument(
            title: "Lecture", folderPath: "paper", kind: .notebook, options: .notebookFactory)
        XCTAssertEqual(notebook.path, "paper/Lecture.paper")
        XCTAssertEqual(notebook.kind, .notebook)
        XCTAssertEqual(notebook.template, .ruled)
        XCTAssertEqual(notebook.paperSize, .iPad)
        XCTAssertEqual(notebook.orientation, .portrait)
        XCTAssertEqual(notebook.paperColor, .white)
        XCTAssertNil(notebook.cover)

        let pages = try pages(notebook)
        XCTAssertEqual(pages.count, 1)
        XCTAssertEqual(pages[0].template, .ruled)
        XCTAssertEqual(pages[0].size, .iPad)
        XCTAssertNil(pages[0].customSize)
        XCTAssertEqual(pages[0].pointSize, CGSize(width: 1024, height: 1366))
        XCTAssertEqual(pages[0].paperColor, .white)

        let ops = try ops(notebook)
        XCTAssertEqual(ops.map(\.kind), [.notebookCreate, .pagePut])
        XCTAssertTrue(ops[1].createsPage, "the page goes up whole, with its colour")
        XCTAssertNil(ops[1].payload["after"])
    }

    func testCoverOnMakesPageOneACoverAndItIsTheThumbnail() throws {
        var options = NewDocumentOptions.notebookFactory
        options.cover = true
        options.coverColor = .forest
        options.paperColor = .cream
        options.template = .dotted
        let notebook = try store.createDocument(
            title: "Journal", folderPath: "paper", kind: .notebook, options: options)

        let pages = try pages(notebook)
        XCTAssertEqual(pages.count, 2)
        XCTAssertEqual(pages[0].template, .cover)
        XCTAssertTrue(pages[0].isCover)
        XCTAssertEqual(pages[0].paperColor, .forest)
        XCTAssertEqual(pages[0].size, .iPad, "the cover is the size of the paper")
        XCTAssertEqual(pages[1].template, .dotted)
        XCTAssertEqual(pages[1].paperColor, .cream)
        XCTAssertEqual(notebook.template, .dotted, "new pages are paper, not covers")
        XCTAssertEqual(notebook.paperColor, .cream)
        XCTAssertEqual(try store.coverPage(ofNotebook: notebook.id)?.id, pages[0].id)

        let ops = try ops(notebook)
        XCTAssertEqual(ops.map(\.kind), [.notebookCreate, .pagePut, .pagePut])
        XCTAssertEqual(ops[2].payload["after"], pages[0].id, "the paper follows the cover")
        let wire = try object(store.pagePartJSON(notebookPath: notebook.path, pageID: pages[0].id))
        XCTAssertEqual(wire["template"] as? String, "cover")
        XCTAssertEqual(wire["paper_color"] as? String, "forest")

        // Deleting page 1 removes the cover; the paper is the thumbnail now.
        _ = try store.deletePage(notebookID: notebook.id, pageID: pages[0].id)
        XCTAssertEqual(try store.coverPage(ofNotebook: notebook.id)?.id, pages[1].id)
    }

    func testCoverOffMakesOnlyThePaperPage() throws {
        var options = NewDocumentOptions.notebookFactory
        options.cover = false
        options.coverColor = .oxblood
        let notebook = try store.createDocument(
            title: "Plain", folderPath: "paper", kind: .notebook, options: options)
        XCTAssertEqual(try pages(notebook).map(\.template), [.ruled])
    }

    func testLandscapeIsThePresetWithItsSidesSwapped() throws {
        var options = NewDocumentOptions.notebookFactory
        options.size = .a4
        options.orientation = .landscape
        options.cover = true
        let notebook = try store.createDocument(
            title: "Wide", folderPath: "paper", kind: .notebook, options: options)
        XCTAssertEqual(notebook.orientation, .landscape)
        XCTAssertEqual(notebook.paperSize, .a4)

        for page in try pages(notebook) {
            XCTAssertEqual(page.size, .a4)
            XCTAssertEqual(page.customSize, SizePoints(width: 842, height: 595))
            XCTAssertEqual(page.orientation, .landscape)
        }
        // Adding a page follows the notebook's orientation.
        let added = try store.createPage(notebookID: notebook.id)
        XCTAssertEqual(added.customSize, SizePoints(width: 842, height: 595))
        XCTAssertEqual(added.template, .ruled)

        // Old clients and the viewer already prefer `size_points`.
        let wire = try object(store.pagePartJSON(notebookPath: notebook.path, pageID: added.id))
        XCTAssertEqual(wire["size_name"] as? String, "a4")
        XCTAssertEqual((wire["size_points"] as? [String: Any])?["width"] as? Double, 842)
        XCTAssertEqual((wire["size"] as? [String: Any])?["height"] as? Double, 595)
        XCTAssertNil(wire["board_extent"])

        // And it reads back as landscape after a pull, once the page's own
        // ops have gone up: a pull writes nothing over unpushed work.
        for op in try store.journal.queue() { try store.journal.complete(try XCTUnwrap(op.id)) }
        let pulled = try XCTUnwrap(
            store.applyRemotePage(
                rawPageJSON: JSONSerialization.data(withJSONObject: wire),
                notebookPath: notebook.path, pageID: added.id, drawing: nil, render: nil))
        XCTAssertEqual(pulled.size, .a4)
        XCTAssertEqual(pulled.orientation, .landscape)
    }

    func testAWhiteboardStartsWithOneBoard() throws {
        let board = try store.createDocument(
            title: "Accounting", folderPath: "paper", kind: .whiteboard, options: .whiteboardFactory)
        XCTAssertEqual(board.kind, .whiteboard)
        XCTAssertEqual(board.paperSize, .board)
        XCTAssertEqual(board.template, .grid)
        XCTAssertEqual(board.paperColor, .cream)

        let pages = try pages(board)
        XCTAssertEqual(pages.count, 1)
        XCTAssertEqual(pages[0].title, "Board 1")
        XCTAssertEqual(pages[0].size, .board)
        XCTAssertEqual(pages[0].template, .grid)
        XCTAssertEqual(pages[0].paperColor, .cream)
        XCTAssertEqual(pages[0].boardExtent, SizePoints(width: 16_384, height: 16_384))
        XCTAssertNil(pages[0].customSize)
        XCTAssertEqual(try store.coverPage(ofNotebook: board.id)?.id, pages[0].id, "board 1")

        let wire = try object(store.pagePartJSON(notebookPath: board.path, pageID: pages[0].id))
        XCTAssertEqual(wire["size_name"] as? String, "board")
        XCTAssertEqual((wire["board_extent"] as? [String: Any])?["width"] as? Double, 16_384)
        XCTAssertNil(wire["size_points"])
        XCTAssertEqual(wire["title"] as? String, "Board 1")

        // Add Board: another board, numbered on.
        let second = try store.createPage(notebookID: board.id)
        XCTAssertEqual(second.size, .board)
        XCTAssertEqual(second.title, "Board 2")
        XCTAssertEqual(second.boardExtent, SizePoints(width: 16_384, height: 16_384))
        XCTAssertTrue(try ops(board).last?.createsPage == true)

        // The kind is in the index too, so the library can tell them apart.
        let reopened = try PaperStore(root: root)
        XCTAssertEqual(try reopened.notebook(id: board.id).kind, .whiteboard)
        try reopened.reconcile()
        XCTAssertEqual(try reopened.notebook(id: board.id).kind, .whiteboard)
        XCTAssertEqual(try reopened.notebook(id: board.id).paperColor, .cream)
    }

    func testBoardNumbersSkipANameAlreadyTaken() throws {
        let board = try store.createDocument(
            title: "Plan", folderPath: "paper", kind: .whiteboard, options: .whiteboardFactory)
        let first = try XCTUnwrap(board.pages.first)
        _ = try store.setPageTitle(notebookID: board.id, pageID: first, title: "Board 2")
        XCTAssertEqual(try store.createPage(notebookID: board.id).title, "Board 3")
    }

    func testTheSheetsRulesHoldWhateverOptionsArrive() throws {
        var odd = NewDocumentOptions.notebookFactory
        odd.cover = true
        odd.orientation = .landscape
        odd.template = .ruled
        odd.size = .a4
        let board = try store.createDocument(
            title: "Odd", folderPath: "paper", kind: .whiteboard, options: odd)
        XCTAssertEqual(board.paperSize, .board)
        XCTAssertEqual(board.orientation, .portrait)
        XCTAssertEqual(board.template, .grid, "ruled is not a board background")
        XCTAssertEqual(try pages(board).map(\.template), [.grid], "no cover on a whiteboard")

        var boardSized = NewDocumentOptions.notebookFactory
        boardSized.size = .board
        boardSized.template = .cover
        let notebook = try store.createDocument(
            title: "Sized", folderPath: "paper", kind: .notebook, options: boardSized)
        XCTAssertEqual(notebook.paperSize, .iPad)
        XCTAssertEqual(notebook.template, .ruled)
    }

    func testTheFolderPathAndTheTitle() throws {
        let nested = try store.createDocument(
            title: "  Ideas ", folderPath: "paper/Work/Accounting", kind: .notebook,
            options: .notebookFactory)
        XCTAssertEqual(nested.path, "paper/Work/Accounting/Ideas.paper")
        XCTAssertEqual(nested.title, "Ideas")
        XCTAssertEqual(nested.folder, "Work/Accounting")
        XCTAssertEqual(nested.folderPath, "paper/Work/Accounting")

        // A folder named like a package keeps its slug's `_`.
        let odd = try store.createDocument(
            title: "Q1", folderPath: "paper/Notes.paper_", kind: .notebook,
            options: .notebookFactory)
        XCTAssertEqual(odd.path, "paper/Notes.paper_/Q1.paper")

        let blank = try store.createDocument(
            title: " ", folderPath: "paper/", kind: .notebook, options: .notebookFactory)
        XCTAssertEqual(blank.title, "Untitled")
        XCTAssertEqual(blank.path, "paper/Untitled.paper")
        XCTAssertEqual(blank.folderPath, "paper")

        let again = try store.createDocument(
            title: "Ideas", folderPath: "paper/Work/Accounting", kind: .whiteboard,
            options: .whiteboardFactory)
        XCTAssertEqual(again.path, "paper/Work/Accounting/Ideas 2.paper")

        for bad in ["Work", "", "paper/../x", "notes/Work", "paper/Work/../.."] {
            XCTAssertThrowsError(
                try store.createDocument(
                    title: "X", folderPath: bad, kind: .notebook, options: .notebookFactory), bad)
        }
    }

    func testDuplicatingAWhiteboardMakesAWhiteboard() throws {
        let board = try store.createDocument(
            title: "Map", folderPath: "paper", kind: .whiteboard, options: .whiteboardFactory)
        let copy = try store.duplicate(notebookID: board.id)
        XCTAssertEqual(copy.title, "Map copy")
        XCTAssertEqual(copy.kind, .whiteboard)
        XCTAssertEqual(try store.notebook(id: copy.id).kind, .whiteboard)
        XCTAssertEqual(try store.notebook(id: copy.id).paperColor, .cream)
        XCTAssertEqual(try pages(copy).first?.size, .board)
        XCTAssertEqual(try pages(copy).first?.title, "Board 1")
    }

    // MARK: - Covers

    func testTheCoverIsAlwaysTheFirstPage() throws {
        let notebook = try store.createNotebook(title: "Three", pageCount: 3)
        let ids = notebook.pages
        // The most recently edited page used to win, and so did a pinned one.
        var last = try store.page(notebookPath: notebook.path, id: ids[2])
        last.modified = Date(timeIntervalSinceNow: 100)
        try store.save(page: last, notebookPath: notebook.path)
        XCTAssertEqual(try store.coverPage(ofNotebook: notebook.id)?.id, ids[0])

        try store.reorderPages(notebookID: notebook.id, order: [ids[2], ids[0], ids[1]])
        XCTAssertEqual(try store.coverPage(ofNotebook: notebook.id)?.id, ids[2])

        let empty = try store.createNotebook(title: "Empty", pageCount: 0)
        XCTAssertNil(try store.coverPage(ofNotebook: empty.id))
    }

    // MARK: - Page colour and orientation

    func testNewPagesFollowTheNotebooksColourAndOrientation() throws {
        let notebook = try store.createNotebook(title: "Follow", size: .letter)
        let updated = try store.updateNotebookMetadata(
            notebookID: notebook.id, orientation: .landscape, paperColor: .dark)
        XCTAssertEqual(updated.orientation, .landscape)
        XCTAssertEqual(updated.paperColor, .dark)
        let patch = try XCTUnwrap(ops(notebook).last { $0.kind == .notebookPatch })
        XCTAssertEqual(patch.payload["orientation"], "landscape")
        XCTAssertEqual(patch.payload["paperColor"], "dark")

        let page = try store.createPage(notebookID: notebook.id)
        XCTAssertEqual(page.paperColor, .dark)
        XCTAssertEqual(page.customSize, SizePoints(width: 792, height: 612))
        XCTAssertTrue(PaperAppearance.isDark(page: page))
        let create = try XCTUnwrap(ops(notebook).last { $0.pageID == page.id })
        XCTAssertTrue(create.createsPage, "a page create cannot carry the colour")

        // The first page is as it was.
        let first = try store.page(notebookPath: notebook.path, id: notebook.pages[0])
        XCTAssertEqual(first.paperColor, .white)
        XCTAssertNil(first.customSize)
    }

    func testAPlainNewPageIsStillAPageCreate() throws {
        let notebook = try store.createNotebook(title: "Plain")
        let page = try store.createPage(notebookID: notebook.id, after: notebook.pages[0])
        let op = try XCTUnwrap(ops(notebook).last { $0.pageID == page.id })
        XCTAssertEqual(op.kind, .pageCreate)
        XCTAssertEqual(op.payload["after"], notebook.pages[0])
    }

    func testSetPageStyleChangesOnePageAndJournalsIt() throws {
        let notebook = try store.createNotebook(title: "Style", pageCount: 2)
        let id = notebook.pages[1]
        let styled = try store.setPageStyle(
            notebookID: notebook.id, pageID: id, template: .grid, paperColor: .cream)
        XCTAssertEqual(styled.template, .grid)
        XCTAssertEqual(styled.paperColor, .cream)
        XCTAssertEqual(try store.page(notebookPath: notebook.path, id: id).paperColor, .cream)
        XCTAssertEqual(
            try store.page(notebookPath: notebook.path, id: notebook.pages[0]).paperColor, .white)
        let put = try XCTUnwrap(ops(notebook).last)
        XCTAssertEqual(put.kind, .pagePut)
        XCTAssertEqual(put.pageID, id)
        let wire = try object(store.pagePartJSON(notebookPath: notebook.path, pageID: id))
        XCTAssertEqual(wire["paper_color"] as? String, "cream")

        let count = try store.journal.pendingCount()
        _ = try store.setPageStyle(notebookID: notebook.id, pageID: id, paperColor: .cream)
        XCTAssertEqual(try store.journal.pendingCount(), count, "no change, no op")
        XCTAssertThrowsError(
            try store.setPageStyle(notebookID: notebook.id, pageID: "nope", paperColor: .dark))
    }

    // MARK: - The wire

    func testAPulledBoardTakesItsExtentAndNeverSizePoints() throws {
        let board = try store.createDocument(
            title: "Pulled", folderPath: "paper", kind: .whiteboard, options: .whiteboardFactory)
        // As the server presents a board: `size` is the render rectangle.
        let wire: [String: Any] = [
            "id": "b2", "template": "dotted", "size_name": "board",
            "size": ["width": 2000, "height": 1500],
            "size_points": ["width": 2000, "height": 1500],
            "board_extent": ["width": 20_480, "height": 16_384],
            "render_rect": ["x": 7000, "y": 7400, "width": 2000, "height": 1500],
            "paper_color": "dark", "title": "Board 2", "revision": 3,
        ]
        let page = try XCTUnwrap(
            store.applyRemotePage(
                rawPageJSON: JSONSerialization.data(withJSONObject: wire),
                notebookPath: board.path, pageID: "b2", drawing: nil, render: nil))
        XCTAssertEqual(page.size, .board)
        XCTAssertNil(page.customSize)
        XCTAssertEqual(page.boardExtent, SizePoints(width: 20_480, height: 16_384))
        XCTAssertEqual(page.pointSize, CGSize(width: 20_480, height: 16_384))
        XCTAssertEqual(page.renderRect, PageObject.Frame(x: 7000, y: 7400, width: 2000, height: 1500))
        XCTAssertEqual(page.paperColor, .dark)
        XCTAssertEqual(page.template, .dotted)
        let onDisk = try object(Data(contentsOf: store.paths.pageJSON(board.path, "b2")))
        XCTAssertNil(onDisk["size_points"])
        XCTAssertNil(onDisk["size_name"])

        // An extent alone says board as well, when a server sent no name.
        var unnamed = wire
        unnamed.removeValue(forKey: "size_name")
        let again = try XCTUnwrap(
            store.applyRemotePage(
                rawPageJSON: JSONSerialization.data(withJSONObject: unnamed),
                notebookPath: board.path, pageID: "b3", drawing: nil, render: nil))
        XCTAssertEqual(again.size, .board)
        XCTAssertNil(again.customSize)

        // And going up again, the derived rectangle stays behind.
        let up = try object(store.pagePartJSON(notebookPath: board.path, pageID: "b2"))
        XCTAssertNil(up["render_rect"])
        XCTAssertNil(up["size_points"])
        XCTAssertEqual((up["board_extent"] as? [String: Any])?["width"] as? Double, 20_480)
        XCTAssertEqual(up["size_name"] as? String, "board")
    }

    func testAPulledPageKeepsItsColourAndAPageThatMeasuresLikeABoardIsNotOne() throws {
        let notebook = try store.createNotebook(title: "Pulled")
        let wire: [String: Any] = [
            "id": "p9", "template": "ruled", "size": ["width": 16_384, "height": 16_384],
            "paper_color": "cream", "revision": 1,
        ]
        let page = try XCTUnwrap(
            store.applyRemotePage(
                rawPageJSON: JSONSerialization.data(withJSONObject: wire),
                notebookPath: notebook.path, pageID: "p9", drawing: nil, render: nil))
        XCTAssertNotEqual(page.size, .board)
        XCTAssertEqual(page.pointSize, CGSize(width: 16_384, height: 16_384))
        XCTAssertEqual(page.paperColor, .cream)
    }

    // MARK: - New-document options

    func testOptionsStartAtTheFactoryValues() {
        let notebook = NewDocumentOptions.load(.notebook, defaults: scratchDefaults)
        XCTAssertEqual(notebook, .notebookFactory)
        XCTAssertEqual(notebook.template, .ruled)
        XCTAssertEqual(notebook.paperColor, .white)
        XCTAssertEqual(notebook.size, .iPad)
        XCTAssertEqual(notebook.orientation, .portrait)
        XCTAssertFalse(notebook.cover)
        XCTAssertEqual(notebook.coverColor, .navy)

        let board = NewDocumentOptions.load(.whiteboard, defaults: scratchDefaults)
        XCTAssertEqual(board, .whiteboardFactory)
        XCTAssertEqual(board.template, .grid)
        XCTAssertEqual(board.paperColor, .cream)
        XCTAssertEqual(board.size, .board)
        XCTAssertEqual(NewDocumentOptions.defaultsKey(.notebook), "paper.newNotebook.options")
        XCTAssertEqual(NewDocumentOptions.defaultsKey(.whiteboard), "paper.newWhiteboard.options")
    }

    func testCreateRemembersTheChoicesPerKind() throws {
        var chosen = NewDocumentOptions.notebookFactory
        chosen.template = .dotted
        chosen.paperColor = .dark
        chosen.size = .letter
        chosen.orientation = .landscape
        chosen.cover = true
        chosen.coverColor = .sage
        chosen.save(.notebook, defaults: scratchDefaults)
        XCTAssertEqual(NewDocumentOptions.load(.notebook, defaults: scratchDefaults), chosen)
        XCTAssertEqual(
            NewDocumentOptions.load(.whiteboard, defaults: scratchDefaults), .whiteboardFactory,
            "a notebook's choices are not a whiteboard's")
        // Stored as a JSON string, so a launch argument can set it too.
        let stored = try XCTUnwrap(scratchDefaults.string(forKey: "paper.newNotebook.options"))
        XCTAssertTrue(stored.contains(#""template":"dotted""#), stored)

        var board = NewDocumentOptions.whiteboardFactory
        board.template = .blank
        board.paperColor = .dark
        board.cover = true
        board.orientation = .landscape
        board.save(.whiteboard, defaults: scratchDefaults)
        let loaded = NewDocumentOptions.load(.whiteboard, defaults: scratchDefaults)
        XCTAssertEqual(loaded.template, .blank)
        XCTAssertEqual(loaded.paperColor, .dark)
        XCTAssertEqual(loaded.size, .board, "fixed")
        XCTAssertFalse(loaded.cover)
        XCTAssertEqual(loaded.orientation, .portrait)
        XCTAssertEqual(NewDocumentOptions.load(.notebook, defaults: scratchDefaults), chosen)
    }

    func testAStoredChoiceTheSheetNoLongerOffersFallsBackFieldByField() {
        let key = NewDocumentOptions.defaultsKey(.notebook)
        scratchDefaults.set(
            #"{"template":"cover","paperColor":"navy","size":"infinite","orientation":"landscape","cover":"yes","coverColor":"white"}"#,
            forKey: key)
        let loaded = NewDocumentOptions.load(.notebook, defaults: scratchDefaults)
        XCTAssertEqual(loaded.template, .ruled, "a cover is not paper")
        XCTAssertEqual(loaded.paperColor, .white, "navy is a cover colour")
        XCTAssertEqual(loaded.size, .iPad, "infinite is no longer offered")
        XCTAssertEqual(loaded.orientation, .landscape, "the good field survives")
        XCTAssertFalse(loaded.cover, "a malformed field is the factory one")
        XCTAssertEqual(loaded.coverColor, .navy, "white is not a cover colour")

        scratchDefaults.set(#"{"template":"hexagonal","size":"a4"}"#, forKey: key)
        let partial = NewDocumentOptions.load(.notebook, defaults: scratchDefaults)
        XCTAssertEqual(partial.template, .ruled)
        XCTAssertEqual(partial.size, .a4)

        scratchDefaults.set("not json", forKey: key)
        XCTAssertEqual(NewDocumentOptions.load(.notebook, defaults: scratchDefaults), .notebookFactory)
        scratchDefaults.set(Data(#"{"size":"letter"}"#.utf8), forKey: key)
        XCTAssertEqual(NewDocumentOptions.load(.notebook, defaults: scratchDefaults).size, .letter)
    }

    // MARK: - The index

    func testV4AddsTheDocumentColumnsToAnExistingDatabase() throws {
        let queue = try DatabaseQueue()
        try PaperDatabase.migrator.migrate(queue, upTo: "v3-ops-notebook-id")
        try queue.write { db in
            try db.execute(
                sql: """
                    INSERT INTO notebooks (id, path, title, template, paper_size, created, modified)
                    VALUES ('n1', 'paper/Old.paper', 'Old', 'ruled', 'a4', 0, 0)
                    """)
        }
        try PaperDatabase.migrator.migrate(queue)
        let row = try XCTUnwrap(
            queue.read { db in
                try Row.fetchOne(db, sql: "SELECT kind, orientation, paper_color FROM notebooks")
            })
        XCTAssertEqual(row["kind"] as String?, "notebook")
        XCTAssertEqual(row["orientation"] as String?, "portrait")
        XCTAssertEqual(row["paper_color"] as String?, "white")
    }

    func testOpenedDatesAreWhatRecentsReads() throws {
        let opened = try store.createNotebook(title: "Opened")
        let never = try store.createNotebook(title: "Never")
        try store.markOpened(notebookID: opened.id)
        let dates = try store.openedDates()
        XCTAssertNotNil(dates[opened.id])
        XCTAssertNil(dates[never.id])
        let recents = try LibraryArrangement.recents(
            store.notebooks(), opened: dates, filter: .all)
        XCTAssertEqual(recents.map(\.id), ["document/\(opened.id)"])
    }
}
