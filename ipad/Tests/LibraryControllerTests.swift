import PDFKit
import PaperCore
import UIKit
import XCTest

@testable import PaperUI

/// The library's logic in `PaperController+Library.swift` and the state the
/// library keeps: folders shown before the launch reconcile, listings, moves
/// and their limits, search groups, the move picker's tree, Quick Notes,
/// export and the rest.
@MainActor
final class LibraryControllerTests: XCTestCase {
    private func makeController() throws -> PaperController {
        let root = FileManager.default.temporaryDirectory
            .appending(path: "LibraryControllerTests-\(UUID().uuidString)", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }
        return PaperController(store: try PaperStore(root: root))
    }

    private func notebook(_ path: String, modified: Date = Date(), kind: DocumentKind = .notebook)
        -> Notebook
    {
        let title = String((path.split(separator: "/").last ?? "").dropLast(".paper".count))
        return Notebook(title: title, modified: modified, kind: kind, path: path)
    }

    private func folder(_ path: String, name: String? = nil, modified: Date = Date()) -> PaperFolder {
        PaperFolder(
            name: name ?? String(path.split(separator: "/").last ?? ""), modified: modified, path: path)
    }

    // MARK: Folders before and after the reconcile

    func testDirectoriesWithoutARecordShowAsTheLegacyFolderTheReconcileWillWrite() {
        let older = Date(timeIntervalSince1970: 1_000)
        let newer = Date(timeIntervalSince1970: 2_000)
        let notebooks = [
            notebook("paper/Work/Accounting/Ledger.paper", modified: older),
            notebook("paper/Work/Accounting/Taxes.paper", modified: newer),
            notebook("paper/Top.paper"),
        ]
        let folders = PaperController.libraryFolders(records: [], notebooks: notebooks)
        XCTAssertEqual(folders.map(\.path), ["paper/Work", "paper/Work/Accounting"])
        XCTAssertEqual(folders.map(\.id), [
            PaperFolderID.legacy(path: "paper/Work"), PaperFolderID.legacy(path: "paper/Work/Accounting"),
        ])
        XCTAssertEqual(folders.map(\.name), ["Work", "Accounting"])
        // Dated by the newest thing inside, not by the moment it was drawn.
        XCTAssertEqual(folders.last?.modified, newer)
    }

    func testARecordIsNeverShownTwice() {
        let record = PaperFolder(id: PaperFolderID.make(), name: "Work", color: .red, path: "paper/Work")
        let folders = PaperController.libraryFolders(
            records: [record, folder("paper/Work/Deep/Deeper")],
            notebooks: [notebook("paper/Work/Accounting/Ledger.paper")])
        XCTAssertEqual(
            folders.map(\.path),
            ["paper/Work", "paper/Work/Accounting", "paper/Work/Deep", "paper/Work/Deep/Deeper"])
        XCTAssertEqual(folders.first?.id, record.id)
        XCTAssertEqual(folders.first?.color, .red)
    }

    /// The owner's notebooks sit in nested folders with no `folder.json`
    /// until the first reconcile after the update. The library shows the
    /// same folders, with the same ids, before and after it.
    func testTheReconcileChangesNothingTheLibraryShows() throws {
        let controller = try makeController()
        let store = controller.store
        // Notebooks a pull from a server without folder records wrote: their
        // directories, and no records until a reconcile.
        for (path, title) in [
            ("paper/Work/Accounting/Ledger.paper", "Ledger"), ("paper/Work/Plan.paper", "Plan"),
            ("paper/Loose.paper", "Loose"),
        ] {
            let json = #"{"path":"\#(path)","id":"\#(UUID().uuidString)","title":"\#(title)","pages":[]}"#
            try store.applyRemoteNotebook(JSONDecoder().decode(NotebookManifest.self, from: Data(json.utf8)))
            store.reindex(notebookPath: path)
        }
        controller.refresh()
        XCTAssertTrue(controller.folderRecords.isEmpty)
        let before = controller.libraryFolders
        XCTAssertEqual(before.map(\.path), ["paper/Work", "paper/Work/Accounting"])
        let rootBefore = controller.libraryItems(inFolder: "paper").map(\.id).sorted()

        _ = try store.reconcile()
        controller.refresh()
        XCTAssertEqual(controller.folderRecords.map(\.path), before.map(\.path))
        XCTAssertEqual(controller.libraryFolders.map(\.id), before.map(\.id))
        XCTAssertEqual(controller.libraryItems(inFolder: "paper").map(\.id).sorted(), rootBefore)
        XCTAssertEqual(controller.libraryItems(inFolder: "paper/Work").count, 2)
        XCTAssertEqual(controller.libraryItems(inFolder: "paper/Work/Accounting").count, 1)
    }

