import Foundation
import XCTest

@testable import PaperCore

final class PaperMetadataTests: PaperStoreTestCase {
    func testPageMetadataIsOptionalAndUnknownFieldsSurviveAReadWrite() throws {
        var object = try XCTUnwrap(
            JSONSerialization.jsonObject(with: PaperJSON.encode(Page(id: "page-1")))
                as? [String: Any])
        object.removeValue(forKey: "title")
        object.removeValue(forKey: "bookmarked")
        object["is_bookmarked"] = true
        object["future_metadata"] = ["accent": "blue", "version": 2]
        let input = try JSONSerialization.data(withJSONObject: object)

        let page = try PaperJSON.decode(Page.self, from: input)
        XCTAssertEqual(page.title, nil)
        XCTAssertTrue(page.bookmarked)
        XCTAssertTrue(page.isBookmarked)
        XCTAssertEqual(page.additionalFields["future_metadata"], .object([
            "accent": .string("blue"), "version": .int(2),
        ]))

        let encoded = try XCTUnwrap(
            JSONSerialization.jsonObject(with: PaperJSON.encode(page)) as? [String: Any])
        XCTAssertEqual(encoded["bookmarked"] as? Bool, true)
        XCTAssertNil(encoded["is_bookmarked"])
        XCTAssertNotNil(encoded["future_metadata"])
    }

    func testPageTextKeepsRawOCRAIAndPrefersCorrectionsForSearch() throws {
        let ai = AITranscript(text: "AI reading", model: "test", revision: 0)
        let raw = PageText(
            lines: [
                RecognizedLine(
                    text: "teh plan", box: PageObject.Frame(x: 10, y: 20, width: 80, height: 20),
                    confidence: 0.6)
            ],
            transcript: "teh plan",
            corrections: [TextCorrection(before: "teh", after: "the", lineIndex: 0)],
            transcriptAI: ai,
            correctionsRevision: 0)

        XCTAssertEqual(raw.transcript, "teh plan")
        XCTAssertEqual(raw.searchableTranscript, "the plan")
        XCTAssertEqual(raw.searchableLines.first?.text, "the plan")
        XCTAssertFalse(PageText(lines: [], transcript: "", correctedTranscript: "typed").isEmpty)

        let roundTrip = try PaperJSON.decode(PageText.self, from: PaperJSON.encode(raw))
        XCTAssertEqual(roundTrip, raw)
        XCTAssertEqual(roundTrip.transcriptAI, ai)
    }

    func testCorrectionMutationMaterializesSearchTextAndRetainsAI() throws {
        let notebook = try store.createNotebook(title: "Corrections")
        let pageID = try XCTUnwrap(notebook.pages.first)
        let page = try store.page(notebookPath: notebook.path, id: pageID)
        let ai = AITranscript(text: "AI reading", model: "test", revision: page.revision)
        try store.writePageText(
            PageText(
                lines: [
                    RecognizedLine(
                        text: "teh plan", box: PageObject.Frame(x: 1, y: 2, width: 3, height: 4),
                        confidence: 0.8)
                ],
                transcript: "teh plan", transcriptAI: ai),
            notebookPath: notebook.path, pageID: pageID)

        let corrected = try store.addPageTextCorrection(
            notebookID: notebook.id,
            pageID: pageID,
            correction: TextCorrection(before: "teh", after: "the", lineIndex: 0))
        XCTAssertEqual(corrected.transcript, "teh plan")
        XCTAssertEqual(corrected.correctedTranscript, "the plan")
        XCTAssertEqual(corrected.searchableTranscript, "the plan")
        XCTAssertEqual(corrected.transcriptAI, ai)
        XCTAssertEqual(store.textIndex.search("the").first?.pages.first?.pageID, pageID)
    }

