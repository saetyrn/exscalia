import Foundation
import XCTest

@testable import PaperCore

/// `LibraryItem` is qualified: XCTest brings in DeveloperToolsSupport, whose
/// `LibraryItem` would otherwise be ambiguous.
///
/// The library's three sorts and four filters over one fixed set, Favorites
/// and Recents. The set is built so every tie-break rule decides something.
final class LibraryArrangeTests: XCTestCase {
    /// Noon UTC on a day of September or October 2026.
    private static func day(_ month: Int, _ day: Int) -> Date {
        var components = DateComponents()
        components.year = 2026
        components.month = month
        components.day = day
        components.hour = 12
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "UTC")!
        return calendar.date(from: components)!
    }

    private static func folder(
        _ name: String, _ modified: Date, favorite: Bool = false, path: String? = nil
    ) -> PaperCore.LibraryItem {
        .folder(
            PaperFolder(
                id: PaperFolderID.legacy(path: path ?? "paper/\(name)"), name: name,
                favorite: favorite, created: modified, modified: modified,
                path: path ?? "paper/\(name)"))
    }

    private static func document(
        _ title: String, _ kind: DocumentKind, _ modified: Date, favorite: Bool = false,
        id: String? = nil
    ) -> PaperCore.LibraryItem {
        .document(
            Notebook(
                id: id ?? "\(kind.rawValue)-\(title)", title: title, created: modified,
                modified: modified, favorite: favorite, kind: kind,
                path: "paper/\(title).paper"))
    }

    /// Ties on purpose: Accounting/archive share a date, so do Journal/Sketch
    /// and Note 2/Note 10; two documents are both called Ideas.
    private var items: [PaperCore.LibraryItem] { Self.items }

    private static let items: [PaperCore.LibraryItem] = [
        folder("Accounting", day(9, 9)),
        folder("Work", day(10, 2), favorite: true),
        folder("archive", day(9, 9)),
        document("Ideas", .notebook, day(10, 1), favorite: true),
        document("Journal", .notebook, day(9, 25)),
        document("Note 10", .notebook, day(9, 20)),
        document("Note 2", .notebook, day(9, 20)),
        document("Board plan", .whiteboard, day(10, 3)),
        document("Sketch", .whiteboard, day(9, 25)),
        document("Ideas", .whiteboard, day(9, 30)),
    ]

    private func names(_ items: [PaperCore.LibraryItem]) -> [String] {
        items.map { item in
            switch item {
            case .folder(let folder): return "F:\(folder.name)"
            case .document(let notebook):
                return "\(notebook.kind == .notebook ? "N" : "W"):\(notebook.title)"
            }
        }
    }

    // MARK: - Sorts

    func testDateIsNewestFirstInterleavedWithTiesByName() {
        XCTAssertEqual(
            names(LibraryArrangement.arrange(items, sort: .date, filter: .all)),
            [
                "W:Board plan", "F:Work", "N:Ideas", "W:Ideas", "N:Journal", "W:Sketch",
                "N:Note 2", "N:Note 10", "F:Accounting", "F:archive",
            ])
    }

    func testNameIsLocalizedStandardOrderInterleavedWithTiesNewestFirst() {
        XCTAssertEqual(
            names(LibraryArrangement.arrange(items, sort: .name, filter: .all)),
            [
                "F:Accounting", "F:archive", "W:Board plan", "N:Ideas", "W:Ideas", "N:Journal",
                "N:Note 2", "N:Note 10", "W:Sketch", "F:Work",
            ])
    }

    func testTypeIsFoldersThenNotebooksThenWhiteboardsEachByName() {
        XCTAssertEqual(
            names(LibraryArrangement.arrange(items, sort: .type, filter: .all)),
            [
                "F:Accounting", "F:archive", "F:Work",
                "N:Ideas", "N:Journal", "N:Note 2", "N:Note 10",
                "W:Board plan", "W:Ideas", "W:Sketch",
            ])
    }

    func testAFullTieFallsBackToTheIDSoTheOrderNeverShuffles() {
        let when = Self.day(9, 1)
        let tied: [PaperCore.LibraryItem] = [
            Self.document("Same", .notebook, when, id: "b"),
            Self.folder("Same", when, path: "paper/Same"),
            Self.document("Same", .whiteboard, when, id: "a"),
        ]
        for sort in [LibrarySort.date, .name] {
            let once = LibraryArrangement.arrange(tied, sort: sort, filter: .all)
            XCTAssertEqual(once.map(\.id), ["document/a", "document/b", "folder/\(PaperFolderID.legacy(path: "paper/Same"))"])
            XCTAssertEqual(
                LibraryArrangement.arrange(tied.reversed(), sort: sort, filter: .all), once)
        }
        XCTAssertEqual(
            names(LibraryArrangement.arrange(tied, sort: .type, filter: .all)),
            ["F:Same", "N:Same", "W:Same"])
    }

    // MARK: - Filters

    func testFilters() {
        XCTAssertEqual(
            names(LibraryArrangement.arrange(items, sort: .date, filter: .folders)),
            ["F:Work", "F:Accounting", "F:archive"])
        XCTAssertEqual(
            names(LibraryArrangement.arrange(items, sort: .date, filter: .notebooks)),
            ["N:Ideas", "N:Journal", "N:Note 2", "N:Note 10"])
        XCTAssertEqual(
            names(LibraryArrangement.arrange(items, sort: .name, filter: .whiteboards)),
            ["W:Board plan", "W:Ideas", "W:Sketch"])
        XCTAssertEqual(LibraryArrangement.arrange(items, sort: .type, filter: .all).count, items.count)
        XCTAssertEqual(LibraryArrangement.arrange([], sort: .date, filter: .all), [])
    }

    // MARK: - Favorites and Recents

    func testFavoritesAreStarredItemsAtAnyDepthUnderTheCurrentSortAndFilter() {
        let folders = [
            PaperFolder(name: "Work", favorite: true, modified: Self.day(10, 2), path: "paper/Work"),
            PaperFolder(
                name: "Taxes", favorite: true, modified: Self.day(9, 1),
                path: "paper/Work/Accounting/Taxes"),
            PaperFolder(name: "Plain", modified: Self.day(10, 4), path: "paper/Plain"),
        ]
        let documents = items.compactMap { item -> Notebook? in
            if case .document(let notebook) = item { return notebook }
            return nil
        }
        XCTAssertEqual(
            names(
                LibraryArrangement.favorites(
                    folders: folders, documents: documents, sort: .date, filter: .all)),
            ["F:Work", "N:Ideas", "F:Taxes"])
        XCTAssertEqual(
            names(
                LibraryArrangement.favorites(
                    folders: folders, documents: documents, sort: .name, filter: .folders)),
            ["F:Taxes", "F:Work"])
        XCTAssertEqual(
            LibraryArrangement.favorites(
                folders: folders, documents: documents, sort: .date, filter: .whiteboards), [])
    }

    func testRecentsAreTheLastTwentyOpenedWhateverTheSort() {
        var documents: [Notebook] = []
        var opened: [String: Date] = [:]
        for index in 0..<30 {
            let kind: DocumentKind = index.isMultiple(of: 3) ? .whiteboard : .notebook
            // Modified runs the other way from opened, and names are random
            // in order, so neither can be what decides.
            let notebook = Notebook(
                id: String(format: "n%02d", index), title: "Doc \((index * 7) % 30)",
                modified: Self.day(9, 1).addingTimeInterval(Double(-index) * 3600), kind: kind,
                path: "paper/Doc \(index).paper")
            documents.append(notebook)
            if index < 25 { opened[notebook.id] = Self.day(10, 1).addingTimeInterval(Double(index) * 60) }
        }
        let recents = LibraryArrangement.recents(documents.shuffled(), opened: opened, filter: .all)
        XCTAssertEqual(recents.count, LibraryArrangement.recentsLimit)
        XCTAssertEqual(LibraryArrangement.recentsLimit, 20)
        XCTAssertEqual(
            recents.map(\.id), (5..<25).reversed().map { String(format: "document/n%02d", $0) })

        // The filter applies before the limit: every whiteboard opened.
        let boards = LibraryArrangement.recents(documents, opened: opened, filter: .whiteboards)
        XCTAssertEqual(
            boards.map(\.id),
            stride(from: 24, through: 0, by: -3).map { String(format: "document/n%02d", $0) })
        XCTAssertEqual(LibraryArrangement.recents(documents, opened: opened, filter: .folders), [])
        XCTAssertEqual(LibraryArrangement.recents(documents, opened: [:], filter: .all), [])
    }

    // MARK: - Items, keys and targets

    func testItemsKnowTheirOwnFacts() {
        let folder = Self.folder("Work", Self.day(10, 2), favorite: true)
        XCTAssertEqual(folder.id, "folder/\(PaperFolderID.legacy(path: "paper/Work"))")
        XCTAssertEqual(folder.name, "Work")
        XCTAssertTrue(folder.isFavorite)
        XCTAssertNil(folder.documentKind)
        let board = Self.document("Plan", .whiteboard, Self.day(10, 3), id: "abc")
        XCTAssertEqual(board.id, "document/abc")
        XCTAssertEqual(board.documentKind, .whiteboard)
        XCTAssertEqual(board.modified, Self.day(10, 3))
        XCTAssertEqual(Set(items.map(\.id)).count, items.count)
    }

    func testStorageKeysAndDefaults() {
        XCTAssertEqual(LibrarySort.storageKey, "paper.library.sort")
        XCTAssertEqual(LibraryFilter.storageKey, "paper.library.filter")
        XCTAssertEqual(LibraryLayout.storageKey, "paper.library.layout")
        XCTAssertEqual(LibrarySort.standard, .date)
        XCTAssertEqual(LibraryFilter.standard, .all)
        XCTAssertEqual(LibraryLayout.standard, .list)
        XCTAssertEqual(LibrarySort.allCases.map(\.rawValue), ["date", "name", "type"])
        XCTAssertEqual(
            LibraryFilter.allCases.map(\.rawValue), ["all", "folders", "notebooks", "whiteboards"])
    }

    func testWindowTargetsRoundTrip() throws {
        for target in [
            PaperWindowTarget.library(folderID: nil),
            .library(folderID: "3dfc9f48645d394cc5a07b793b247654"),
            .document(id: "n1", pageID: nil),
            .document(id: "n1", pageID: "p2"),
        ] {
            let data = try JSONEncoder().encode(target)
            XCTAssertEqual(try JSONDecoder().decode(PaperWindowTarget.self, from: data), target)
        }
    }

    func testTheDateLine() {
        let date = Self.day(9, 25).addingTimeInterval(21 * 60)  // 12:21 UTC
        let line = LibraryArrangement.dateLine(
            date, locale: Locale(identifier: "en_US"), timeZone: TimeZone(identifier: "UTC")!)
        // ICU puts a narrow no-break space before PM.
        let normalized = line.replacingOccurrences(of: "\u{202F}", with: " ")
        XCTAssertEqual(normalized, "Sep 25, 2026 at 12:21 PM")
    }
}
