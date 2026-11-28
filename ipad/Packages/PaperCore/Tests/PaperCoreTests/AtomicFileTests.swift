import Foundation
import XCTest

@testable import PaperCore

final class AtomicFileTests: XCTestCase {
    private var directory: URL!

    override func setUpWithError() throws {
        directory = FileManager.default.temporaryDirectory
            .appending(path: "AtomicFileTests-\(UUID().uuidString)", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        if let directory { try? FileManager.default.removeItem(at: directory) }
    }

    func testWritesThroughIntermediateDirectories() throws {
        let url = directory.appending(path: "a/b/c/drawing.pkdrawing", directoryHint: .notDirectory)
        try AtomicFile.write(Data("ink".utf8), to: url)
        XCTAssertEqual(try Data(contentsOf: url), Data("ink".utf8))
    }

    func testReplacesInPlaceAndLeavesNoTemporaries() throws {
        let url = directory.appending(path: "page.json", directoryHint: .notDirectory)
        try AtomicFile.write(Data("one".utf8), to: url)
        try AtomicFile.write(Data("two".utf8), to: url)

        XCTAssertEqual(try Data(contentsOf: url), Data("two".utf8))
        let leftovers = try FileManager.default.contentsOfDirectory(
            at: directory, includingPropertiesForKeys: nil, options: [])
        XCTAssertEqual(leftovers.map(\.lastPathComponent), ["page.json"])
    }

    func testJSONRoundTripKeepsDatesAndOptionals() throws {
        let url = directory.appending(path: "page.json", directoryHint: .notDirectory)
        let page = Page(
            template: .grid, size: .letter, revision: 4,
            objects: [
                PageObject(type: .text, frame: .init(x: 1, y: 2, width: 3, height: 4), text: "hi")
            ],
            conflictOf: "abc")
        try AtomicFile.writeJSON(page, to: url)
        let decoded = try AtomicFile.readJSON(Page.self, at: url)

        XCTAssertEqual(decoded.id, page.id)
        XCTAssertEqual(decoded.template, .grid)
        XCTAssertEqual(decoded.size, .letter)
        XCTAssertEqual(decoded.revision, 4)
        XCTAssertEqual(decoded.objects.first?.text, "hi")
        XCTAssertEqual(decoded.conflictOf, "abc")
        XCTAssertEqual(
            decoded.created.timeIntervalSince1970, page.created.timeIntervalSince1970, accuracy: 0.01
        )
    }

    func testPageJSONUsesTheFrozenKeys() throws {
        let data = try PaperJSON.encode(Page())
        let text = try XCTUnwrap(String(data: data, encoding: .utf8))
        XCTAssertTrue(text.contains("\"render_revision\""))
        XCTAssertTrue(text.contains("\"text_revision\""))
    }

    func testAFailedWriteLeavesTheOldFileIntact() throws {
        let url = directory.appending(path: "page.json", directoryHint: .notDirectory)
        try AtomicFile.write(Data("good".utf8), to: url)
        // A directory where the temporary file would go cannot be created.
        let blocked = directory.appending(path: "blocked", directoryHint: .notDirectory)
        try AtomicFile.write(Data(), to: blocked)
        XCTAssertThrowsError(
            try AtomicFile.write(
                Data("bad".utf8),
                to: blocked.appending(path: "child.json", directoryHint: .notDirectory)))
        XCTAssertEqual(try Data(contentsOf: url), Data("good".utf8))
    }
}