    func testAFolderListsWhatIsDirectlyInsideIt() {
        let folders = [folder("paper/Work"), folder("paper/Work/Accounting"), folder("paper/Home")]
        let notebooks = [
            notebook("paper/Top.paper"), notebook("paper/Work/Plan.paper"),
            notebook("paper/Work/Board.paper", kind: .whiteboard),
            notebook("paper/Work/Accounting/Ledger.paper"),
        ]
        let root = PaperController.items(inFolder: "paper", folders: folders, notebooks: notebooks)
        XCTAssertEqual(Set(root.map(\.name)), ["Work", "Home", "Top"])
        let work = PaperController.counts(inFolder: "paper/Work", folders: folders, notebooks: notebooks)
        XCTAssertEqual(work, FolderCounts(folders: 1, notebooks: 1, whiteboards: 1))
        XCTAssertEqual(
            PaperController.folderChain(to: "paper/Work/Accounting", in: folders).map(\.name),
            ["Work", "Accounting"])
        XCTAssertEqual(PaperController.unprefixed("paper/Work/Accounting"), "Work/Accounting")
        XCTAssertEqual(PaperController.unprefixed("paper"), "")
    }

    // MARK: Writes

    func testAFolderFromCreationToTheTrashAndBack() throws {
        let controller = try makeController()
        let made = try XCTUnwrap(controller.createFolder(name: "Taxes", in: "paper", color: .orange))
        XCTAssertEqual(controller.libraryItems(inFolder: "paper").map(\.name), ["Taxes"])

        controller.rename(.folder(made), to: "Taxes 2026")
        let renamed = try XCTUnwrap(controller.libraryFolder(id: made.id))
        XCTAssertEqual(renamed.name, "Taxes 2026")

        let notebook = try XCTUnwrap(
            controller.createDocument(
                title: "Receipts", folderPath: "paper", kind: .notebook,
                options: .notebookFactory))
        XCTAssertTrue(controller.move([.document(notebook)], toFolderPath: renamed.path))
        XCTAssertEqual(controller.notebooks.first?.folderPath, renamed.path)
        XCTAssertEqual(controller.libraryItems(inFolder: "paper").count, 1)

        controller.setFavorite(.folder(renamed), true)
        XCTAssertEqual(controller.libraryFolder(id: made.id)?.favorite, true)
        XCTAssertEqual(
            controller.favoriteItems(sort: .date, filter: .all).map(\.id), ["folder/\(made.id)"])

        controller.setFolderColor(renamed, .red)
        controller.setFolderIcon(renamed, .emoji("📒"))
        XCTAssertEqual(controller.libraryFolder(id: made.id)?.color, .red)
        XCTAssertEqual(controller.libraryFolder(id: made.id)?.icon, .emoji("📒"))
        controller.setFolderIcon(renamed, nil)
        XCTAssertNil(controller.libraryFolder(id: made.id)?.icon)

        let current = try XCTUnwrap(controller.libraryFolder(id: made.id))
        let token = try XCTUnwrap(controller.trash(.folder(current)))
        XCTAssertNil(controller.libraryFolder(id: made.id))
        XCTAssertTrue(controller.notebooks.isEmpty, "the notebook went with its folder")

        controller.restore([token])
        XCTAssertEqual(controller.libraryFolder(id: made.id)?.name, "Taxes 2026")
        XCTAssertEqual(controller.notebooks.map(\.title), ["Receipts"])
    }

