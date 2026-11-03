import PaperCore
import PencilKit
import XCTest

@testable import PaperUI

/// The window's tabs: opening, switching, closing, Home, keys, what is kept
/// across launches, Read Only per tab and Quick Notes.
@MainActor
final class DocumentTabsModelTests: XCTestCase {
    private func makeModel(notebooks titles: [String] = ["One", "Two", "Three"]) throws
        -> (DocumentTabsModel, [Notebook])
    {
        let root = FileManager.default.temporaryDirectory
            .appending(path: "DocumentTabsModelTests-\(UUID().uuidString)", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }
        let store = try PaperStore(root: root)
        let controller = PaperController(store: store)
        let notebooks = try titles.map { try store.createNotebook(title: $0) }
        controller.refresh()
        return (DocumentTabsModel(controller: controller), notebooks)
    }

    func testOpeningAddsATabOnceAndShowsIt() throws {
        let (tabs, books) = try makeModel()
        XCTAssertTrue(tabs.isHome)
        tabs.open(books[0])
        tabs.open(books[1])
        tabs.open(books[0])
        XCTAssertEqual(tabs.ids, [books[0].id, books[1].id])
        XCTAssertEqual(tabs.activeID, books[0].id)
        XCTAssertEqual(tabs.session?.notebook.id, books[0].id)
        XCTAssertEqual(tabs.title(of: books[1].id), "Two")
    }

    func testSwitchingBuildsANewSessionOnTheWindowsTools() throws {
        let (tabs, books) = try makeModel()
        tabs.open(books[0])
        let first = try XCTUnwrap(tabs.session)
        tabs.open(books[1])
        let second = try XCTUnwrap(tabs.session)
        XCTAssertFalse(first === second)
        XCTAssertTrue(first.tools === second.tools)
        XCTAssertTrue(second.tools === tabs.controller.tools)
    }

    func testClosingTheActiveTabShowsTheOneThatTookItsPlace() throws {
        let (tabs, books) = try makeModel()
        for book in books { tabs.open(book) }
        tabs.activate(books[1].id)
        tabs.close(books[1].id)
        XCTAssertEqual(tabs.ids, [books[0].id, books[2].id])
        XCTAssertEqual(tabs.activeID, books[2].id)
        tabs.close(books[2].id)
        XCTAssertEqual(tabs.activeID, books[0].id)
        tabs.close(books[0].id)
        XCTAssertTrue(tabs.isHome)
        XCTAssertNil(tabs.session)
        XCTAssertTrue(tabs.ids.isEmpty)
    }

    func testClosingAnotherTabLeavesTheActiveOneAlone() throws {
        let (tabs, books) = try makeModel()
        for book in books { tabs.open(book) }
        let session = tabs.session
        tabs.close(books[0].id)
        XCTAssertEqual(tabs.activeID, books[2].id)
        XCTAssertTrue(tabs.session === session)
    }

    func testHomeKeepsTheTabs() throws {
        let (tabs, books) = try makeModel()
        tabs.open(books[0])
        tabs.open(books[1])
        tabs.goHome()
        XCTAssertTrue(tabs.isHome)
        XCTAssertNil(tabs.session)
        XCTAssertEqual(tabs.ids.count, 2)
        tabs.activate(books[1].id)
        XCTAssertEqual(tabs.session?.notebook.id, books[1].id)
    }

    func testCloseOthersAndCloseAll() throws {
        let (tabs, books) = try makeModel()
        for book in books { tabs.open(book) }
        tabs.closeOthers(keeping: books[1].id)
        XCTAssertEqual(tabs.ids, [books[1].id])
        XCTAssertEqual(tabs.activeID, books[1].id)
        tabs.open(books[0])
        tabs.closeAll()
        XCTAssertTrue(tabs.ids.isEmpty)
        XCTAssertTrue(tabs.isHome)
    }

