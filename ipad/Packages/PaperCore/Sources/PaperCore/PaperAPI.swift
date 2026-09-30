import Foundation

/// Errors the client surfaces rather than swallowing.
public enum PaperAPIError: Error, Equatable, Sendable {
    /// The server rejected the credential; the caller moves to `.signedOut`.
    case unauthorized
    /// Any other non-2xx response.
    case http(status: Int, body: String)
    /// A 2xx body that did not decode.
    case malformedResponse

    /// 4xx other than 401/403/409/429: the request itself is wrong, so retrying
    /// it unchanged will fail forever. The sync engine parks such an op instead
    /// of spinning on it.
    public var isPermanentClientError: Bool {
        guard case .http(let status, _) = self else { return false }
        return (400..<500).contains(status) && status != 409 && status != 429
    }

    public var status: Int? {
        guard case .http(let status, _) = self else { return nil }
        return status
    }

    /// What the pill shows for a parked op.
    public var summary: String {
        switch self {
        case .unauthorized: return "the server rejected this device"
        case .malformedResponse: return "the server sent an answer we could not read"
        case .http(let status, let body):
            let detail = PaperAPI.detailMessage(in: body)
            return detail.map { "\(status): \($0)" } ?? "server error \(status)"
        }
    }
}

/// `GET /api/auth/status`.
public struct AuthStatus: Codable, Equatable, Sendable {
    public let authRequired: Bool
    public let authenticated: Bool

    enum CodingKeys: String, CodingKey {
        case authRequired = "auth_required"
        case authenticated
    }

    public init(authRequired: Bool, authenticated: Bool) {
        self.authRequired = authRequired
        self.authenticated = authenticated
    }

    /// True when this device may write. A server running with authentication
    /// off (the LAN development case, where `__Host-` cookies cannot exist)
    /// says `auth_required: false` and is perfectly usable.
    public var canSync: Bool { !authRequired || authenticated }
}

/// What `POST /api/paper/selection-action` answers once its OMNIA turn has
/// run. The rest of the server's answer (the page, the confirmations
/// themselves) belongs to the web thread view, which is where the user goes.
public struct SelectionActionResponse: Decodable, Equatable, Sendable {
    public let threadID: String
    /// Where the web shell shows the thread.
    public let navigate: String?
    public let reply: String
    public let error: String?
    /// Confirmations waiting across the app, including any this turn staged.
    public let pendingCount: Int?

    enum CodingKeys: String, CodingKey {
        case threadID = "thread_id"
        case navigate, reply, error
        case pendingCount = "pending_count"
    }
}

