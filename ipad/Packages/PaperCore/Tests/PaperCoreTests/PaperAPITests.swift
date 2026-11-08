import Foundation
import XCTest

@testable import PaperCore

/// Records every request the client makes and replies with a canned response,
/// so the bearer header can be asserted without a server.
final class RecordingURLProtocol: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) private static var lock = NSLock()
    nonisolated(unsafe) private static var recorded: [URLRequest] = []
    nonisolated(unsafe) private static var status: Int = 200
    nonisolated(unsafe) private static var body: Data = Data("{}".utf8)

    static func reset(status: Int = 200, body: Data = Data("{}".utf8)) {
        lock.lock()
        defer { lock.unlock() }
        recorded = []
        self.status = status
        self.body = body
    }

    static var requests: [URLRequest] {
        lock.lock()
        defer { lock.unlock() }
        return recorded
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        Self.lock.lock()
        Self.recorded.append(request)
        let status = Self.status
        let body = Self.body
        Self.lock.unlock()

        guard let url = request.url,
            let response = HTTPURLResponse(
                url: url, statusCode: status, httpVersion: "HTTP/1.1",
                headerFields: ["Content-Type": "application/json"])
        else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

/// A provider with a fixed token that remembers being told the token is bad.
actor StubAuth: AuthProvider {
    private let token: String?
    private(set) var rejections = 0

    init(token: String?) { self.token = token }

    func bearerToken(for origin: URL) async -> String? { token }
    func tokenWasRejected() async { rejections += 1 }
}

/// Keeps every request a transport is handed, exactly as sent: a
/// `URLProtocol` sees the body only as a stream.
actor RequestLog {
    private(set) var requests: [URLRequest] = []
    var last: URLRequest? { requests.last }

    func append(_ request: URLRequest) { requests.append(request) }
}

final class PaperAPITests: XCTestCase {
    private func makeSession() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpShouldSetCookies = false
        configuration.protocolClasses = [RecordingURLProtocol.self]
        return URLSession(configuration: configuration)
    }

    private func settings() throws -> ServerSettings {
        try XCTUnwrap(ServerSettings(urlString: "https://notes.exscalia.art"))
    }

    func testBearerHeaderCarriesTheSessionToken() async throws {
        RecordingURLProtocol.reset(
            body: Data(#"{"auth_required": true, "authenticated": true}"#.utf8))
        let api = PaperAPI(
            settings: try settings(), auth: StubAuth(token: "tok-123"), session: makeSession())

        let status = try await api.authStatus()

        XCTAssertTrue(status.authenticated)
        XCTAssertTrue(status.authRequired)
        let requests = RecordingURLProtocol.requests
        XCTAssertEqual(requests.count, 1)
        let request = try XCTUnwrap(requests.first)
        XCTAssertEqual(
            request.url?.absoluteString, "https://notes.exscalia.art/api/auth/status")
        XCTAssertEqual(
            request.value(forHTTPHeaderField: "Authorization"), "Bearer tok-123")
    }

    func testNoTokenMeansNoAuthorizationHeader() async throws {
        RecordingURLProtocol.reset(
            body: Data(#"{"auth_required": true, "authenticated": false}"#.utf8))
        let api = PaperAPI(
            settings: try settings(), auth: StubAuth(token: nil), session: makeSession())

        let status = try await api.authStatus()

        XCTAssertFalse(status.authenticated)
        let request = try XCTUnwrap(RecordingURLProtocol.requests.first)
        XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"))
    }

    func testUnauthorizedTellsTheProviderAndThrows() async throws {
        RecordingURLProtocol.reset(status: 401, body: Data(#"{"detail":"no"}"#.utf8))
        let auth = StubAuth(token: "stale")
        let api = PaperAPI(settings: try settings(), auth: auth, session: makeSession())

        do {
            _ = try await api.authStatus()
            XCTFail("expected PaperAPIError.unauthorized")
        } catch let error as PaperAPIError {
            XCTAssertEqual(error, .unauthorized)
        }
        let rejections = await auth.rejections
        XCTAssertEqual(rejections, 1)
    }

    /// The server refuses a render over 8 MB with 413 for the whole request,
    /// and a 413 parks the op: the ink would never go up. The render is left
    /// out instead; the page and the ink still go.
    func testAnOversizedRenderIsLeftOutOfThePagePut() async throws {
        let log = RequestLog()
        let api = PaperAPI(
            settings: try settings(), auth: StubAuth(token: "tok-123"),
            transport: { request in
                await log.append(request)
                let response = try XCTUnwrap(
                    HTTPURLResponse(
                        url: try XCTUnwrap(request.url), statusCode: 200, httpVersion: "HTTP/1.1",
                        headerFields: ["Content-Type": "application/json"]))
                return (Data(#"{"page":{"id":"p1","revision":3},"revision":3}"#.utf8), response)
            })

        func sentParts(render: Data) async throws -> MultipartParser.Parsed {
            _ = try await api.putPage(
                notebook: "paper/A.paper", pageID: "p1", baseRevision: 2,
                pageJSON: Data(#"{"template":"ruled"}"#.utf8), drawing: Data("ink".utf8),
                render: render, text: nil, forRevision: 3, after: nil, clientID: "client",
                boundary: "BOUNDARY")
            let last = await log.last
            let request = try XCTUnwrap(last)
            return MultipartParser.parse(
                body: try XCTUnwrap(request.httpBody),
                contentType: try XCTUnwrap(request.value(forHTTPHeaderField: "Content-Type")))
        }

        let oversized = try await sentParts(render: Data(count: PageRenderer.maximumRenderBytes + 1))
        XCTAssertNil(oversized.files["render"])
        XCTAssertEqual(oversized.files["drawing"], Data("ink".utf8))
        XCTAssertEqual(oversized.files["page"], Data(#"{"template":"ruled"}"#.utf8))
        XCTAssertEqual(oversized.fields["base_revision"], "2")

        // A render exactly at the cap is one the server takes.
        let atCap = try await sentParts(render: Data(count: PageRenderer.maximumRenderBytes))
        XCTAssertEqual(atCap.files["render"]?.count, PageRenderer.maximumRenderBytes)
    }

    // MARK: - Selection action

    private static let selectionReply = Data(
        #"""
        {"action": "transcribe", "thread_id": "th 1", "navigate": "/omnia?t=th%201",
         "page": {"notebook_id": "nb", "page_id": "pg", "notebook": "paper/A.paper",
                  "url": "/paper/id/nb/pg"},
         "region": "1,2,3,4", "reply": "Read it.", "message": null,
         "confirmations": [{"id": "c1"}], "confirmation": {"id": "c1"}, "actions": [],
         "action_record": null, "pending_count": 2, "error": null}
        """#.utf8)

    private func selectionAPI(log: RequestLog, token: String? = "tok-123") throws -> PaperAPI {
        let body = Self.selectionReply
        return PaperAPI(settings: try settings(), auth: StubAuth(token: token)) { request in
            await log.append(request)
            let response = HTTPURLResponse(
                url: request.url!, statusCode: 200, httpVersion: "HTTP/1.1",
                headerFields: ["Content-Type": "application/json"])!
            return (body, response)
        }
    }

    private func sendSelection(_ api: PaperAPI, threadID: String?) async throws
        -> SelectionActionResponse
    {
        try await api.selectionAction(
            notebookID: "nb", pageID: "pg", action: "transcribe", region: "1,2,3,4",
            prompt: nil, threadID: threadID, image: Data("not-really-a-png".utf8))
    }

    func testSelectionActionUsesALongTimeoutAndTheBearerHeader() async throws {
        let log = RequestLog()
        let api = try selectionAPI(log: log)

        _ = try await sendSelection(api, threadID: "th 1")
        _ = try await sendSelection(api, threadID: nil)

        let requests = await log.requests
        XCTAssertEqual(requests.count, 2)
        let bodies = requests.map { String(decoding: $0.httpBody ?? Data(), as: UTF8.self) }
        for request in requests {
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(
                request.url?.absoluteString,
                "https://notes.exscalia.art/api/paper/selection-action")
            // The server answers only after the whole OMNIA turn.
            XCTAssertEqual(request.timeoutInterval, 300)
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer tok-123")
            XCTAssertTrue(
                request.value(forHTTPHeaderField: "Content-Type")?
                    .hasPrefix("multipart/form-data; boundary=") ?? false)
        }
        for body in bodies {
            XCTAssertTrue(body.contains(#"name="notebook_id""# + "\r\n\r\nnb\r\n"))
            XCTAssertTrue(body.contains(#"name="region""# + "\r\n\r\n1,2,3,4\r\n"))
            XCTAssertTrue(body.contains(#"name="image"; filename="image.png""#))
            XCTAssertTrue(body.contains("Content-Type: image/png"))
            XCTAssertTrue(body.contains("not-really-a-png"))
            // No prompt was given, so none is sent.
            XCTAssertFalse(body.contains(#"name="prompt""#))
        }
        XCTAssertTrue(bodies[0].contains(#"name="thread_id""# + "\r\n\r\nth 1\r\n"))
        XCTAssertFalse(bodies[1].contains(#"name="thread_id""#))
    }

    func testSelectionActionUnauthorizedTellsTheProvider() async throws {
        RecordingURLProtocol.reset(status: 401, body: Data(#"{"detail":"Not authenticated"}"#.utf8))
        let auth = StubAuth(token: "stale")
        let api = PaperAPI(settings: try settings(), auth: auth, session: makeSession())

        do {
            _ = try await sendSelection(api, threadID: nil)
            XCTFail("expected PaperAPIError.unauthorized")
        } catch let error as PaperAPIError {
            XCTAssertEqual(error, .unauthorized)
        }
        let rejections = await auth.rejections
        XCTAssertEqual(rejections, 1)
    }

    func testSelectionActionServerErrorCarriesTheDetail() async throws {
        let detail = Data(#"{"detail":"selection image is larger than 12 MB"}"#.utf8)
        RecordingURLProtocol.reset(status: 413, body: detail)
        let auth = StubAuth(token: "tok-123")
        let api = PaperAPI(settings: try settings(), auth: auth, session: makeSession())

        do {
            _ = try await sendSelection(api, threadID: nil)
            XCTFail("expected PaperAPIError.http")
        } catch let error as PaperAPIError {
            guard case .http(let status, let body) = error else {
                return XCTFail("expected .http, got \(error)")
            }
            XCTAssertEqual(status, 413)
            XCTAssertEqual(
                PaperAPI.detailMessage(in: body), "selection image is larger than 12 MB")
        }
        let rejections = await auth.rejections
        XCTAssertEqual(rejections, 0)
    }

    func testSelectionActionDecodesNavigateAndPendingCount() async throws {
        let api = try selectionAPI(log: RequestLog())

        let response = try await sendSelection(api, threadID: nil)

        XCTAssertEqual(response.threadID, "th 1")
        XCTAssertEqual(response.navigate, "/omnia?t=th%201")
        XCTAssertEqual(response.reply, "Read it.")
        XCTAssertNil(response.error)
        XCTAssertEqual(response.pendingCount, 2)
    }
}