    func testAFolderNeverGoesIntoItselfOrPastThePathLimit() {
        let work = folder("paper/Work")
        let items: [PaperCore.LibraryItem] = [.folder(work)]
        let notebooks = [notebook("paper/Work/Accounting/Ledger.paper")]
        XCTAssertFalse(PaperController.canMove(items, into: "paper/Work", notebooks: notebooks))
        XCTAssertFalse(PaperController.canMove(items, into: "paper/Work/Accounting", notebooks: notebooks))
        XCTAssertTrue(PaperController.canMove(items, into: "paper/Workshop", notebooks: notebooks))
        XCTAssertTrue(PaperController.canMove(items, into: "paper", notebooks: notebooks))
        let deep = "paper/" + String(repeating: "x", count: 890)
        XCTAssertFalse(PaperController.canMove(items, into: deep, notebooks: notebooks))
        XCTAssertFalse(PaperController.canMove([], into: "paper", notebooks: []))
        XCTAssertTrue(PaperController.isAlreadyIn(items, "paper"))
        XCTAssertFalse(PaperController.isAlreadyIn(items, "paper/Home"))
    }

    func testAFolderMovesWithEverythingInIt() throws {
        let controller = try makeController()
        let work = try XCTUnwrap(controller.createFolder(name: "Work", in: "paper"))
        let archive = try XCTUnwrap(controller.createFolder(name: "Archive", in: "paper"))
        _ = controller.createDocument(
            title: "Plan", folderPath: work.path, kind: .whiteboard, options: .whiteboardFactory)
        XCTAssertFalse(controller.move([.folder(work)], toFolderPath: work.path))
        XCTAssertTrue(controller.move([.folder(work)], toFolderPath: archive.path))
        XCTAssertEqual(controller.libraryFolder(id: work.id)?.path, "paper/Archive/Work")
        XCTAssertEqual(controller.notebooks.first?.folderPath, "paper/Archive/Work")
    }

    func testAQuickNoteLandsInTheOpenFolderWithTheNotebookDefaults() throws {
        let controller = try makeController()
        let defaults = try scratchDefaults("LibraryControllerTests")
        var options = NewDocumentOptions.notebookFactory
        options.cover = true
        options.template = .grid
        options.save(.notebook, defaults: defaults)
        let work = try XCTUnwrap(controller.createFolder(name: "Work", in: "paper"))

        let note = try XCTUnwrap(controller.createQuickNote(in: work.path, defaults: defaults))
        XCTAssertEqual(note.title, "Quick Note")
        XCTAssertEqual(note.folderPath, work.path)
        XCTAssertEqual(note.kind, .notebook)
        XCTAssertEqual(note.pages.count, 2, "a cover, then the paper")
        XCTAssertEqual(try controller.store.coverPage(ofNotebook: note.id)?.template, .cover)
    }

    func testDuplicateAndTrashOfADocument() throws {
        let controller = try makeController()
        let original = try XCTUnwrap(
            controller.createDocument(
                title: "Ideas", folderPath: "paper", kind: .notebook, options: .notebookFactory))
        let copy = try XCTUnwrap(controller.duplicate(.document(original)))
        XCTAssertEqual(copy.title, "Ideas copy")
        XCTAssertNil(controller.duplicate(.folder(folder("paper/Work"))))
        let token = try XCTUnwrap(controller.trash(.document(copy)))
        XCTAssertEqual(controller.notebooks.map(\.id), [original.id])
        controller.restore([token])
        XCTAssertEqual(controller.notebooks.count, 2)

        controller.rename(.document(original), to: "  ")
        XCTAssertEqual(controller.notebooks.first { $0.id == original.id }?.title, "Ideas")
        controller.rename(.document(original), to: "Better Ideas")
        XCTAssertEqual(controller.notebooks.first { $0.id == original.id }?.title, "Better Ideas")
        controller.setTags(try XCTUnwrap(controller.notebooks.first { $0.id == original.id }), ["a"])
        XCTAssertEqual(controller.notebooks.first { $0.id == original.id }?.tags, ["a"])
    }

    // MARK: Search

