import Foundation
import XCTest

@testable import PaperCore

/// A store rooted in a throwaway directory, torn down after every test.
class PaperStoreTestCase: XCTestCase {
    var root: URL!
    var store: PaperStore!

    override func setUpWithError() throws {
        try super.setUpWithError()
        root = FileManager.default.temporaryDirectory
            .appending(path: "PaperStoreTests-\(UUID().uuidString)", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        store = try PaperStore(root: root)
    }

    override func tearDownWithError() throws {
        store = nil
        if let root { try? FileManager.default.removeItem(at: root) }
        root = nil
        try super.tearDownWithError()
    }

    func exists(_ url: URL) -> Bool {
        FileManager.default.fileExists(atPath: url.path(percentEncoded: false))
    }
}

final class PaperStoreTests: PaperStoreTestCase {
    func testCreatingANotebookWritesTheFrozenLayout() throws {
        let notebook = try store.createNotebook(title: "Ideas", folder: "Work")

        XCTAssertEqual(notebook.path, "paper/Work/Ideas.paper")
        XCTAssertEqual(notebook.folder, "Work")
        XCTAssertEqual(notebook.pages.count, 1)
        XCTAssertTrue(exists(store.paths.notebookJSON(notebook.path)))
        let pageID = try XCTUnwrap(notebook.pages.first)
        XCTAssertTrue(exists(store.paths.pageJSON(notebook.path, pageID)))
        // A nil cover means automatic selection (the most recently modified
        // page), rather than pinning the first page as a side effect of
        // creating the notebook.
        XCTAssertNil(notebook.cover)
        XCTAssertEqual(try store.coverPage(ofNotebook: notebook.id)?.id, pageID)

        // The default the plan preselects.
        let defaulted = try store.createNotebook(title: "Notebook 1")
        XCTAssertEqual(defaulted.template, .ruled)
        XCTAssertEqual(defaulted.paperSize, .a4)
        XCTAssertEqual(defaulted.path, "paper/Notebook 1.paper")
    }

    func testTitlesThatCollideGetDistinctPackages() throws {
        let first = try store.createNotebook(title: "Ideas")
        let second = try store.createNotebook(title: "Ideas")
        XCTAssertEqual(first.path, "paper/Ideas.paper")
        XCTAssertEqual(second.path, "paper/Ideas 2.paper")
        XCTAssertNotEqual(first.id, second.id)
    }

    func testUnsafeTitlesAreSluggedNotObeyed() {
        XCTAssertEqual(PaperPaths.slug("../../etc/passwd"), ".. .. etc passwd")
        XCTAssertEqual(PaperPaths.slug("  "), "Untitled")
        XCTAssertEqual(PaperPaths.slug("NUL"), "_NUL")
        XCTAssertEqual(PaperPaths.slug("Report."), "Report")
        XCTAssertFalse(PaperPaths.isSafeNotebookPath("paper/../x.paper"))
        XCTAssertFalse(PaperPaths.isSafeNotebookPath("notes/x.paper"))
        XCTAssertTrue(PaperPaths.isSafeNotebookPath("paper/Work/x.paper"))
    }

