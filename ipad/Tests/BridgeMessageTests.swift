import CoreGraphics
import XCTest

@testable import EXSCALIA

/// The bridge is the one place page content reaches the shell, so its
/// validation is tested rather than trusted.
final class BridgeMessageTests: XCTestCase {
    func testAcceptsTheOnlySupportedMessage() {
        let parsed = BridgeMessage.parse(
            ["type": "open", "section": "paper", "notebook": "Work/Ideas.paper", "page": "abc123"])
        XCTAssertEqual(
            parsed, .open(PaperTarget(notebook: "Work/Ideas.paper", page: "abc123")))
    }

    func testAcceptsABareOpen() {
        XCTAssertEqual(
            BridgeMessage.parse(["type": "open", "section": "paper"]),
            .open(PaperTarget(notebook: nil, page: nil)))
    }

    func testRejectsAnythingElse() {
        XCTAssertNil(BridgeMessage.parse("open paper"))
        XCTAssertNil(BridgeMessage.parse(["type": "eval", "section": "paper"]))
        XCTAssertNil(BridgeMessage.parse(["type": "open", "section": "settings"]))
        XCTAssertNil(BridgeMessage.parse(["type": "open"]))
    }

    func testDropsHostilePathFields() {
        let parsed = BridgeMessage.parse([
            "type": "open", "section": "paper", "notebook": "../../etc/passwd",
            "page": "/absolute",
        ])
        XCTAssertEqual(parsed, .open(PaperTarget(notebook: nil, page: nil)))
    }

    // MARK: - Deep links (M6)

    /// The web route names a notebook without the vault root; everything
    /// native is keyed with it. The translation happens once, here.
    func testTheBridgeTargetBecomesAVaultPath() {
        let target = PaperTarget(notebook: "Work/Ideas.paper", page: "page-1")
        XCTAssertEqual(target.deepLink.notebook, "paper/Work/Ideas.paper")
        XCTAssertEqual(target.deepLink.page, "page-1")

        // Already rooted, from a caller that speaks the store's spelling.
        XCTAssertEqual(
            PaperTarget(notebook: "paper/Work/Ideas.paper").deepLink.notebook,
            "paper/Work/Ideas.paper")
    }

    func testANotebookThatIsNotAPackageIsNotFollowed() {
        // Not a `.paper` package: the message is page data, and a path that
        // does not name a notebook is dropped rather than guessed at.
        XCTAssertNil(PaperTarget(notebook: "Work/Ideas").deepLink.notebook)
        XCTAssertNil(PaperTarget(notebook: "").deepLink.notebook)
        XCTAssertNil(PaperTarget().deepLink.notebook)
    }

    func testDropsOverlongFields() {
        let long = String(repeating: "a", count: BridgeMessage.maxFieldLength + 1)
        XCTAssertEqual(
            BridgeMessage.parse(["type": "open", "section": "paper", "notebook": long]),
            .open(PaperTarget(notebook: nil, page: nil)))
    }

    func testDurableLinksCarryNotebookIDAndRegion() {
        let parsed = BridgeMessage.parse([
            "type": "open", "section": "paper", "notebookID": "nb_123",
            "page": "page-1", "region": "12.5,20,80,40",
        ])
        guard case .open(let target) = parsed else {
            return XCTFail("expected a Paper open message")
        }
        XCTAssertEqual(target.notebookID, "nb_123")
        XCTAssertEqual(target.deepLink.notebookID, "nb_123")
        XCTAssertEqual(target.deepLink.page, "page-1")
        XCTAssertEqual(target.deepLink.box, CGRect(x: 12.5, y: 20, width: 80, height: 40))
    }

    func testDurableLinkRejectsUnsafeNotebookIDAndRegion() {
        let parsed = BridgeMessage.parse([
            "type": "open", "section": "paper", "notebookID": "../etc",
            "region": "not-a-box",
        ])
        XCTAssertEqual(parsed, .open(PaperTarget()))
    }

    // MARK: - Who may speak

    private let server = URL(string: "https://notes.exscalia.art")!