    func testSearchHasFiveGroupsMatchedWithoutCaseOrAccents() {
        let old = Date(timeIntervalSince1970: 1_000)
        let new = Date(timeIntervalSince1970: 2_000)
        let folders = [
            folder("paper/Economie", name: "Économie", modified: old),
            folder("paper/Econ", name: "Econ", modified: new), folder("paper/Home"),
        ]
        let notebooks = [
            notebook("paper/econometrics.paper", modified: new),
            notebook("paper/Board.paper", kind: .whiteboard),
            notebook("paper/Notes.paper"),
        ]
        let outline = [
            OutlineEntry(
                notebookID: "n", notebookPath: "paper/Notes.paper", notebookTitle: "Notes",
                pageID: "p", pageTitle: "ECON lecture", pageNumber: 2, kind: .notebook, modified: new)
        ]
        func match(_ path: String) -> PageTextMatch {
            PageTextMatch(notebookPath: path, pageID: "x", pageNumber: 1, line: "econ", boxes: [])
        }
        let handwriting = [
            NotebookMatches(notebookPath: "paper/Board.paper", notebookTitle: "Board", pages: [match("paper/Board.paper")]),
            NotebookMatches(notebookPath: "paper/Notes.paper", notebookTitle: "Notes", pages: [match("paper/Notes.paper")]),
            NotebookMatches(notebookPath: "paper/econometrics.paper", notebookTitle: "econometrics", pages: []),
        ]
        let results = PaperController.search(
            "econ", folders: folders, notebooks: notebooks, outline: outline, handwriting: handwriting)
        XCTAssertEqual(results.folders.map(\.name), ["Econ", "Économie"], "newest first")
        XCTAssertEqual(results.documents.map(\.title), ["econometrics"])
        XCTAssertEqual(results.outline.map(\.pageTitle), ["ECON lecture"])
        XCTAssertEqual(results.notebooks.map(\.notebookPath), ["paper/Notes.paper"])
        XCTAssertEqual(results.whiteboards.map(\.notebookPath), ["paper/Board.paper"])
        XCTAssertTrue(
            PaperController.search("  ", folders: folders, notebooks: notebooks, outline: outline,
                handwriting: handwriting).isEmpty)

        let many = (0..<80).map { folder("paper/F\($0)", name: "Folder \($0)") }
        XCTAssertEqual(
            PaperController.search("folder", folders: many, notebooks: [], outline: [], handwriting: [])
                .folders.count,
            LibrarySearchResults.groupLimit)
    }

    func testTheOutlineIsEveryTitledPage() async throws {
        let controller = try makeController()
        let notebook = try XCTUnwrap(
            controller.createDocument(
                title: "Lectures", folderPath: "paper", kind: .notebook, options: .notebookFactory))
        let page = try XCTUnwrap(notebook.pages.first)
        _ = try controller.store.setPageTitle(notebookID: notebook.id, pageID: page, title: "Week 1")
        let entries = await controller.outlineEntries()
        XCTAssertEqual(entries.map(\.pageTitle), ["Week 1"])
        XCTAssertEqual(entries.first?.pageNumber, 1)
        XCTAssertEqual(controller.librarySearch("week", outline: entries).outline.count, 1)
    }

    // MARK: The move picker's tree

    func testTheTreeShowsTheChildrenOfOpenFolders() {
        let folders = [
            folder("paper/Work"), folder("paper/Work/Accounting"), folder("paper/Work/Plans"),
            folder("paper/Home"),
        ]
        let closed = PaperController.folderTree(folders, expanded: [])
        XCTAssertEqual(closed.map(\.name), ["Documents"])
        XCTAssertTrue(closed[0].hasChildren)
        let top = PaperController.folderTree(folders, expanded: ["paper"])
        XCTAssertEqual(top.map(\.name), ["Documents", "Home", "Work"])
        XCTAssertEqual(top.map(\.depth), [0, 1, 1])
        XCTAssertEqual(top.map(\.hasChildren), [true, false, true])
        let open = PaperController.folderTree(folders, expanded: ["paper", "paper/Work"])
        XCTAssertEqual(open.map(\.name), ["Documents", "Home", "Work", "Accounting", "Plans"])
        XCTAssertEqual(open.map(\.depth), [0, 1, 1, 2, 2])
    }

    // MARK: Thumbnails, export, imports