/// Thin client over the OMNIA HTTP API.
///
/// Auth is bearer-only on purpose: the session cookie is read once from the web
/// shell and replayed as `Authorization: Bearer`, and the session used here has
/// `httpShouldSetCookies = false` so nothing the API returns can quietly change
/// the web shell's login state.
public actor PaperAPI {
    /// Anything that can run a request. `URLSession` conforms below; tests
    /// inject a `URLProtocol`-backed session through the same door.
    public typealias Transport = @Sendable (URLRequest) async throws -> (Data, URLResponse)

    private let settings: ServerSettings
    private let auth: AuthProvider
    private let transport: Transport
    private let userAgentSuffix: String

    public init(
        settings: ServerSettings,
        auth: AuthProvider,
        session: URLSession,
        userAgentSuffix: String = "EXSCALIA-iPad"
    ) {
        self.init(
            settings: settings,
            auth: auth,
            userAgentSuffix: userAgentSuffix,
            transport: { request in try await session.data(for: request) }
        )
    }

    public init(
        settings: ServerSettings,
        auth: AuthProvider,
        userAgentSuffix: String = "EXSCALIA-iPad",
        transport: @escaping Transport
    ) {
        self.settings = settings
        self.auth = auth
        self.userAgentSuffix = userAgentSuffix
        self.transport = transport
    }

    public nonisolated var origin: URL { settings.origin }

    /// A session shaped the way every native request wants: no cookie storage
    /// of its own, no cache, so the bearer header is the only credential.
    public static func makeSession() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpShouldSetCookies = false
        configuration.httpCookieAcceptPolicy = .never
        configuration.httpCookieStorage = nil
        configuration.urlCache = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.waitsForConnectivity = false
        return URLSession(configuration: configuration)
    }

    // MARK: - URLs

    /// Percent-encodes one path segment. Slashes are escaped, so a notebook
    /// title containing one can never become an extra path component.
    public static func encode(segment: String) -> String {
        var allowed = CharacterSet.urlPathAllowed
        allowed.remove(charactersIn: "/?#")
        return segment.addingPercentEncoding(withAllowedCharacters: allowed) ?? segment
    }

    /// Encodes a whole path one segment at a time, so the separators survive
    /// and the content does not.
    public static func encode(path: String) -> String {
        path.split(separator: "/").map { encode(segment: String($0)) }.joined(separator: "/")
    }

    /// Builds an absolute URL from an already-encoded path.
    nonisolated func url(encodedPath: String, query: [URLQueryItem] = []) -> URL {
        var components = URLComponents(url: settings.origin, resolvingAgainstBaseURL: false)
        components?.percentEncodedPath =
            encodedPath.hasPrefix("/") ? encodedPath : "/" + encodedPath
        if !query.isEmpty { components?.queryItems = query }
        return components?.url ?? settings.url(path: encodedPath)
    }

    /// Builds the request for `path`, attaching the bearer token when the
    /// provider has one. Exposed so tests can assert the header without a
    /// round trip.
    public func makeRequest(method: String = "GET", path: String) async -> URLRequest {
        await makeRequest(method: method, url: url(encodedPath: Self.encode(path: path)))
    }

    func makeRequest(method: String, url: URL) async -> URLRequest {
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue(userAgentSuffix, forHTTPHeaderField: "X-EXSCALIA-Client")
        if let token = await auth.bearerToken(for: settings.origin) {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        return request
    }

    /// The `/api/live` request, handed to `LiveEventStream`. Built here so the
    /// bearer rule lives in exactly one place.
    public func liveRequest() async -> URLRequest {
        var request = await makeRequest(method: "GET", url: url(encodedPath: "/api/live"))
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        request.timeoutInterval = 3600
        return request
    }

    // MARK: - Auth

    /// Whether this device's credential is good.
    public func authStatus() async throws -> AuthStatus {
        try await get(AuthStatus.self, path: "/api/auth/status")
    }

    // MARK: - Library and feed

    public func library() async throws -> PaperLibrary {
        try await get(PaperLibrary.self, path: "/api/paper")
    }

    /// Everything that moved after `since`. `epoch` is the cursor's epoch; a
    /// mismatch makes the server answer `full`.
    public func changes(since: Int, epoch: String?) async throws -> ChangesFeed {
        var query = [URLQueryItem(name: "since", value: String(max(0, since)))]
        if let epoch, !epoch.isEmpty { query.append(URLQueryItem(name: "epoch", value: epoch)) }
        let request = await makeRequest(
            method: "GET", url: url(encodedPath: "/api/paper/changes", query: query))
        return try decode(ChangesFeed.self, from: try await send(request))
    }

    // MARK: - Notebooks

    public func notebook(path: String) async throws -> NotebookManifest {
        try await get(NotebookManifest.self, path: "/api/paper/notebooks/\(path)")
    }

    /// `nil` when the server has never heard of this notebook, which is how the
    /// pusher knows a create still has work to do.
    public func notebookIfPresent(path: String) async throws -> NotebookManifest? {
        do {
            return try await notebook(path: path)
        } catch let error as PaperAPIError where error.status == 404 {
            return nil
        }
    }

    public func createNotebook(
        title: String, folder: String, template: Template, size: PaperSize, id: String?,
        clientID: String
    ) async throws -> NotebookManifest {
        var body: [String: JSONValue] = [
            "title": .string(title),
            "folder": .string(folder),
            "template": .string(template.rawValue),
            "paperSize": .string(size.rawValue),
            "client_id": .string(clientID),
        ]
        if let id { body["id"] = .string(id) }
        return try await postJSON(NotebookManifest.self, path: "/api/paper/notebooks", body: body)
    }

    /// Rename and move are the same operation: the destination carries its own
    /// folder. The path is only the slug, so a retitle sends the verbatim
    /// `title` too; a move sends none and the server keeps the one it has.
    public func renameNotebook(
        path: String, to newPath: String, title: String? = nil, clientID: String
    ) async throws -> NotebookManifest {
        var body: [String: JSONValue] = [
            "new_path": .string(newPath), "client_id": .string(clientID),
        ]
        if let title { body["title"] = .string(title) }
        return try await postJSON(
            NotebookManifest.self, path: "/api/paper/notebooks/\(path)/rename", body: body)
    }

    public func patchNotebook(path: String, patch: [String: JSONValue], clientID: String)
        async throws -> NotebookManifest
    {
        var body = patch
        body["client_id"] = .string(clientID)
        let request = await jsonRequest(
            method: "PATCH", path: "/api/paper/notebooks/\(path)", body: body)
        return try decode(NotebookManifest.self, from: try await send(request))
    }

    /// A notebook the server no longer has is a delete that already happened,
    /// so 404 counts as success.
    public func deleteNotebook(path: String) async throws {
        try await deleteIdempotent(path: "/api/paper/notebooks/\(path)")
    }

    // MARK: - Pages

    public func page(notebook: String, pageID: String) async throws -> RemotePageResponse {
        let request = await makeRequest(
            method: "GET", path: "/api/paper/pages/\(notebook)/\(pageID)")
        let data = try await send(request)
        return RemotePageResponse(page: try decode(RemotePage.self, from: data), raw: data)
    }

    public func createPage(
        notebook: String, pageID: String, after: String?, template: Template, size: PaperSize,
        clientID: String, customSize: SizePoints? = nil
    ) async throws -> RemotePage {
        let wireSize: JSONValue = customSize.map {
            .object(["width": .double($0.width), "height": .double($0.height)])
        } ?? .string(size.rawValue)
        var body: [String: JSONValue] = [
            "id": .string(pageID),
            "template": .string(template.rawValue),
            "size": wireSize,
            // Always, not only beside a bare name: dimensions alone cannot
            // say that a page is infinite.
            "size_name": .string(size.rawValue),
            "client_id": .string(clientID),
        ]
        if let after { body["after"] = .string(after) }
        return try await postJSON(
            RemotePage.self, path: "/api/paper/notebooks/\(notebook)/pages", body: body)
    }

    public func deletePage(notebook: String, pageID: String) async throws {
        try await deleteIdempotent(path: "/api/paper/pages/\(notebook)/\(pageID)")
    }

    /// The one write path for ink.
    ///
    /// `baseRevision` is the revision the local content was made from; 0 means
    /// "create". `forRevision` stamps the derived parts, so a render made for
    /// an older revision is accepted and flagged rather than dropped.
    public func putPage(
        notebook: String,
        pageID: String,
        baseRevision: Int,
        pageJSON: Data?,
        drawing: Data?,
        render: Data?,
        text: Data?,
        forRevision: Int?,
        after: String?,
        clientID: String,
        boundary: String? = nil
    ) async throws -> PagePutOutcome {
        // The server answers a render over its cap with 413 for the whole
        // request, and a 413 parks the op, so an oversized picture would keep
        // the ink from ever going up. The ink goes now; the picture follows in
        // a derived push once it has been drawn again within the budget.
        let render = render.flatMap { $0.count <= PageRenderer.maximumRenderBytes ? $0 : nil }
        var body = boundary.map { MultipartBody(boundary: $0) } ?? MultipartBody()
        body.addField("base_revision", String(baseRevision))
        if let forRevision { body.addField("for_revision", String(forRevision)) }
        body.addField("client_id", clientID)
        body.addField("after", after)
        body.addPart(pageJSON.map { MultipartBody.Part.json(name: "page", data: $0) })
        body.addPart(
            drawing.map {
                MultipartBody.Part.binary(name: "drawing", filename: "drawing.pkdrawing", data: $0)
            })
        body.addPart(render.map { MultipartBody.Part.png(name: "render", data: $0) })
        body.addPart(text.map { MultipartBody.Part.json(name: "text", data: $0) })

        var request = await makeRequest(
            method: "PUT", path: "/api/paper/pages/\(notebook)/\(pageID)")
        request.setValue(body.contentType, forHTTPHeaderField: "Content-Type")
        request.httpBody = body.encoded()

        let (data, response) = try await perform(request)
        if response.statusCode == 409 {
            return .conflict(try decode(PageConflict.self, from: data))
        }
        try check(response, data: data)
        return .saved(try decode(PageSaveResult.self, from: data))
    }

    // MARK: - Blobs

    /// Bytes of `drawing.pkdrawing`, `render.png` or `text.json`.
    ///
    /// `revision` makes the URL immutable-cacheable on the server side; the
    /// ETag makes a re-fetch of unchanged bytes free.
    public func blob(
        notebook: String, pageID: String, name: String, revision: Int?, etag: String? = nil
    ) async throws -> BlobResponse {
        let query = revision.map { [URLQueryItem(name: "r", value: String($0))] } ?? []
        var request = await makeRequest(
            method: "GET",
            url: url(
                encodedPath: "/api/paper/blob/"
                    + Self.encode(path: "\(notebook)/\(pageID)/\(name)"),
                query: query))
        if let etag { request.setValue(etag, forHTTPHeaderField: "If-None-Match") }
        let (data, response) = try await perform(request)
        if response.statusCode == 304 {
            return BlobResponse(etag: response.value(forHTTPHeaderField: "ETag"), notModified: true)
        }
        if response.statusCode == 404 { return BlobResponse(missing: true) }
        try check(response, data: data)
        return BlobResponse(data: data, etag: response.value(forHTTPHeaderField: "ETag"))
    }

    // MARK: - Trash

    public func trash() async throws -> PaperTrashList {
        try await get(PaperTrashList.self, path: "/api/paper/trash")
    }

    @discardableResult
    public func restoreFromTrash(id: String) async throws -> Data {
        let request = await makeRequest(method: "POST", path: "/api/paper/trash/\(id)/restore")
        return try await send(request)
    }

    // MARK: - History

    public func history(notebook: String, pageID: String, limit: Int = 50) async throws
        -> PageHistory
    {
        let request = await makeRequest(
            method: "GET",
            url: url(
                encodedPath: "/api/paper/history/" + Self.encode(path: "\(notebook)/\(pageID)"),
                query: [URLQueryItem(name: "limit", value: String(limit))]))
        return try decode(PageHistory.self, from: try await send(request))
    }

    @discardableResult
    public func restoreVersion(notebook: String, pageID: String, sha: String, clientID: String)
        async throws -> PageSaveResult
    {
        try await postJSON(
            PageSaveResult.self, path: "/api/paper/history/\(notebook)/\(pageID)/restore",
            body: ["sha": .string(sha), "client_id": .string(clientID)])
    }

    // MARK: - Assets

    /// Content-hash idempotent: the same bytes always land on the same path.
    /// The server names the file `safe_stem(filename)-<sha256[:16]>.<ext>`,
    /// so `filename` is the name before hashing, never a stored name.
    public func uploadAsset(
        notebook: String, filename: String, data: Data,
        contentType: String = "application/octet-stream"
    ) async throws -> AssetReference {
        var body = MultipartBody()
        body.addPart(
            MultipartBody.Part(
                name: "file", filename: filename, contentType: contentType, data: data))
        var request = await makeRequest(method: "POST", path: "/api/paper/assets/\(notebook)")
        request.setValue(body.contentType, forHTTPHeaderField: "Content-Type")
        request.httpBody = body.encoded()
        return try decode(AssetReference.self, from: try await send(request))
    }

    /// The bytes of `<notebook>/assets/<filename>`, or `nil` when the server
    /// has no file by that name.
    public func asset(notebook: String, filename: String) async throws -> Data? {
        let request = await makeRequest(
            method: "GET", path: "/api/paper/assets/\(notebook)/\(filename)")
        let (data, response) = try await perform(request)
        if response.statusCode == 404 { return nil }
        try check(response, data: data)
        return data
    }

    // MARK: - OMNIA

    /// One OMNIA turn over a lasso crop. The image rides in the body only; the
    /// server never writes it to the vault. `threadID` sends the turn into a
    /// thread an earlier action opened rather than a new one.
    public func selectionAction(
        notebookID: String,
        pageID: String,
        action: String,
        region: String?,
        prompt: String?,
        threadID: String?,
        image: Data
    ) async throws -> SelectionActionResponse {
        var body = MultipartBody()
        body.addField("notebook_id", notebookID)
        body.addField("page_id", pageID)
        body.addField("action", action)
        body.addField("region", region)
        body.addField("prompt", prompt)
        body.addField("thread_id", threadID)
        body.addPart(.png(name: "image", data: image))

        var request = await makeRequest(method: "POST", path: "/api/paper/selection-action")
        request.setValue(body.contentType, forHTTPHeaderField: "Content-Type")
        request.httpBody = body.encoded()
        // The server answers only after the whole OMNIA turn -- vision, tool
        // rounds, staged confirmations -- so the session's 60 s would call a
        // turn that is still succeeding a timeout.
        request.timeoutInterval = 300
        return try decode(SelectionActionResponse.self, from: try await send(request))
    }

    // MARK: - Plumbing

    private func get<T: Decodable>(_ type: T.Type, path: String) async throws -> T {
        let request = await makeRequest(method: "GET", path: path)
        return try decode(type, from: try await send(request))
    }

    private func postJSON<T: Decodable>(_ type: T.Type, path: String, body: [String: JSONValue])
        async throws -> T
    {
        let request = await jsonRequest(method: "POST", path: path, body: body)
        return try decode(type, from: try await send(request))
    }

    private func jsonRequest(method: String, path: String, body: [String: JSONValue]) async
        -> URLRequest
    {
        var request = await makeRequest(method: method, path: path)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = JSONValue.object(body).encoded()
        return request
    }

    /// DELETE where a missing thing means the job is already done.
    private func deleteIdempotent(path: String) async throws {
        let request = await makeRequest(method: "DELETE", path: path)
        let (data, response) = try await perform(request)
        if response.statusCode == 404 { return }
        try check(response, data: data)
    }

    private func send(_ request: URLRequest) async throws -> Data {
        let (data, response) = try await perform(request)
        try check(response, data: data)
        return data
    }

    /// Runs the request and handles the one status that is about this device
    /// rather than about the request: a rejected credential.
    private func perform(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let (data, response) = try await transport(request)
        guard let http = response as? HTTPURLResponse else {
            throw PaperAPIError.malformedResponse
        }
        if http.statusCode == 401 || http.statusCode == 403 {
            await auth.tokenWasRejected()
            throw PaperAPIError.unauthorized
        }
        return (data, http)
    }

    private func check(_ response: HTTPURLResponse, data: Data) throws {
        guard (200..<300).contains(response.statusCode) else {
            throw PaperAPIError.http(
                status: response.statusCode, body: String(data: data, encoding: .utf8) ?? "")
        }
    }

    private func decode<T: Decodable>(_ type: T.Type, from data: Data) throws -> T {
        guard let value = try? JSONDecoder().decode(type, from: data) else {
            throw PaperAPIError.malformedResponse
        }
        return value
    }

    /// FastAPI errors are `{"detail": "..."}`; showing that beats showing JSON.
    public static func detailMessage(in body: String) -> String? {
        guard let data = body.data(using: .utf8),
            let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let detail = object["detail"] as? String, !detail.isEmpty
        else { return nil }
        return detail
    }
}