    /// One slug spec with the server's `media_slug`: the vector file under
    /// `backend/tests/fixtures` is checked by pytest and by this test.
    func testSlugMatchesTheSharedVectors() throws {
        var repo = URL(fileURLWithPath: #filePath)
        // Up past this file, PaperCoreTests, Tests, PaperCore, Packages and ipad.
        for _ in 0..<6 { repo.deleteLastPathComponent() }
        let url = repo.appending(path: "backend/tests/fixtures/slug_vectors.json")
        guard let data = try? Data(contentsOf: url) else {
            throw XCTSkip("no slug vectors at \(url.path(percentEncoded: false))")
        }
        struct Vector: Decodable {
            let title: String
            let slug: String
        }
        let vectors = try JSONDecoder().decode([Vector].self, from: data)
        XCTAssertGreaterThan(vectors.count, 10)
        for vector in vectors {
            let slug = PaperPaths.slug(vector.title)
            // `==` on String is canonical equivalence; the contract is exact
            // scalars, which is what ends up in the directory name.
            XCTAssertEqual(
                slug.unicodeScalars.map(\.value), vector.slug.unicodeScalars.map(\.value),
                "slug(\(vector.title.debugDescription)) = \(slug.debugDescription)")
            XCTAssertLessThanOrEqual(slug.unicodeScalars.count, 80)
            XCTAssertLessThanOrEqual(slug.utf8.count, 200)
        }
        // The decoder must not have normalised the titles on the way in, or
        // the NFC vectors prove nothing.
        XCTAssertTrue(vectors.contains { $0.title.unicodeScalars.map(\.value) == [0x65, 0x301] })
    }

    func testRenameMovesThePackageAndKeepsTheID() throws {
        let notebook = try store.createNotebook(title: "Ideas")
        let renamed = try store.rename(notebookID: notebook.id, to: "Plans")

        XCTAssertEqual(renamed.id, notebook.id)
        XCTAssertEqual(renamed.path, "paper/Plans.paper")
        XCTAssertFalse(exists(store.paths.url(forVaultPath: "paper/Ideas.paper")))
        XCTAssertTrue(exists(store.paths.notebookJSON("paper/Plans.paper")))
        XCTAssertEqual(try store.notebook(id: notebook.id).title, "Plans")
    }

    func testMoveChangesFolderOnly() throws {
        let notebook = try store.createNotebook(title: "Ideas")
        let moved = try store.move(notebookID: notebook.id, toFolder: "Work/2026")
        XCTAssertEqual(moved.path, "paper/Work/2026/Ideas.paper")
        XCTAssertEqual(try store.folders(), ["Work/2026"])
    }

    func testFavouriteRoundTripsThroughTheFile() throws {
        let notebook = try store.createNotebook(title: "Ideas")
        _ = try store.setFavorite(notebookID: notebook.id, true)
        let onDisk = try AtomicFile.readJSON(
            Notebook.self, at: store.paths.notebookJSON(notebook.path))
        XCTAssertTrue(onDisk.favorite)
        XCTAssertTrue(try store.notebook(id: notebook.id).favorite)
    }

    func testDuplicateCopiesInkUnderNewIDs() throws {
        let notebook = try store.createNotebook(title: "Ideas")
        let pageID = try XCTUnwrap(notebook.pages.first)
        try store.writeDrawing(Data("ink".utf8), notebookPath: notebook.path, pageID: pageID)
        try AtomicFile.write(Data("png".utf8), to: store.paths.render(notebook.path, pageID))
        let second = try store.createPage(notebookID: notebook.id, after: pageID)
        try store.writeDrawing(Data("more".utf8), notebookPath: notebook.path, pageID: second.id)

        let copy = try store.duplicate(notebookID: notebook.id)
        XCTAssertEqual(copy.title, "Ideas copy")
        XCTAssertNotEqual(copy.id, notebook.id)
        let copiedPage = try XCTUnwrap(copy.pages.first)
        XCTAssertNotEqual(copiedPage, pageID)
        XCTAssertEqual(
            store.drawingData(notebookPath: copy.path, pageID: copiedPage), Data("ink".utf8))
        XCTAssertEqual(
            store.drawingData(notebookPath: copy.path, pageID: copy.pages[1]), Data("more".utf8))
        // The render rides the create-PUT, so the web viewer has the copy at once.
        XCTAssertEqual(
            store.renderData(notebookPath: copy.path, pageID: copiedPage), Data("png".utf8))
        // The original is untouched.
        XCTAssertEqual(
            store.drawingData(notebookPath: notebook.path, pageID: pageID), Data("ink".utf8))

        // Exactly the source's pages, in its order, in every place order lives:
        // no blank page left over from creating the package.
        XCTAssertEqual(copy.pages.count, 2)
        XCTAssertEqual(try store.pages(ofNotebook: copy.id).map(\.id), copy.pages)
        XCTAssertEqual(
            try AtomicFile.readJSON(Notebook.self, at: store.paths.notebookJSON(copy.path)).pages,
            copy.pages)
        let directories = try FileManager.default.contentsOfDirectory(
            atPath: store.paths.pagesDirectory(copy.path).path(percentEncoded: false))
        XCTAssertEqual(Set(directories), Set(copy.pages))
        try store.reconcile()
        XCTAssertEqual(try store.notebook(id: copy.id).pages, copy.pages)
        XCTAssertEqual(try store.pages(ofNotebook: copy.id).map(\.id), copy.pages)

        // Every page goes up whole, in order, and the order itself follows.
        let ops = try store.journal.pending().filter { $0.notebookPath == copy.path }
        XCTAssertFalse(ops.contains { $0.kind == .pageCreate })
        let creates = ops.filter(\.createsPage)
        XCTAssertEqual(creates.map(\.kind), [.pagePut, .pagePut])
        XCTAssertEqual(creates.map(\.pageID), copy.pages)
        XCTAssertEqual(creates.map { $0.payload["after"] }, [nil, copy.pages[0]])
        let patch = try XCTUnwrap(ops.last { $0.kind == .notebookPatch })
        XCTAssertEqual(patch.payload["pages"], copy.pages.joined(separator: ","))
    }

    func testDuplicateCopiesAssetsAndPointsTheCopyAtThem() throws {
        let notebook = try store.createNotebook(title: "Scans")
        let pageID = try XCTUnwrap(notebook.pages.first)
        let png = try PageRendererObjectFixtures.redPNG()
        let imagePath = try store.storeAsset(
            png, preferredName: "swatch.png", notebookPath: notebook.path)
        try store.setObjects(
            [
                PageObject(
                    type: .image, frame: PageObject.Frame(x: 10, y: 10, width: 50, height: 50),
                    asset: imagePath),
                PageObject(
                    type: .text, frame: PageObject.Frame(x: 10, y: 80, width: 200, height: 40),
                    text: "Caption"),
            ],
            notebookPath: notebook.path, pageID: pageID)
        let imported = try PDFImporter(store: store).importPDF(
            data: try PDFFixture.threePages(), name: "source.pdf", into: notebook.id)
        let pdfBytes = try XCTUnwrap(store.assets.data(forAssetPath: imported.assetPath))

        let copy = try store.duplicate(notebookID: notebook.id)
        let copyAssets = "\(copy.path)/assets/"
        XCTAssertEqual(
            store.assets.assetPaths(forNotebook: copy.path),
            store.assets.assetPaths(forNotebook: notebook.path).map {
                copyAssets + ($0 as NSString).lastPathComponent
            })

        let pages = try store.pages(ofNotebook: copy.id)
        XCTAssertEqual(pages.count, 4)
        let image = try XCTUnwrap(pages[0].objects.first?.asset)
        XCTAssertEqual(image, copyAssets + (imagePath as NSString).lastPathComponent)
        XCTAssertNil(pages[0].objects[1].asset)
        XCTAssertEqual(pages[0].objects[1].text, "Caption")
        let pdfName = (imported.assetPath as NSString).lastPathComponent
        XCTAssertEqual(
            pages.dropFirst().map { $0.pdf?.asset }, Array(repeating: copyAssets + pdfName, count: 3))
        XCTAssertEqual(pages.dropFirst().map { $0.pdf?.pageIndex }, [0, 1, 2])
        // The source keeps pointing at its own files.
        XCTAssertEqual(
            try store.page(notebookPath: notebook.path, id: pageID).objects.first?.asset, imagePath)

        // The copy stands on its own once the source is gone.
        _ = try store.delete(notebookID: notebook.id)
        XCTAssertEqual(store.assets.data(forAssetPath: image), png)
        XCTAssertEqual(
            store.assets.data(forAssetPath: try XCTUnwrap(pages[1].pdf?.asset)), pdfBytes)
    }

    func testImportedPDFPagesAreJournalledAsCreatesThatCarryTheirBinding() throws {
        let notebook = try store.createNotebook(title: "Imports")
        let first = try XCTUnwrap(notebook.pages.first)
        let result = try PDFImporter(store: store).importPDF(
            data: try PDFFixture.threePages(), name: "source.pdf", into: notebook.id, after: first)
        let ids = result.pages.map(\.id)

        let ops = try store.journal.pending().filter { ids.contains($0.pageID) }
        XCTAssertFalse(ops.contains { $0.kind == .pageCreate })
        XCTAssertEqual(ops.map(\.kind), [.pagePut, .pagePut, .pagePut])
        XCTAssertTrue(ops.allSatisfy(\.createsPage))
        XCTAssertEqual(ops.map(\.pageID), ids)
        XCTAssertEqual(ops.map { $0.payload["after"] }, [first] + ids.dropLast())

        for (index, page) in result.pages.enumerated() {
            let body = try XCTUnwrap(
                store.pagePartJSON(notebookPath: notebook.path, pageID: page.id))
            let json = try XCTUnwrap(JSONSerialization.jsonObject(with: body) as? [String: Any])
            let pdf = try XCTUnwrap(json["pdf"] as? [String: Any])
            XCTAssertEqual(pdf["asset"] as? String, result.assetPath)
            XCTAssertEqual(pdf["pageIndex"] as? Int, index)
            XCTAssertNotNil(json["size_points"])
        }
    }

    func testDeleteMovesToTrashAndUndoBringsItBackWhole() throws {
        let notebook = try store.createNotebook(title: "Ideas")
        let pageID = try XCTUnwrap(notebook.pages.first)
        try store.writeDrawing(Data("ink".utf8), notebookPath: notebook.path, pageID: pageID)

        let token = try store.delete(notebookID: notebook.id)
        XCTAssertFalse(exists(store.paths.url(forVaultPath: notebook.path)))
        XCTAssertTrue(try store.notebooks().isEmpty)

        let restored = try XCTUnwrap(try store.restore(token))
        XCTAssertEqual(restored.id, notebook.id)
        XCTAssertEqual(restored.pages, notebook.pages)
        XCTAssertEqual(
            store.drawingData(notebookPath: notebook.path, pageID: pageID), Data("ink".utf8))
    }

    func testPageCreateReorderAndDelete() throws {
        let notebook = try store.createNotebook(title: "Ideas")
        let first = try XCTUnwrap(notebook.pages.first)
        let second = try store.createPage(notebookID: notebook.id, after: first)
        let third = try store.createPage(notebookID: notebook.id, after: first)

        XCTAssertEqual(try store.notebook(id: notebook.id).pages, [first, third.id, second.id])

        try store.reorderPages(notebookID: notebook.id, order: [second.id, first])
        // Unknown ids filtered, omitted ids appended -- never dropped.
        XCTAssertEqual(try store.notebook(id: notebook.id).pages, [second.id, first, third.id])

        let token = try store.deletePage(notebookID: notebook.id, pageID: first)
        XCTAssertEqual(try store.notebook(id: notebook.id).pages, [second.id, third.id])
        _ = try store.restore(token)
        XCTAssertEqual(try store.notebook(id: notebook.id).pages, [second.id, first, third.id])
    }

    func testViewStateIsPerNotebookAndSurvivesAReopen() throws {
        let notebook = try store.createNotebook(title: "Ideas")
        XCTAssertEqual(store.viewState(notebookID: notebook.id).zoom, 1)

        let state = NotebookViewState(
            lastPageID: notebook.pages.first, zoom: 2.5, offset: CGPoint(x: 10, y: 240))
        try store.setViewState(state, notebookID: notebook.id)

        let reopened = try PaperStore(root: root)
        XCTAssertEqual(reopened.viewState(notebookID: notebook.id), state)
    }

    func testReconcilerRebuildsTheIndexFromTheTreeAndHealsPageOrder() throws {
        let notebook = try store.createNotebook(title: "Ideas")
        let first = try XCTUnwrap(notebook.pages.first)
        let orphan = PaperID.make()
        try FileManager.default.createDirectory(
            at: store.paths.pageDirectory(notebook.path, orphan), withIntermediateDirectories: true)
        try store.writeDrawing(Data("ink".utf8), notebookPath: notebook.path, pageID: orphan)

        // A brand new store over the same tree: nothing in SQLite yet.
        try FileManager.default.removeItem(at: store.paths.database)
        let rebuilt = try PaperStore(root: root)
        let report = try rebuilt.reconcile()

        XCTAssertEqual(report.indexed, 1)
        let healed = try XCTUnwrap(try rebuilt.notebook(path: notebook.path))
        XCTAssertEqual(healed.pages.count, 2)
        XCTAssertEqual(healed.pages.first, first)
        XCTAssertTrue(healed.pages.contains(orphan))
        // The recovered page got a page.json so it is a real page again.
        XCTAssertTrue(exists(rebuilt.paths.pageJSON(notebook.path, orphan)))
    }

    func testReconcilerDropsRowsForPackagesThatVanished() throws {
        let notebook = try store.createNotebook(title: "Ideas")
        try FileManager.default.removeItem(at: store.paths.url(forVaultPath: notebook.path))
        let report = try store.reconcile()
        XCTAssertEqual(report.removed, 1)
        XCTAssertTrue(try store.notebooks().isEmpty)
    }

    func testClearDerivedFilesRemovesRendersAndIndexRowsButKeepsInkAndText() throws {
        let notebook = try store.createNotebook(title: "Ideas")
        let pageID = try XCTUnwrap(notebook.pages.first)
        try store.writeDrawing(Data("ink".utf8), notebookPath: notebook.path, pageID: pageID)
        try Data("png".utf8).write(to: store.paths.render(notebook.path, pageID))
        let pdfCache = store.paths.pageDirectory(notebook.path, pageID)
            .appending(path: "pdf-2x.png", directoryHint: .notDirectory)
        try Data("pdf".utf8).write(to: pdfCache)
        let cover = PageRenderer.coverURL(pageID: pageID)
        try FileManager.default.createDirectory(
            at: cover.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data("jpg".utf8).write(to: cover)
        defer { try? FileManager.default.removeItem(at: cover) }
        let text = PageText(
            lines: [
                RecognizedLine(
                    text: "quarterly review",
                    box: PageObject.Frame(x: 60, y: 120, width: 200, height: 24),
                    confidence: 0.9)
            ],
            correctedTranscript: "quarterly review, corrected")
        try store.writePageText(text, notebookPath: notebook.path, pageID: pageID)
        store.indexPageText(pageID: pageID, notebookPath: notebook.path)
        XCTAssertNotNil(store.textIndex.text(pageID: pageID))

        // Asking for a source file by name must not reach it.
        let removed = try store.clearDerivedFiles(
            alsoPageFiles: ["pdf-2x.png", "drawing.pkdrawing", "../page.json"])

        XCTAssertEqual(removed, 3, "render, PDF cache and cover")
        XCTAssertFalse(exists(store.paths.render(notebook.path, pageID)))
        XCTAssertFalse(exists(pdfCache))
        XCTAssertFalse(exists(cover))
        XCTAssertEqual(
            store.drawingData(notebookPath: notebook.path, pageID: pageID), Data("ink".utf8))
        XCTAssertTrue(exists(store.paths.pageJSON(notebook.path, pageID)))
        XCTAssertEqual(
            store.pageText(notebookPath: notebook.path, pageID: pageID)?.correctedTranscript,
            "quarterly review, corrected", "the transcript and its corrections are kept")
        XCTAssertNil(store.textIndex.text(pageID: pageID))

        // The rows come straight back from text.json...
        store.indexPageText(pageID: pageID, notebookPath: notebook.path)
        XCTAssertEqual(store.textIndex.text(pageID: pageID)?.transcript, "quarterly review")

        // ...and a reconcile brings them back too: with the row gone there is
        // no `indexedAt` for the mtime shortcut to trust.
        try store.clearDerivedFiles()
        XCTAssertNil(store.textIndex.text(pageID: pageID))
        try store.reconcile()
        XCTAssertEqual(
            store.textIndex.text(pageID: pageID)?.correctedTranscript, "quarterly review, corrected")
    }
}
