import Foundation
import XCTest

@testable import PaperCore

final class ServerSettingsTests: XCTestCase {
    func testNormalizesWhatPeopleType() throws {
        let settings = try XCTUnwrap(ServerSettings(urlString: "  notes.exscalia.art/  "))
        XCTAssertEqual(settings.originString, "https://notes.exscalia.art")
        XCTAssertFalse(settings.isPlaintext)
    }

    func testKeepsExplicitPortAndPlaintextScheme() throws {
        let settings = try XCTUnwrap(ServerSettings(urlString: "http://127.0.0.1:8600/api/"))
        XCTAssertEqual(settings.originString, "http://127.0.0.1:8600")
        XCTAssertTrue(settings.isPlaintext)
    }

    func testRejectsNonHTTPInput() {
        XCTAssertNil(ServerSettings(urlString: ""))
        XCTAssertNil(ServerSettings(urlString: "ftp://example.com"))
        XCTAssertNil(ServerSettings(urlString: "not a url at all"))
    }

    func testStoreKeyIsPerOriginAndStable() throws {
        let a = try XCTUnwrap(ServerSettings(urlString: "https://notes.exscalia.art"))
        let b = try XCTUnwrap(ServerSettings(urlString: "http://127.0.0.1:8600"))
        XCTAssertEqual(a.storeKey.count, 40)
        XCTAssertEqual(a.storeKey, try XCTUnwrap(ServerSettings(urlString: "NOTES.exscalia.art")).storeKey)
        XCTAssertNotEqual(a.storeKey, b.storeKey)
    }

    func testAPIPathJoining() throws {
        let settings = try XCTUnwrap(ServerSettings(urlString: "https://notes.exscalia.art"))
        XCTAssertEqual(
            settings.url(path: "/api/auth/status").absoluteString,
            "https://notes.exscalia.art/api/auth/status")
    }
}
