import Foundation
import XCTest

@testable import PaperCore

final class SaveStateTests: XCTestCase {
    func testEveryCaseHasTitleAndSymbol() {
        let states: [SaveState] = [
            .savedLocally, .syncing(count: 3), .synced(at: Date()), .offline,
            .conflict(pageNumber: 7), .signedOut,
        ]
        for state in states {
            XCTAssertFalse(state.title.isEmpty)
            XCTAssertFalse(state.symbolName.isEmpty)
        }
        XCTAssertEqual(SaveState.syncing(count: 3).title, "Syncing 3")
        XCTAssertEqual(SaveState.conflict(pageNumber: 7).title, "Attention: conflict on page 7")
    }

    func testOnlyTroubleIsAnnounced() {
        XCTAssertTrue(SaveState.offline.deservesAnnouncement)
        XCTAssertTrue(SaveState.conflict(pageNumber: 1).deservesAnnouncement)
        XCTAssertTrue(SaveState.signedOut.deservesAnnouncement)
        XCTAssertFalse(SaveState.savedLocally.deservesAnnouncement)
        XCTAssertFalse(SaveState.syncing(count: 1).deservesAnnouncement)
    }

    func testRelativeWording() {
        let now = Date(timeIntervalSince1970: 10_000)
        XCTAssertEqual(SaveState.relative(since: now, now: now), "just now")
        XCTAssertEqual(SaveState.relative(since: now.addingTimeInterval(-30), now: now), "30s ago")
        XCTAssertEqual(SaveState.relative(since: now.addingTimeInterval(-600), now: now), "10m ago")
    }
}