/// The smallest JSON tree that can be built and encoded without `Any`.
///
/// Request bodies here are small and heterogeneous (`{title, folder, tags[],
/// favorite}`), and `[String: Any]` is not `Sendable`; this keeps the actor
/// boundary honest without inventing a type per body.
public enum JSONValue: Equatable, Sendable, Codable {
    case string(String)
    case int(Int)
    case double(Double)
    case bool(Bool)
    case array([JSONValue])
    case object([String: JSONValue])
    case null

    func encoded() -> Data {
        (try? JSONSerialization.data(withJSONObject: foundationValue, options: [.sortedKeys]))
            ?? Data("{}".utf8)
    }

    var foundationValue: Any {
        switch self {
        case .string(let value): return value
        case .int(let value): return value
        case .double(let value): return value
        case .bool(let value): return value
        case .array(let values): return values.map(\.foundationValue)
        case .object(let values): return values.mapValues(\.foundationValue)
        case .null: return NSNull()
        }
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Int.self) {
            self = .int(value)
        } else if let value = try? container.decode(Double.self) {
            self = .double(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else if let value = try? container.decode([String: JSONValue].self) {
            self = .object(value)
        } else {
            throw DecodingError.dataCorruptedError(
                in: container, debugDescription: "Unsupported JSON value")
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .string(let value): try container.encode(value)
        case .int(let value): try container.encode(value)
        case .double(let value): try container.encode(value)
        case .bool(let value): try container.encode(value)
        case .array(let value): try container.encode(value)
        case .object(let value): try container.encode(value)
        case .null: try container.encodeNil()
        }
    }
}
