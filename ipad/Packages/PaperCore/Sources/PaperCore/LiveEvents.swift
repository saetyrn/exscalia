import Foundation

/// One dispatched server-sent event.
public struct ServerSentEvent: Equatable, Sendable {
    /// `event:` field, or `message` when the stream did not name one.
    public var event: String
    /// `data:` fields, joined with newlines in arrival order.
    public var data: String
    public var id: String?

    public init(event: String = "message", data: String = "", id: String? = nil) {
        self.event = event
        self.data = data
        self.id = id
    }
}

/// The `paper` event `/api/live` publishes after every write.
///
/// `origin` is the `client_id` of whoever caused the change, which is the whole
/// point: an event this device caused must not make it pull its own echo.
public struct PaperLiveEvent: Codable, Equatable, Sendable {
    public var type: String
    public var path: String?
    public var revision: Int?
    public var origin: String?

    public init(type: String, path: String? = nil, revision: Int? = nil, origin: String? = nil) {
        self.type = type
        self.path = path
        self.revision = revision
        self.origin = origin
    }
}

/// A `text/event-stream` parser, one line at a time.
///
/// Small on purpose: the whole grammar this needs is comments, `field: value`
/// with one optional leading space, `data` accumulating across lines, and a
/// blank line dispatching. Written as a `mutating func consume` so it can be
/// driven by a test with a string array and by the network with a byte stream,
/// with no difference between them.
public struct SSEParser: Sendable {
    private var event: String?
    private var dataLines: [String] = []
    private var id: String?

    public init() {}

    /// Feeds one line (with its terminator already stripped). Returns an event
    /// when that line completed one.
    public mutating func consume(line: String) -> ServerSentEvent? {
        // A stream that ends in CRLF leaves the CR behind when split on LF.
        let line = line.hasSuffix("\r") ? String(line.dropLast()) : line

        if line.isEmpty {
            defer { reset() }
            guard event != nil || !dataLines.isEmpty else { return nil }
            return ServerSentEvent(
                event: event ?? "message", data: dataLines.joined(separator: "\n"), id: id)
        }
        // ": keepalive" and any other comment line.
        if line.hasPrefix(":") { return nil }

        let field: String
        var value: String
        if let colon = line.firstIndex(of: ":") {
            field = String(line[line.startIndex..<colon])
            value = String(line[line.index(after: colon)...])
            if value.hasPrefix(" ") { value.removeFirst() }
        } else {
            field = line
            value = ""
        }

        switch field {
        case "event": event = value
        case "data": dataLines.append(value)
        case "id": id = value
        default: break  // `retry` and anything unknown: not our business.
        }
        return nil
    }

    /// Feeds a whole chunk of text, for tests and for a buffered reader.
    public mutating func consume(text: String) -> [ServerSentEvent] {
        var events: [ServerSentEvent] = []
        for line in text.components(separatedBy: "\n") {
            if let event = consume(line: line) { events.append(event) }
        }
        return events
    }

    private mutating func reset() {
        event = nil
        dataLines = []
        id = nil
    }
}

extension ServerSentEvent {
    /// The `paper` payload, or `nil` when this event is about something else.
    public var paperEvent: PaperLiveEvent? {
        guard event == "paper", let payload = data.data(using: .utf8) else { return nil }
        return try? JSONDecoder().decode(PaperLiveEvent.self, from: payload)
    }
}

/// Reads `/api/live` and hands back events until the connection ends.
///
/// Reconnection is the caller's business: the sync engine already knows about
/// reachability and backoff, and a stream that quietly reconnected on its own
/// would hide "we are not hearing about other devices" from the pill.
public struct LiveEventStream: Sendable {
    private let session: URLSession
    private let request: @Sendable () async -> URLRequest

    public init(session: URLSession, request: @escaping @Sendable () async -> URLRequest) {
        self.session = session
        self.request = request
    }

    /// One connection's worth of events. Throws when the connection fails or
    /// the server answers non-2xx.
    public func events() -> AsyncThrowingStream<ServerSentEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    let (bytes, response) = try await session.bytes(for: await request())
                    if let http = response as? HTTPURLResponse,
                        !(200..<300).contains(http.statusCode)
                    {
                        throw PaperAPIError.http(status: http.statusCode, body: "")
                    }
                    var parser = SSEParser()
                    for try await line in bytes.lines {
                        if let event = parser.consume(line: line) {
                            continuation.yield(event)
                        }
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }
}
