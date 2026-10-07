import Foundation
import XCTest

@testable import PaperCore

/// `folder.json` and the rules every side derives alike: legacy ids, folder
/// slugs, icons.
final class FolderModelTests: XCTestCase {
    private func folder(_ json: String) throws -> PaperFolder {
        try PaperJSON.decode(PaperFolder.self, from: Data(json.utf8))
    }

    private func object(_ data: Data) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    // MARK: - Record

    func testTheContractRecordDecodes() throws {
        let decoded = try folder(
            #"{"id":"3dfc9f48645d394cc5a07b793b247654","name":"Accounting","color":"red","icon":"emoji:📒","favorite":true,"created":"2026-09-09T11:20:00.000000Z","modified":"2026-09-09T11:20:00.000000Z","seq":812}"#
        )
        XCTAssertEqual(decoded.id, "3dfc9f48645d394cc5a07b793b247654")
        XCTAssertEqual(decoded.name, "Accounting")
        XCTAssertEqual(decoded.color, .red)
        XCTAssertEqual(decoded.icon, .emoji("📒"))
        XCTAssertTrue(decoded.favorite)
        // The server's six fractional digits, which `PaperJSON` alone refuses.
        XCTAssertEqual(
            decoded.created, PaperDate.parse("2026-09-09T11:20:00.000Z"))
        XCTAssertEqual(decoded.modified, decoded.created)
        XCTAssertEqual(decoded.seq, 812)
        XCTAssertTrue(decoded.additionalFields.isEmpty)
    }

    func testUnknownKeysSurviveARoundTripAndTheWireKeysDoNot() throws {
        let decoded = try folder(
            #"{"id":"9e7201dacb87ef0d43fb22798ccf4af6","name":"Work","color":"teal","icon":null,"favorite":false,"created":"2026-09-09T11:20:00.000Z","modified":"2026-09-10T11:20:00.000Z","seq":3,"path":"paper/Work","parent":"paper","sharedWith":["a","b"],"layout":{"view":"grid"}}"#
        )
        XCTAssertEqual(decoded.path, "paper/Work", "a wire record says where it is")
        XCTAssertEqual(decoded.parentPath, "paper")
        XCTAssertEqual(Set(decoded.additionalFields.keys), ["sharedWith", "layout"])

        let encoded = try object(PaperJSON.encode(decoded))
        XCTAssertNil(encoded["path"], "the file's own location is the truth")
        XCTAssertNil(encoded["parent"])
        XCTAssertEqual(encoded["sharedWith"] as? [String], ["a", "b"])
        XCTAssertEqual((encoded["layout"] as? [String: Any])?["view"] as? String, "grid")
        XCTAssertTrue(encoded.keys.contains("icon"), "no icon is written as null")
        XCTAssertTrue(encoded["icon"] is NSNull)

        var again = try PaperJSON.decode(PaperFolder.self, from: PaperJSON.encode(decoded))
        XCTAssertEqual(again.path, "")
        again.path = decoded.path
        XCTAssertEqual(again, decoded)
    }