    func testAThumbnailTakesThePapersShape() async throws {
        let controller = try makeController()
        var landscape = NewDocumentOptions.notebookFactory
        landscape.orientation = .landscape
        landscape.size = .a4
        let wide = try XCTUnwrap(
            controller.createDocument(title: "Wide", folderPath: "paper", kind: .notebook, options: landscape))
        let cover = await controller.cover(for: wide)
        XCTAssertEqual(cover.aspect, 842.0 / 595.0, accuracy: 0.01)
        XCTAssertFalse(cover.isBoard)
        let board = try XCTUnwrap(
            controller.createDocument(
                title: "Board", folderPath: "paper", kind: .whiteboard, options: .whiteboardFactory))
        let boardCover = await controller.cover(for: board)
        XCTAssertTrue(boardCover.isBoard)
        XCTAssertEqual(boardCover.aspect, 1366.0 / 1024.0, accuracy: 0.01)
        XCTAssertEqual(boardCover.paperColor, .cream)
        XCTAssertEqual(PaperController.clampedAspect(10), 2)
        XCTAssertEqual(
            DocumentThumbnail.fitted(aspect: 0.75, in: CGSize(width: 70, height: 54)),
            CGSize(width: 41, height: 54))
    }

    func testAFolderExportsAsATreeOfPDFs() throws {
        let work = folder("paper/Work", name: "Work")
        let folders = [work, folder("paper/Work/Accounting", name: "Accounting"), folder("paper/Work/Empty")]
        let notebooks = [
            notebook("paper/Work/Plan.paper"), notebook("paper/Work/Accounting/Ledger.paper"),
            notebook("paper/Work/Accounting/Ledger 2.paper"),
        ]
        let renamed = notebooks.map { notebook -> Notebook in
            var copy = notebook
            if copy.title == "Ledger 2" { copy.title = "Ledger" }
            return copy
        }
        let top = try XCTUnwrap(
            FolderExporter.stage(
                folder: work, folders: folders, notebooks: renamed, pdf: { _ in Data("%PDF".utf8) }))
        addTeardownBlock { try? FileManager.default.removeItem(at: top.deletingLastPathComponent()) }
        let manager = FileManager.default
        XCTAssertEqual(top.lastPathComponent, "Work")
        XCTAssertTrue(manager.fileExists(atPath: top.appending(path: "Plan.pdf").path(percentEncoded: false)))
        XCTAssertTrue(
            manager.fileExists(atPath: top.appending(path: "Accounting/Ledger.pdf").path(percentEncoded: false)))
        XCTAssertTrue(
            manager.fileExists(
                atPath: top.appending(path: "Accounting/Ledger 2.pdf").path(percentEncoded: false)))
        XCTAssertTrue(manager.fileExists(atPath: top.appending(path: "Empty").path(percentEncoded: false)))

        let zip = try XCTUnwrap(FolderExporter.zip(top))
        addTeardownBlock { try? manager.removeItem(at: zip) }
        XCTAssertEqual(zip.lastPathComponent, "Work.zip")
        let head = try Data(contentsOf: zip).prefix(2)
        XCTAssertEqual(Array(head), [0x50, 0x4B], "a ZIP starts with PK")
    }

    func testAPictureBecomesAOnePageDocument() throws {
        let image = UIGraphicsImageRenderer(size: CGSize(width: 300, height: 200)).image { context in
            UIColor.red.setFill()
            context.fill(CGRect(x: 0, y: 0, width: 300, height: 200))
        }
        let data = try XCTUnwrap(PaperController.pdfDocument(fromImage: image))
        let pdf = try XCTUnwrap(PDFDocument(data: data))
        XCTAssertEqual(pdf.pageCount, 1)
        let bounds = try XCTUnwrap(pdf.page(at: 0)).bounds(for: .mediaBox)
        XCTAssertEqual(max(bounds.width, bounds.height), 1024, accuracy: 1)
        XCTAssertGreaterThan(bounds.width, bounds.height)
    }