    func testPageAndNotebookMetadataMutationsPersistAndCoverCanBeAutomatic() throws {
        let notebook = try store.createNotebook(title: "Metadata", pageCount: 2)
        let first = try XCTUnwrap(notebook.pages.first)
        let second = try XCTUnwrap(notebook.pages.dropFirst().first)

        let titled = try store.setPageTitle(notebookID: notebook.id, pageID: first, title: "Agenda")
        XCTAssertEqual(titled.title, "Agenda")
        XCTAssertTrue(try store.setPageBookmarked(notebookID: notebook.id, pageID: first, true).bookmarked)
        let updated = try store.updateNotebookMetadata(
            notebookID: notebook.id, tags: ["work", "planning"], template: .grid, paperSize: .letter)
        XCTAssertEqual(updated.tags, ["work", "planning"])
        XCTAssertEqual(updated.template, .grid)
        XCTAssertEqual(updated.paperSize, .letter)

        XCTAssertEqual(try store.setCover(notebookID: notebook.id, pageID: second).cover, second)
        XCTAssertNil(try store.setCover(notebookID: notebook.id, pageID: nil).cover)
        var latest = try store.page(notebookPath: notebook.path, id: second)
        latest.modified = Date(timeIntervalSinceNow: 100)
        try store.save(page: latest, notebookPath: notebook.path)
        XCTAssertEqual(try store.coverPage(ofNotebook: notebook.id)?.id, second)

        let reopened = try PaperStore(root: root)
        XCTAssertEqual(try reopened.page(notebookPath: notebook.path, id: first).title, "Agenda")
        XCTAssertTrue(try reopened.page(notebookPath: notebook.path, id: first).bookmarked)
        XCTAssertNil(try reopened.notebook(id: notebook.id).cover)
    }

    func testPaperLinkUsesStableIDsAndRoundTripsRegionCoordinates() throws {
        let normalized = PageObject.Frame(x: 0.1, y: 0.2, width: 0.3, height: 0.4)
        let link = PaperLink(
            notebookID: "notebook-uuid", pageID: "page-uuid", normalizedRect: normalized)
        XCTAssertEqual(link.notebookUUID, "notebook-uuid")
        XCTAssertEqual(link.pageUUID, "page-uuid")
        XCTAssertEqual(
            link.pageRect(for: CGSize(width: 1000, height: 800)),
            PageObject.Frame(x: 100, y: 160, width: 300, height: 320))
        XCTAssertEqual(try PaperJSON.decode(PaperLink.self, from: PaperJSON.encode(link)), link)

        let legacy = Data(
            #"{"notebook_uuid":"notebook-uuid","page_uuid":"page-uuid","rect":{"x":1,"y":2,"width":3,"height":4},"coordinate_space":"page"}"#.utf8)
        let decoded = try PaperJSON.decode(PaperLink.self, from: legacy)
        XCTAssertEqual(decoded.pageRect, PageObject.Frame(x: 1, y: 2, width: 3, height: 4))
    }

    func testSyncWriterPreservesPageMetadataAndExactCustomSize() throws {
        let notebook = try store.createNotebook(title: "Wire")
        let pageID = try XCTUnwrap(notebook.pages.first)
        var page = try store.page(notebookPath: notebook.path, id: pageID)
        page.title = "Imported PDF"
        page.bookmarked = true
        page.customSize = SizePoints(width: 701.25, height: 999.5)
        page.additionalFields["future_flag"] = .bool(true)
        try store.save(page: page, notebookPath: notebook.path)

        let wire = try XCTUnwrap(store.pagePartJSON(notebookPath: notebook.path, pageID: pageID))
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: wire) as? [String: Any])
        let size = try XCTUnwrap(object["size"] as? [String: Any])
        XCTAssertEqual(size["width"] as? Double, 701.25)
        XCTAssertEqual(size["height"] as? Double, 999.5)
        XCTAssertEqual(object["size_points"] as? [String: Double], ["width": 701.25, "height": 999.5])
        XCTAssertEqual(object["title"] as? String, "Imported PDF")
        XCTAssertEqual(object["bookmarked"] as? Bool, true)
        XCTAssertEqual(object["future_flag"] as? Bool, true)
        XCTAssertNil(object["revision"])

        let remote = Data(
            #"{"id":"remote-page","title":"Remote","bookmarked":true,"template":"blank","size":{"width":701.25,"height":999.5},"created":"2026-01-01T00:00:00.000Z","modified":"2026-01-01T00:00:00.000Z","revision":4,"objects":[],"pdf":null}"#.utf8)
        let applied = try XCTUnwrap(
            store.applyRemotePage(
                rawPageJSON: remote, notebookPath: notebook.path, pageID: "remote-page",
                drawing: nil, render: nil))
        XCTAssertEqual(applied.title, "Remote")
        XCTAssertTrue(applied.bookmarked)
        XCTAssertEqual(applied.customSize, SizePoints(width: 701.25, height: 999.5))
        XCTAssertEqual(applied.pointSize, CGSize(width: 701.25, height: 999.5))
    }
}