    func testNumberKeysAndCyclingWrap() throws {
        let (tabs, books) = try makeModel()
        for book in books { tabs.open(book) }
        tabs.select(number: 1)
        XCTAssertEqual(tabs.activeID, books[0].id)
        tabs.select(number: 9)
        XCTAssertEqual(tabs.activeID, books[0].id, "there is no ninth tab")
        tabs.cycle(by: -1)
        XCTAssertEqual(tabs.activeID, books[2].id)
        tabs.cycle(by: 1)
        XCTAssertEqual(tabs.activeID, books[0].id)
        tabs.goHome()
        tabs.cycle(by: 1)
        XCTAssertEqual(tabs.activeID, books[0].id)
    }

    /// Cmd-2 shows the second tab, Cmd-W closes the one showing and
    /// Option-Cmd-W closes them all, through what the window's key buttons
    /// call. This replaces a UI test that depended on the simulator passing
    /// hardware keys to SwiftUI's keyboard shortcuts, which it does only now
    /// and then; the keys themselves are checked on the iPad (RELEASE_GATE.md
    /// item 18).
    func testTheTabKeys() throws {
        let (tabs, books) = try makeModel()
        for book in books { tabs.open(book) }
        tabs.activate(books[0].id)
        tabs.select(number: 2)
        XCTAssertEqual(tabs.activeID, books[1].id, "Cmd-2 did not show the second tab")
        tabs.closeActive()
        XCTAssertEqual(tabs.ids, [books[0].id, books[2].id], "Cmd-W did not close the tab")
        XCTAssertEqual(tabs.activeID, books[2].id, "the tab that took its place is not showing")
        tabs.closeAll()
        XCTAssertTrue(tabs.isHome, "Option-Cmd-W did not go Home")
        XCTAssertTrue(tabs.ids.isEmpty)
        tabs.closeActive()
        XCTAssertTrue(tabs.isHome, "Cmd-W at Home did something")
    }

    func testCommandOGoesHomeAndAsksForSearch() throws {
        let (tabs, books) = try makeModel()
        tabs.open(books[0])
        let before = tabs.librarySearchRequest
        tabs.goHomeToSearch()
        XCTAssertTrue(tabs.isHome)
        XCTAssertEqual(tabs.librarySearchRequest, before + 1)
    }

    func testTheTabListSurvivesARelaunchWithoutDocumentsThatWent() throws {
        let (tabs, books) = try makeModel()
        for book in books { tabs.open(book) }
        tabs.activate(books[1].id)
        tabs.isSidebarVisible = true
        tabs.sidebarTab = .outline
        let json = tabs.storedJSON
        _ = tabs.controller.delete(books[2])

        let relaunched = DocumentTabsModel(controller: tabs.controller)
        relaunched.restore(json: json)
        XCTAssertEqual(relaunched.ids, [books[0].id, books[1].id])
        XCTAssertEqual(relaunched.activeID, books[1].id)
        XCTAssertEqual(relaunched.session?.notebook.id, books[1].id)
        XCTAssertTrue(relaunched.isSidebarVisible)
        XCTAssertEqual(relaunched.sidebarTab, .outline)

        let decoded = try JSONDecoder().decode(
            DocumentTabsModel.Stored.self, from: Data(json.utf8))
        XCTAssertEqual(decoded.tabs, books.map(\.id))
        XCTAssertEqual(decoded.active, books[1].id)
        XCTAssertEqual(decoded.sidebarTab, "outline")

        let empty = DocumentTabsModel(controller: tabs.controller)
        empty.restore(json: "not json")
        XCTAssertTrue(empty.ids.isEmpty)
        XCTAssertTrue(empty.isHome)
    }

    func testADocumentDeletedElsewhereLosesItsTab() throws {
        let (tabs, books) = try makeModel()
        tabs.open(books[0])
        tabs.open(books[1])
        _ = tabs.controller.delete(books[1])
        tabs.closeMissing()
        XCTAssertEqual(tabs.ids, [books[0].id])
        XCTAssertEqual(tabs.activeID, books[0].id)
    }