    func testEveryFieldButTheIDIsOptionalAndUnknownValuesFallBack() throws {
        let sparse = try folder(#"{"id":"9e7201dacb87ef0d43fb22798ccf4af6"}"#)
        XCTAssertEqual(sparse.name, "")
        XCTAssertEqual(sparse.color, .blue)
        XCTAssertNil(sparse.icon)
        XCTAssertFalse(sparse.favorite)
        XCTAssertEqual(sparse.seq, 0)

        let odd = try folder(
            #"{"id":"9e7201dacb87ef0d43fb22798ccf4af6","color":"chartreuse","icon":"glyph:star","favorite":"yes"}"#
        )
        XCTAssertEqual(odd.color, .blue)
        XCTAssertNil(odd.icon)
        XCTAssertFalse(odd.favorite)
        XCTAssertEqual(try folder(#"{"id":"x","color":"Purple"}"#).color, .purple)

        XCTAssertThrowsError(try folder(#"{"name":"No id"}"#))
    }

    func testColoursAreTheContractsTwelveInOrder() {
        XCTAssertEqual(
            FolderColor.allCases.map(\.rawValue),
            [
                "red", "orange", "yellow", "green", "mint", "teal", "blue", "indigo", "purple",
                "pink", "brown", "gray",
            ])
        XCTAssertEqual(FolderColor.indigo.displayName, "Indigo")
    }

    func testParentPaths() {
        XCTAssertEqual(PaperFolder(name: "Work", path: "paper/Work").parentPath, "paper")
        XCTAssertEqual(
            PaperFolder(name: "Accounting", path: "paper/Work/Accounting").parentPath, "paper/Work")
        XCTAssertEqual(PaperFolder(name: "", path: "paper").parentPath, "")
    }

    // MARK: - Icons

    func testSymbolIcons() {
        XCTAssertEqual(FolderIcon(wire: "symbol:folder"), .symbol("folder"))
        XCTAssertEqual(FolderIcon(wire: "symbol:chart.bar"), .symbol("chart.bar"))
        XCTAssertEqual(FolderIcon(wire: "symbol:books.vertical"), .symbol("books.vertical"))
        XCTAssertEqual(FolderIcon(wire: "symbol:a1.b2.c3"), .symbol("a1.b2.c3"))
        XCTAssertNil(FolderIcon(wire: "symbol:"))
        XCTAssertNil(FolderIcon(wire: "symbol:Folder"), "lowercase only")
        XCTAssertNil(FolderIcon(wire: "symbol:chart..bar"))
        XCTAssertNil(FolderIcon(wire: "symbol:.folder"))
        XCTAssertNil(FolderIcon(wire: "symbol:folder."))
        XCTAssertNil(FolderIcon(wire: "symbol:folder fill"))
        XCTAssertNil(FolderIcon(wire: "symbol:" + String(repeating: "a", count: 65)))
        XCTAssertNotNil(FolderIcon(wire: "symbol:" + String(repeating: "a", count: 64)))
        XCTAssertEqual(FolderIcon.symbol("star").wire, "symbol:star")
        XCTAssertEqual(FolderIcon.symbolChoices.count, 24)
        XCTAssertTrue(FolderIcon.symbolChoices.allSatisfy(FolderIcon.isValidSymbol))
    }

    func testEmojiIcons() {
        XCTAssertEqual(FolderIcon(wire: "emoji:📒"), .emoji("📒"))
        // A ZWJ sequence is one grapheme of several code points.
        let family = "👩\u{200D}👩\u{200D}👧"
        XCTAssertEqual(FolderIcon(wire: "emoji:" + family), .emoji(family))
        XCTAssertEqual(FolderIcon(wire: "emoji:❤️"), .emoji("❤️"), "with its variation selector")
        XCTAssertNil(FolderIcon(wire: "emoji:"))
        XCTAssertNil(FolderIcon(wire: "emoji:a"), "no ASCII")
        XCTAssertNil(FolderIcon(wire: "emoji:📒 "), "no whitespace")
        XCTAssertNil(FolderIcon(wire: "emoji:📒\u{00A0}"), "not even non-breaking")
        XCTAssertNil(FolderIcon(wire: "emoji:📒\u{0085}"), "no controls")
        XCTAssertNil(FolderIcon(wire: "emoji:" + String(repeating: "📒", count: 17)))
        XCTAssertNotNil(FolderIcon(wire: "emoji:" + String(repeating: "📒", count: 16)))
        XCTAssertNil(FolderIcon(wire: "📒"), "the prefix is required")
        XCTAssertEqual(FolderIcon.emoji("📒").wire, "emoji:📒")
    }

    // MARK: - Ids

    func testLegacyIDsMatchTheContractVectors() {
        XCTAssertEqual(PaperFolderID.legacy(path: "paper/Work"), "9e7201dacb87ef0d43fb22798ccf4af6")
        XCTAssertEqual(
            PaperFolderID.legacy(path: "paper/Work/Accounting"), "3dfc9f48645d394cc5a07b793b247654")
    }

    func testLegacyIDsHashTheNFCSpelling() {
        let composed = "paper/Caf\u{00E9}"
        let decomposed = "paper/Cafe\u{0301}"
        XCTAssertEqual(PaperFolderID.legacy(path: decomposed), PaperFolderID.legacy(path: composed))
        XCTAssertNotEqual(PaperFolderID.legacy(path: "paper/work"), PaperFolderID.legacy(path: "paper/Work"))
    }

    func testIDShapes() {
        let made = PaperFolderID.make()
        XCTAssertTrue(PaperFolderID.isValid(made))
        XCTAssertNotEqual(made, PaperFolderID.make())
        XCTAssertTrue(PaperFolderID.isValid(PaperFolderID.legacy(path: "paper/Work")))
        XCTAssertFalse(PaperFolderID.isValid("3DFC9F48645D394CC5A07B793B247654"))
        XCTAssertFalse(PaperFolderID.isValid("3dfc9f48645d394cc5a07b793b24765"))
        XCTAssertFalse(PaperFolderID.isValid("3dfc9f48645d394cc5a07b793b24765g"))
    }

    func testALegacyRecordIsTheDirectoryAsItStands() {
        let now = Date(timeIntervalSince1970: 1_790_000_000)
        let record = PaperFolder.legacy(path: "paper/Work/Accounting", now: now)
        XCTAssertEqual(record.id, "3dfc9f48645d394cc5a07b793b247654")
        XCTAssertEqual(record.name, "Accounting")
        XCTAssertEqual(record.color, .blue)
        XCTAssertNil(record.icon)
        XCTAssertFalse(record.favorite)
        XCTAssertEqual(record.created, now)
        XCTAssertEqual(record.modified, now)
        XCTAssertEqual(record.seq, 0)
        XCTAssertEqual(record.path, "paper/Work/Accounting")
    }

    // MARK: - Slugs

    func testFolderSlugs() {
        let vectors: [(String, String)] = [
            ("Accounting", "Accounting"),
            ("Notes.paper", "Notes.paper_"),
            ("Notes.PAPER", "Notes.PAPER_"),
            ("folder.json", "folder.json_"),
            ("Folder.JSON", "Folder.JSON_"),
            ("folder.json.bak", "folder.json.bak"),
            ("paper", "paper"),
            ("CON", "_CON"),
            ("Work/Plans", "Work Plans"),
            ("📒 Ledger", "📒 Ledger"),
            ("  ", "Untitled"),
            ("Trailing.", "Trailing"),
        ]
        for (name, slug) in vectors {
            XCTAssertEqual(PaperFolderID.folderSlug(name), slug, name)
        }
    }

    /// One folder-slug spec with the server: the vector file B1 adds under
    /// `backend/tests/fixtures` is checked by pytest and by this test.
    func testFolderSlugMatchesTheSharedVectors() throws {
        var repo = URL(fileURLWithPath: #filePath)
        for _ in 0..<6 { repo.deleteLastPathComponent() }
        let url = repo.appending(path: "backend/tests/fixtures/folder_slug_vectors.json")
        guard let data = try? Data(contentsOf: url) else {
            throw XCTSkip("no folder slug vectors at \(url.path(percentEncoded: false))")
        }
        struct Vector: Decodable {
            let name: String
            let slug: String
        }
        let vectors = try JSONDecoder().decode([Vector].self, from: data)
        XCTAssertGreaterThanOrEqual(vectors.count, 10)
        for vector in vectors {
            let slug = PaperFolderID.folderSlug(vector.name)
            XCTAssertEqual(
                slug.unicodeScalars.map(\.value), vector.slug.unicodeScalars.map(\.value),
                "folderSlug(\(vector.name.debugDescription)) = \(slug.debugDescription)")
        }
    }
}