    func testAnImportedPDFIsANewDocumentInTheFolder() async throws {
        let controller = try makeController()
        let work = try XCTUnwrap(controller.createFolder(name: "Work", in: "paper"))
        let image = UIGraphicsImageRenderer(size: CGSize(width: 20, height: 30)).image { _ in }
        let pdf = try XCTUnwrap(PaperController.pdfDocument(fromImage: image))
        let imported = await controller.importDocument(pdf: pdf, title: "Scan", folderPath: work.path)
        let notebook = try XCTUnwrap(imported)
        XCTAssertEqual(notebook.folderPath, work.path)
        XCTAssertEqual(notebook.pages.count, 1)
        let refused = await controller.importDocument(
            pdf: Data("not a pdf".utf8), title: "Bad", folderPath: work.path)
        XCTAssertNil(refused)
        XCTAssertEqual(controller.notebooks.count, 1, "a failed import leaves nothing behind")
    }

    // MARK: Small pieces

    func testDragPayloadsCarryIdsAndRefuseOtherText() {
        let payload = LibraryDrag.payload(["folder/a", "document/b"])
        XCTAssertEqual(LibraryDrag.ids(from: [payload]), ["folder/a", "document/b"])
        XCTAssertEqual(LibraryDrag.ids(from: ["folder/a"]), [])
    }

    func testTagsAreAddedOnceAndSplitOnCommas() {
        XCTAssertEqual(ItemInfoSheet.adding("Tax, receipts ,tax,", to: ["Receipts"]), ["Receipts", "Tax"])
        XCTAssertEqual(ItemInfoSheet.adding("  ", to: ["a"]), ["a"])
    }

    func testTheTrashSaysHowLongIsLeft() {
        let now = Date(timeIntervalSince1970: 1_700_000_000)
        var item = PaperTrashItem(id: "t", kind: "paper_folder", originalPath: "paper/Work")
        item.expiresAt = ISO8601DateFormatter().string(from: now.addingTimeInterval(12 * 86_400 - 60))
        item.notebookCount = 3
        XCTAssertEqual(TrashView.subtitle(for: item, now: now), "Deletes in 12 days · 3 documents")
        item.expiresAt = ISO8601DateFormatter().string(from: now.addingTimeInterval(3_600))
        item.notebookCount = nil
        XCTAssertEqual(TrashView.subtitle(for: item, now: now), "Deletes in 1 day")
        XCTAssertEqual(TrashView.symbol(for: item), "folder")
    }

    func testARowReadsNameDateKindAndFavorite() {
        var folder = folder("paper/Work", modified: Date(timeIntervalSince1970: 0))
        folder.favorite = true
        let label = LibraryItemText.accessibilityLabel(.folder(folder))
        XCTAssertTrue(label.hasPrefix("Work, "), label)
        XCTAssertTrue(label.hasSuffix(", Folder, Favorite"), label)
        XCTAssertTrue(
            LibraryItemText.accessibilityLabel(.document(notebook("paper/B.paper", kind: .whiteboard)))
                .hasSuffix(", Whiteboard"))
    }
}

/// Where the library is: sections, the open folder, the back chip, following
/// a folder that moved or went, and the arrow keys.
@MainActor
final class LibraryStateTests: XCTestCase {
    private func makeController() throws -> PaperController {
        let root = FileManager.default.temporaryDirectory
            .appending(path: "LibraryStateTests-\(UUID().uuidString)", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }
        return PaperController(store: try PaperStore(root: root))
    }

    func testOpeningAFolderAndGoingBackUp() throws {
        let controller = try makeController()
        let work = try XCTUnwrap(controller.createFolder(name: "Work", in: "paper"))
        let plans = try XCTUnwrap(controller.createFolder(name: "Plans", in: work.path))
        let state = LibraryState()
        state.select(.favorites)
        state.open(plans)
        XCTAssertEqual(state.section, .documents, "a folder opens under Documents")
        XCTAssertEqual(state.currentFolder(controller)?.id, plans.id)
        XCTAssertEqual(state.newItemFolderPath(controller), plans.path)
        state.goUp(controller)
        XCTAssertEqual(state.currentFolder(controller)?.id, work.id)
        XCTAssertEqual(state.keyboardItemID, "folder/\(plans.id)")
        state.goUp(controller)
        XCTAssertNil(state.currentFolder(controller))
        state.select(.recents)
        XCTAssertEqual(state.newItemFolderPath(controller), "paper")
    }