    /// `WKSecurityOrigin` spells the default port 0; the configured origin
    /// leaves it out. Both are the same server.
    func testTheServersOwnTopFrameIsTrusted() {
        XCTAssertTrue(
            BridgeMessageHandler.isTrustedFrame(
                isMainFrame: true, scheme: "https", host: "notes.exscalia.art", port: 0,
                expected: server))
        XCTAssertTrue(
            BridgeMessageHandler.isTrustedFrame(
                isMainFrame: true, scheme: "https", host: "notes.exscalia.art", port: 443,
                expected: server))
        XCTAssertTrue(
            BridgeMessageHandler.isTrustedFrame(
                isMainFrame: true, scheme: "http", host: "192.168.1.4", port: 8600,
                expected: URL(string: "http://192.168.1.4:8600")!))
    }

    func testASubframeIsNotTrustedEvenOnTheServersOrigin() {
        XCTAssertFalse(
            BridgeMessageHandler.isTrustedFrame(
                isMainFrame: false, scheme: "https", host: "notes.exscalia.art", port: 0,
                expected: server))
    }

    func testAnotherOriginIsNotTrusted() {
        XCTAssertFalse(
            BridgeMessageHandler.isTrustedFrame(
                isMainFrame: true, scheme: "https", host: "evil.example", port: 0,
                expected: server))
        XCTAssertFalse(
            BridgeMessageHandler.isTrustedFrame(
                isMainFrame: true, scheme: "https", host: "notes.exscalia.art", port: 8443,
                expected: server))
        XCTAssertFalse(
            BridgeMessageHandler.isTrustedFrame(
                isMainFrame: true, scheme: "http", host: "notes.exscalia.art", port: 0,
                expected: server))
    }

    // MARK: - What loads inside the shell

    func testOnlyTheServersOriginIsInternal() {
        XCTAssertTrue(
            WebShellController.isInternal(URL(string: "https://notes.exscalia.art/")!, origin: server))
        XCTAssertTrue(
            WebShellController.isInternal(
                URL(string: "HTTPS://NOTES.EXSCALIA.ART/x")!, origin: server))
        XCTAssertTrue(
            WebShellController.isInternal(
                URL(string: "https://notes.exscalia.art:443/paper")!, origin: server))

        XCTAssertFalse(
            WebShellController.isInternal(
                URL(string: "http://notes.exscalia.art:9999/")!, origin: server))
        XCTAssertFalse(
            WebShellController.isInternal(URL(string: "http://notes.exscalia.art/")!, origin: server))
        XCTAssertFalse(
            WebShellController.isInternal(URL(string: "file:///etc/passwd")!, origin: server))
        XCTAssertFalse(
            WebShellController.isInternal(URL(string: "https://evil.example/")!, origin: server))
    }

    // MARK: - Cookie as bearer

    private func sessionCookie(domain: String, secure: Bool) throws -> HTTPCookie {
        var properties: [HTTPCookiePropertyKey: Any] = [
            .name: CookieSessionAuth.cookieName, .value: "token", .domain: domain, .path: "/",
        ]
        if secure { properties[.secure] = "TRUE" }
        return try XCTUnwrap(HTTPCookie(properties: properties))
    }

    func testOnlyASecureSessionCookieForTheHostIsUsable() throws {
        XCTAssertTrue(
            CookieSessionAuth.isUsable(
                try sessionCookie(domain: "notes.exscalia.art", secure: true),
                host: "notes.exscalia.art"))
        XCTAssertFalse(
            CookieSessionAuth.isUsable(
                try sessionCookie(domain: "notes.exscalia.art", secure: false),
                host: "notes.exscalia.art"))
        XCTAssertFalse(
            CookieSessionAuth.isUsable(
                try sessionCookie(domain: "evil.example", secure: true),
                host: "notes.exscalia.art"))
        XCTAssertFalse(
            CookieSessionAuth.isUsable(
                try sessionCookie(domain: "notes.exscalia.art", secure: true),
                host: "evilnotes.exscalia.art"))

        let other = try XCTUnwrap(
            HTTPCookie(properties: [
                .name: "other", .value: "token", .domain: "notes.exscalia.art", .path: "/",
                .secure: "TRUE",
            ]))
        XCTAssertFalse(CookieSessionAuth.isUsable(other, host: "notes.exscalia.art"))
    }
}