    func testTrashingFromTheTabClosesItAndUndoBringsItBack() throws {
        let (tabs, books) = try makeModel()
        tabs.open(books[0])
        tabs.open(books[1])
        tabs.trash(books[1].id)
        XCTAssertEqual(tabs.ids, [books[0].id])
        XCTAssertNil(try? tabs.controller.store.notebook(id: books[1].id))
        XCTAssertNotNil(tabs.pendingUndo)
        tabs.undoTrash()
        XCTAssertEqual(tabs.activeID, books[1].id)
        XCTAssertNotNil(try? tabs.controller.store.notebook(id: books[1].id))
    }

    func testAWhiteboardShowsBoardsInTheSidebar() throws {
        let (tabs, books) = try makeModel()
        let board = try tabs.controller.store.createDocument(
            title: "Board", folderPath: "paper", kind: .whiteboard, options: .whiteboardFactory)
        tabs.sidebarTab = .outline
        tabs.open(books[0])
        XCTAssertEqual(tabs.sidebarTab, .outline)
        tabs.open(board)
        XCTAssertEqual(tabs.sidebarTab, .boards)
        XCTAssertEqual(tabs.kind(of: board.id), .whiteboard)
        tabs.activate(books[0].id)
        XCTAssertEqual(tabs.sidebarTab, .pages)
    }

    func testReadOnlyBelongsToOneTab() throws {
        let (tabs, books) = try makeModel()
        tabs.open(books[0])
        tabs.setReadOnly(true, for: books[0].id)
        XCTAssertTrue(tabs.session?.chrome.isReadOnly == true)
        tabs.open(books[1])
        XCTAssertFalse(tabs.session?.chrome.isReadOnly == true)
        tabs.activate(books[0].id)
        XCTAssertTrue(tabs.session?.chrome.isReadOnly == true, "the flag comes back with the tab")
        tabs.close(books[0].id)
        tabs.open(books[0])
        XCTAssertFalse(tabs.session?.chrome.isReadOnly == true, "closing forgets it")
    }

    func testAnUntouchedQuickNoteGoesWithItsTab() throws {
        let (tabs, _) = try makeModel(notebooks: [])
        let store = tabs.controller.store
        let quick = try store.createNotebook(title: "Quick Note")
        tabs.open(quick, isQuickNote: true)
        tabs.close(quick.id)
        XCTAssertNil(try? store.notebook(id: quick.id))

        let written = try store.createNotebook(title: "Quick Note")
        tabs.open(written, isQuickNote: true)
        let page = try XCTUnwrap(written.pages.first)
        try store.writeDrawing(
            PKDrawing(strokes: [NotebookViewControllerRebuildTests.stroke(y: 200)])
                .dataRepresentation(),
            notebookPath: written.path, pageID: page)
        tabs.goHome()
        XCTAssertNotNil(try? store.notebook(id: written.id), "a written Quick Note stays")
        XCTAssertEqual(tabs.ids, [written.id])

        let titled = try store.createNotebook(title: "Quick Note")
        tabs.open(titled, isQuickNote: true)
        tabs.controller.rename(titled, to: "Groceries")
        tabs.goHome()
        XCTAssertNotNil(try? store.notebook(id: titled.id), "a renamed Quick Note stays")
    }
}

/// The canvas side of the chrome: the reading position per document, Read
/// Only, page style, Clear Page off screen and the picture OMNIA gets.
@MainActor
final class EditorCommandsTests: XCTestCase {
    private struct Fixture {
        let store: PaperStore
        let controller: PaperController
        let notebook: Notebook
        let session: NotebookSession
        let notebookController: NotebookViewController
        let window: UIWindow
    }