    func testTheOpenFolderIsFollowedWhenItMovesAndLeftWhenItGoes() throws {
        let controller = try makeController()
        let work = try XCTUnwrap(controller.createFolder(name: "Work", in: "paper"))
        let plans = try XCTUnwrap(controller.createFolder(name: "Plans", in: work.path))
        let state = LibraryState()
        state.open(plans)
        controller.rename(.folder(plans), to: "Projects")
        state.follow(controller)
        XCTAssertEqual(state.currentFolder(controller)?.name, "Projects")
        XCTAssertEqual(state.currentPath(controller), "paper/Work/Projects")

        let current = try XCTUnwrap(state.currentFolder(controller))
        _ = controller.trash(.folder(current))
        state.follow(controller)
        XCTAssertEqual(state.currentFolder(controller)?.id, work.id, "up to what is still there")
    }

    func testTheArrowKeysStayInsideTheList() {
        let items: [PaperCore.LibraryItem] = ["a", "b", "c"].map {
            .document(Notebook(id: $0, title: $0, path: "paper/\($0).paper"))
        }
        let state = LibraryState()
        state.moveKeyboard(by: 1, in: items)
        XCTAssertEqual(state.keyboardItemID, "document/a")
        state.moveKeyboard(by: 5, in: items)
        XCTAssertEqual(state.keyboardItemID, "document/c")
        state.moveKeyboard(by: -1, in: items)
        XCTAssertEqual(state.keyboardItemID, "document/b")
        state.moveKeyboard(by: -9, in: items)
        XCTAssertEqual(state.keyboardItemID, "document/a")
        state.keyboardItemID = nil
        state.moveKeyboard(by: -1, in: items)
        XCTAssertEqual(state.keyboardItemID, "document/c")
    }

    func testSearchAndSelectModeDoNotOverlap() {
        let state = LibraryState()
        state.beginSelecting()
        state.selection = ["x"]
        state.beginSearch()
        XCTAssertTrue(state.isSearching)
        XCTAssertFalse(state.isSelecting)
        state.query = "abc"
        state.endSearch()
        XCTAssertEqual(state.query, "")
    }

    /// Cmd-F, Escape, the arrow keys and Return, as the library's keys run
    /// them. This replaces a UI test that the simulator skipped four runs in
    /// five: it delivers hardware keys to SwiftUI's keyboard shortcuts only
    /// now and then, so the keys themselves are checked on the iPad.
    func testTheKeysSearchEscapeWalkAndOpen() {
        let items: [PaperCore.LibraryItem] = ["a", "b", "c"].map {
            .document(Notebook(id: $0, title: $0, path: "paper/\($0).paper"))
        }
        let state = LibraryState()
        state.beginSelecting()
        state.selection = ["document/a"]
        let focus = state.searchFocusRequest
        state.searchFromKeyboard()
        XCTAssertTrue(state.isSearching)
        XCTAssertFalse(state.isSelecting, "Cmd-F left select mode on")
        XCTAssertEqual(state.searchFocusRequest, focus + 1, "Cmd-F did not focus the field")
        state.query = "practice"
        state.escape()
        XCTAssertFalse(state.isSearching, "Escape did not end the search")
        XCTAssertEqual(state.query, "")

        XCTAssertNil(state.activateKeyboardItem(in: items), "Return with no row opened something")
        XCTAssertEqual(state.keyboardItemID, "document/a", "the first Return lands on the first row")
        state.moveKeyboard(by: 1, in: items)
        XCTAssertEqual(state.activateKeyboardItem(in: items)?.id, "document/b", "Return opened nothing")

        // In select mode Return toggles the row instead.
        state.beginSelecting()
        XCTAssertNil(state.activateKeyboardItem(in: items))
        XCTAssertEqual(state.selection, ["document/b"])

        // Escape goes back one thing at a time: a menu, select mode, the sidebar.
        state.isSidebarVisible = true
        state.menu = LibraryMenuRequest(itemID: "document/a", page: .main)
        state.escape()
        XCTAssertNil(state.menu)
        XCTAssertTrue(state.isSelecting)
        state.escape()
        XCTAssertFalse(state.isSelecting)
        XCTAssertTrue(state.isSidebarVisible)
        state.escape()
        XCTAssertFalse(state.isSidebarVisible)
    }
}
