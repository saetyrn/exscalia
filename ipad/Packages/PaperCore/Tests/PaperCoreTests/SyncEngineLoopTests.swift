import Foundation
import XCTest

@testable import PaperCore

/// The run loop: it waits when there is nothing it can do, and a rejected
/// credential parks it only until the credential is good again.
final class SyncEngineLoopTests: XCTestCase {
    private var root: URL!
    private var store: PaperStore!
    private let clientID = "ipad-test-client"

    override func setUpWithError() throws {
        try super.setUpWithError()
        MockPaperServer.state.reset()
        root = FileManager.default.temporaryDirectory
            .appending(
                path: "SyncEngineLoopTests-\(UUID().uuidString)", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        store = try PaperStore(root: root)
    }

    override func tearDownWithError() throws {
        MockPaperServer.state.reset()
        store = nil
        if let root { try? FileManager.default.removeItem(at: root) }
        root = nil
        try super.tearDownWithError()
    }

    // MARK: - Fixtures

    private func makeEngine(clock: SyncClock) throws -> SyncEngine {
        let settings = try XCTUnwrap(ServerSettings(urlString: "https://paper.test"))
        let api = PaperAPI(
            settings: settings, auth: AnonymousAuthProvider(),
            session: MockPaperServer.makeSession())
        return SyncEngine(
            store: store, api: api, clientID: clientID, clock: clock, jitter: { $0 })
    }

    /// Three ops queued: notebookCreate, pageCreate, pagePut.
    private func seedLocalNotebook() throws {
        let notebook = try store.createNotebook(title: "Ideas", folder: "Work")
        let pageID = try XCTUnwrap(notebook.pages.first)
        try store.writeDrawing(Data("stroke".utf8), notebookPath: notebook.path, pageID: pageID)
        try store.journal.append(
            .init(kind: .pagePut, notebookPath: notebook.path, pageID: pageID))
    }

    /// A 401 on the first push latches the engine signed out.
    private func signedOutEngine(clock: SyncClock) async throws -> SyncEngine {
        try seedLocalNotebook()
        MockPaperServer.state.failStatus = 401
        MockPaperServer.state.failuresRemaining = 1
        let engine = try makeEngine(clock: clock)
        let sent = await engine.pushPending()
        XCTAssertEqual(sent, 0)
        let signedOut = await engine.isSignedOut
        XCTAssertTrue(signedOut)
        return engine
    }

    // MARK: - Waiting

    func testAKickWhileOfflineWakesTheLoopOnceRatherThanSpinning() async throws {
        try seedLocalNotebook()
        MockPaperServer.state.failuresRemaining = 1
        let engine = try makeEngine(clock: SystemSyncClock())
        await engine.pushPending()
        let failures = await engine.failureCount
        XCTAssertGreaterThan(failures, 0, "so every pass of the loop records a backoff step")
        await engine.setOnline(false)
        let requests = MockPaperServer.state.requests.count

        await engine.start()
        // Let the loop park in its one-second backoff wait before kicking it.
        try await Task.sleep(nanoseconds: 100_000_000)
        await engine.kick()
        try await Task.sleep(nanoseconds: 300_000_000)
        await engine.stop()

        let passes = await engine.backoffHistory.count
        XCTAssertLessThanOrEqual(passes, 3, "one pass at start and one per kick, not \(passes)")
        XCTAssertEqual(MockPaperServer.state.requests.count, requests)
    }

    // MARK: - Signed out

    func testCredentialsChangedLiftsTheLatchAndTheQueueDrains() async throws {
        let engine = try await signedOutEngine(clock: TestClock())
        let state = await engine.state
        XCTAssertEqual(state, .signedOut)
        let requests = MockPaperServer.state.requests.count
        await engine.pushPending()
        XCTAssertEqual(MockPaperServer.state.requests.count, requests, "latched: nothing goes out")

        await engine.credentialsChanged()
        let sent = await engine.pushPending()

        XCTAssertEqual(sent, 3)
        let signedOut = await engine.isSignedOut
        XCTAssertFalse(signedOut)
        XCTAssertEqual(try store.journal.pendingCount(), 0)
    }

    /// The web shell says this on every page load. An engine that was never
    /// signed out keeps its backoff against a failing server.
    func testCredentialsChangedLeavesAnEngineThatWasNotSignedOutAlone() async throws {
        try seedLocalNotebook()
        MockPaperServer.state.failuresRemaining = 2
        let engine = try makeEngine(clock: TestClock())
        await engine.pushPending()
        await engine.pushPending()
        let failures = await engine.failureCount
        XCTAssertEqual(failures, 2)

        await engine.credentialsChanged()

        let after = await engine.failureCount
        XCTAssertEqual(after, 2)
        let signedOut = await engine.isSignedOut
        XCTAssertFalse(signedOut)
    }

    func testTheSignedOutLoopProbesTheCredentialAndResumes() async throws {
        let clock = TestClock()
        let engine = try await signedOutEngine(clock: clock)

        await engine.start()
        var signedOut = true
        for _ in 0..<400 {
            signedOut = await engine.isSignedOut
            if !signedOut, (try? store.journal.pendingCount()) == 0 { break }
            try? await Task.sleep(nanoseconds: 5_000_000)
        }
        await engine.stop()

        XCTAssertFalse(signedOut)
        XCTAssertTrue(clock.sleeps.contains(SyncEngine.signedOutProbeInterval))
        XCTAssertFalse(MockPaperServer.state.paths(matching: "/api/auth/status").isEmpty)
        XCTAssertEqual(try store.journal.pendingCount(), 0)
    }

    func testAForegroundSyncProbesAndKeepsTheLatchUntilTheServerSaysYes() async throws {
        let engine = try await signedOutEngine(clock: TestClock())
        MockPaperServer.state.authRequired = true
        MockPaperServer.state.authenticated = false

        await engine.syncNow()
        var signedOut = await engine.isSignedOut
        XCTAssertTrue(signedOut)
        XCTAssertEqual(MockPaperServer.state.paths(matching: "/api/auth/status").count, 1)
        XCTAssertEqual(try store.journal.pendingCount(), 3)

        // Signed in on the web shell since.
        MockPaperServer.state.authenticated = true
        await engine.syncNow()
        signedOut = await engine.isSignedOut
        XCTAssertFalse(signedOut)
        XCTAssertEqual(try store.journal.pendingCount(), 0)
    }

    /// Signed out, the loop's only job is the slow probe; an autosave kick
    /// must not turn it into a request per stroke.
    func testKicksWhileSignedOutDoNotBecomeRequests() async throws {
        let engine = try await signedOutEngine(clock: SystemSyncClock())
        let requests = MockPaperServer.state.requests.count

        await engine.start()
        for _ in 0..<5 {
            await engine.kick()
            try await Task.sleep(nanoseconds: 20_000_000)
        }
        await engine.stop()

        XCTAssertEqual(MockPaperServer.state.requests.count, requests)
        let signedOut = await engine.isSignedOut
        XCTAssertTrue(signedOut)
    }
}
