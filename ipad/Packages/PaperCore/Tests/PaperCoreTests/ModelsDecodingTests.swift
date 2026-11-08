import Foundation
import XCTest

@testable import PaperCore

/// A value a newer build writes must not make a page, or the notebook that
/// lists it, unreadable to this one: unknown names fall back, unknown object
/// kinds are carried through untouched.
final class ModelsDecodingTests: XCTestCase {
    private func page(_ fields: String) throws -> Page {
        try PaperJSON.decode(
            Page.self,
            from: Data(
                #"{"id":"p1","created":"2026-01-01T00:00:00.000Z","modified":"2026-01-01T00:00:00.000Z",\#(fields)}"#
                    .utf8))
    }

    private func object(_ data: Data) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    // MARK: - Template and size

    func testAnUnknownTemplateFallsBackToRuled() throws {
        XCTAssertEqual(try page(#""template":"hexagonal","size":"a4""#).template, .ruled)
    }

    func testAnUnknownSizeFallsBackToA4() throws {
        XCTAssertEqual(try page(#""template":"grid","size":"tabloid""#).size, .a4)
    }

    func testNamesMatchCaseInsensitivelyLikeTheServer() throws {
        let decoded = try page(#""template":"Dotted","size":"INFINITE""#)
        XCTAssertEqual(decoded.template, .dotted)
        XCTAssertEqual(decoded.size, .infinite)
        XCTAssertEqual(try page(#""template":"blank","size":"ipad""#).size, .iPad)
    }

    func testAMissingOrMisshapenSizeStillFallsBack() throws {
        XCTAssertEqual(try page(#""template":"blank""#).size, .a4)
        XCTAssertEqual(try page(#""size":{"width":1,"height":2}"#).size, .a4)
        XCTAssertEqual(try page(#""size":"a4""#).template, .ruled)
    }

    func testASizeNameOnDiskIsNotKeptBesideSize() throws {
        let decoded = try page(#""size":"infinite","size_name":"iPad""#)
        XCTAssertEqual(decoded.size, .infinite)
        XCTAssertNil(decoded.additionalFields["size_name"])
        XCTAssertNil(try object(PaperJSON.encode(decoded))["size_name"])
    }

    // MARK: - Object kinds

    private let video =
        #"{"id":"o1","type":"video","frame":{"x":1,"y":2,"width":30,"height":40},"src":"paper/A.paper/assets/clip-0123456789abcdef.mov","loop":true}"#

    func testAnUnknownObjectKindDecodesAndEncodesUnderItsOwnName() throws {
        let decoded = try page(#""size":"a4","objects":[\#(video)]"#)
        let object = try XCTUnwrap(decoded.objects.first)
        XCTAssertEqual(object.type, .unknown("video"))
        XCTAssertEqual(object.type.rawValue, "video")
        XCTAssertEqual(object.frame, PageObject.Frame(x: 1, y: 2, width: 30, height: 40))

        let encoded = try self.object(PaperJSON.encode(decoded))
        let objects = try XCTUnwrap(encoded["objects"] as? [[String: Any]])
        XCTAssertEqual(objects.first?["type"] as? String, "video")
        XCTAssertEqual(
            objects.first?["src"] as? String, "paper/A.paper/assets/clip-0123456789abcdef.mov",
            "its payload survives the round trip too")
        XCTAssertEqual(objects.first?["loop"] as? Bool, true)
        XCTAssertEqual(try PaperJSON.decode(Page.self, from: PaperJSON.encode(decoded)), decoded)
    }

    func testKnownKindsRoundTripAsBefore() throws {
        let text = PageObject(
            id: "t1", type: .text, frame: .init(x: 1, y: 2, width: 3, height: 4), text: "hi",
            fontSize: 20)
        let image = PageObject(
            id: "i1", type: .image, frame: .init(x: 5, y: 6, width: 7, height: 8),
            asset: "paper/A.paper/assets/a-0123456789abcdef.png",
            crop: .init(x: 0, y: 0, width: 0.5, height: 0.5))
        for object in [text, image] {
            let encoded = try PaperJSON.encode(object)
            XCTAssertEqual(try PaperJSON.decode(PageObject.self, from: encoded), object)
            XCTAssertEqual(
                Set(try self.object(encoded).keys),
                object.type == .text
                    ? ["id", "type", "frame", "text", "fontSize"]
                    : ["id", "type", "frame", "asset", "crop"])
        }
        XCTAssertEqual(PageObject.Kind(rawValue: "text"), .text)
        XCTAssertEqual(PageObject.Kind(rawValue: "image"), .image)
    }

    func testAnUnknownObjectKindDoesNotBreakTheWire() throws {
        let remote = #"{"id":"p1","revision":3,"size":{"width":595,"height":842},"objects":[\#(video)]}"#
        let decoded = try PaperJSON.decode(RemotePage.self, from: Data(remote.utf8))
        XCTAssertEqual(decoded.objects?.first?.type, .unknown("video"))

        // One such page used to fail the whole manifest.
        let manifest = try PaperJSON.decode(
            NotebookManifest.self,
            from: Data(#"{"path":"paper/A.paper","pages":["p1"],"pages_detail":[\#(remote)]}"#.utf8))
        XCTAssertEqual(manifest.pagesDetail?.map(\.id), ["p1"])
    }

    func testAnUnknownObjectKindSurvivesAPullAndThePushAfterIt() throws {
        let root = FileManager.default.temporaryDirectory
            .appending(path: "ModelsDecodingTests-\(UUID().uuidString)", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let store = try PaperStore(root: root)
        let notebook = try store.createNotebook(title: "Clips")

        let remote = #"{"id":"p1","template":"hexagonal","revision":3,"size":{"width":595,"height":842},"objects":[\#(video)]}"#
        let applied = try XCTUnwrap(
            store.applyRemotePage(
                rawPageJSON: Data(remote.utf8), notebookPath: notebook.path, pageID: "p1",
                drawing: nil, render: nil))
        XCTAssertEqual(applied.template, .ruled)
        XCTAssertEqual(applied.objects.first?.type, .unknown("video"))

        let wire = try object(
            XCTUnwrap(store.pagePartJSON(notebookPath: notebook.path, pageID: "p1")))
        let objects = try XCTUnwrap(wire["objects"] as? [[String: Any]])
        XCTAssertEqual(objects.first?["type"] as? String, "video")
        XCTAssertNotNil(objects.first?["src"])
    }
}