    private func makeFixture(pages: Int = 5, notebook existing: Notebook? = nil, in shared: Fixture? = nil)
        throws -> Fixture
    {
        let store: PaperStore
        let controller: PaperController
        if let shared {
            store = shared.store
            controller = shared.controller
        } else {
            let root = FileManager.default.temporaryDirectory
                .appending(path: "EditorCommandsTests-\(UUID().uuidString)", directoryHint: .isDirectory)
            try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
            addTeardownBlock { try? FileManager.default.removeItem(at: root) }
            store = try PaperStore(root: root)
            controller = PaperController(store: store)
        }
        let notebook = try existing ?? store.createNotebook(title: "Chrome", size: .a4, pageCount: pages)
        let session = NotebookSession(controller: controller, notebook: notebook)
        let notebookController = NotebookViewController(session: session)
        let window = UIWindow(frame: CGRect(x: 0, y: 0, width: 1024, height: 1366))
        window.rootViewController = notebookController
        window.makeKeyAndVisible()
        addTeardownBlock { @MainActor in
            window.isHidden = true
            window.rootViewController = nil
        }
        notebookController.loadViewIfNeeded()
        notebookController.view.frame = window.bounds
        notebookController.view.layoutIfNeeded()
        notebookController.beginAppearanceTransition(true, animated: false)
        notebookController.endAppearanceTransition()
        return Fixture(
            store: store, controller: controller, notebook: notebook, session: session,
            notebookController: notebookController, window: window)
    }

    /// Takes the window down the way closing a tab does, writing the position.
    private func close(_ f: Fixture) {
        f.notebookController.beginAppearanceTransition(false, animated: false)
        f.notebookController.endAppearanceTransition()
        f.window.isHidden = true
        f.window.rootViewController = nil
    }

    func testTheReadingPositionComesBackOnBothAxesOnALaterPage() throws {
        let f = try makeFixture()
        let vc = f.notebookController
        vc.setZoom(2)
        vc.view.layoutIfNeeded()
        let frame = vc.stack.frame(ofPageAt: 3)
        // Inside the content: an A4 page at 2x is wider than the window.
        let spot = CGPoint(x: frame.minX + 100, y: frame.minY + 420)
        XCTAssertLessThan(spot.x, vc.scrollView.contentSize.width - vc.scrollView.bounds.width)
        vc.scrollView.contentOffset = spot
        vc.scrollView.delegate?.scrollViewDidScroll?(vc.scrollView)
        XCTAssertEqual(f.session.currentIndex, 3)
        close(f)
        XCTAssertEqual(
            f.store.viewState(notebookID: f.notebook.id).offset, CGPoint(x: 50, y: 210),
            "kept in page points from the page's corner")

        let reopened = try makeFixture(notebook: f.notebook, in: f)
        let again = reopened.notebookController
        XCTAssertEqual(again.zoom, 2, accuracy: 0.001)
        XCTAssertEqual(reopened.session.currentIndex, 3)
        XCTAssertEqual(again.scrollView.contentOffset.x, spot.x, accuracy: 1)
        XCTAssertEqual(again.scrollView.contentOffset.y, spot.y, accuracy: 1)
    }

    func testAnOffsetFromBeforePagesKeptTheirOwnOpensAtThePagesTop() throws {
        let f = try makeFixture()
        let pageID = try XCTUnwrap(f.session.pages.last?.id)
        close(f)
        // What the old build stored: the scroll view's own offset.
        try f.store.setViewState(
            NotebookViewState(lastPageID: pageID, zoom: 1, offset: CGPoint(x: 0, y: 3600)),
            notebookID: f.notebook.id)
        let reopened = try makeFixture(notebook: f.notebook, in: f)
        let vc = reopened.notebookController
        let frame = vc.stack.frame(ofPageAt: 4)
        let ceiling = max(0, vc.scrollView.contentSize.height - vc.scrollView.bounds.height)
        XCTAssertEqual(
            vc.scrollView.contentOffset.y,
            min(frame.minY - PageStackView.gutter, ceiling), accuracy: 1)
    }

    func testReadOnlyTakesTheInkTheLassoAndThePagesTouchesAway() throws {
        let f = try makeFixture()
        let vc = f.notebookController
        let page = try XCTUnwrap(vc.pageViews.first)
        let canvas = try XCTUnwrap(page.canvas)
        f.session.tools.select(.lasso)
        XCTAssertTrue(vc.lassoGesture?.isEnabled == true)

        vc.setReadOnly(true)
        XCTAssertTrue(f.session.chrome.isReadOnly)
        XCTAssertFalse(canvas.drawingGestureRecognizer.isEnabled)
        XCTAssertFalse(page.isUserInteractionEnabled)
        XCTAssertFalse(vc.lassoGesture?.isEnabled == true)
        XCTAssertFalse(vc.objectTap.isEnabled)
        XCTAssertFalse(vc.editorGestures?.undoTap.isEnabled == true)
        // A tool change from another window does not bring any of it back.
        f.session.tools.select(.text)
        XCTAssertFalse(page.insertTap.isEnabled)
        f.session.tools.select(.pen)
        XCTAssertFalse(canvas.drawingGestureRecognizer.isEnabled)
        XCTAssertFalse(vc.keyCommands?.contains { $0.input == "z" } == true)

        vc.setReadOnly(false)
        XCTAssertTrue(canvas.drawingGestureRecognizer.isEnabled)
        XCTAssertTrue(page.isUserInteractionEnabled)
        XCTAssertTrue(vc.objectTap.isEnabled)
        XCTAssertTrue(vc.editorGestures?.undoTap.isEnabled == true)
    }

    func testReadOnlyRefusesPageEdits() throws {
        let f = try makeFixture(pages: 2)
        let vc = f.notebookController
        vc.setReadOnly(true)
        vc.addPage(.last, template: nil)
        vc.setPageStyle(template: .grid, paperColor: nil, at: 0)
        XCTAssertEqual(f.session.pages.count, 2)
        XCTAssertEqual(f.session.pages[0].template, .ruled)
    }

    func testPageStyleChangesTheColourAndTheTemplateOfOnePage() throws {
        let f = try makeFixture(pages: 2)
        f.notebookController.setPageStyle(template: nil, paperColor: .cream, at: 1)
        f.notebookController.setPageStyle(template: .dotted, paperColor: nil, at: 1)
        let page = try f.store.page(notebookPath: f.notebook.path, id: f.session.pages[1].id)
        XCTAssertEqual(page.paperColor, .cream)
        XCTAssertEqual(page.template, .dotted)
        XCTAssertEqual(f.session.pages[0].paperColor, .white)
        XCTAssertEqual(f.notebookController.pageViews[1].page.paperColor, .cream)
    }

    func testClearPageOnAPageWithNoCanvasClearsItOnDisk() throws {
        let f = try makeFixture(pages: 5)
        let pageID = f.session.pages[4].id
        try f.store.writeDrawing(
            PKDrawing(strokes: [NotebookViewControllerRebuildTests.stroke(y: 300)])
                .dataRepresentation(),
            notebookPath: f.notebook.path, pageID: pageID)
        XCTAssertFalse(f.notebookController.pageViews[4].isLive)
        f.notebookController.clearPage(at: 4)
        f.controller.autosave.flushAll()
        let data = try XCTUnwrap(f.store.drawingData(notebookPath: f.notebook.path, pageID: pageID))
        XCTAssertTrue(try PKDrawing(data: data).strokes.isEmpty)
    }

    func testOMNIAGetsTheVisiblePartOfThePageShowing() throws {
        let f = try makeFixture(pages: 2)
        let region = try XCTUnwrap(f.notebookController.visiblePageRegion())
        XCTAssertTrue(region.page === f.notebookController.pageViews[0])
        XCTAssertNotNil(UIImage(data: region.image))
        XCTAssertEqual(region.rect.minX, 0, accuracy: 0.5)
        XCTAssertEqual(region.rect.minY, 0, accuracy: 0.5)
        XCTAssertGreaterThan(region.rect.height, 100)
        XCTAssertLessThanOrEqual(region.rect.width, PaperSize.a4.points.width + 0.5)
    }

    func testExportingThisPageIsAllowedInReadOnly() throws {
        let f = try makeFixture(pages: 2)
        f.notebookController.setReadOnly(true)
        f.notebookController.export(.currentPage, from: nil)
        XCTAssertTrue(f.notebookController.isExporting)
    }
}
