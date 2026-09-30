import CryptoKit
import Foundation

/// This install's identity on the change feed.
///
/// Every write carries it as `client_id`, and the server puts it back on the
/// live event as `origin`, which is how this device tells its own echo from
/// somebody else's edit. Per install rather than per server: it identifies the
/// iPad, not the account.
public enum PaperClientID {
    public static func current(defaults: UserDefaults = .standard) -> String {
        if let stored = defaults.string(forKey: ServerSettings.Key.clientID),
            !stored.isEmpty
        {
            return stored
        }
        let raw = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        let created = "ipad-" + String(raw.prefix(16))
        defaults.set(created, forKey: ServerSettings.Key.clientID)
        return created
    }
}

/// Where the app talks to, and where it keeps that server's files.
///
/// Every server gets its own store root keyed by `sha1(origin)` so two servers
/// (say the LAN box and the public one) can never mix notebooks.
public struct ServerSettings: Equatable, Sendable {
    public static let defaultOrigin = URL(string: "https://notes.exscalia.art")

    /// UserDefaults keys, shared with the app target's settings screen.
    public enum Key {
        public static let serverURL = "paper.serverURL"
        public static let drawWithFinger = "paper.drawWithFinger"
        public static let useSystemToolPicker = "paper.useSystemToolPicker"
        public static let recognitionLanguages = "paper.recognitionLanguages"
        public static let clientID = "paper.clientID"
    }

    /// Scheme + host + port only; no path, no query, no trailing slash.
    public let origin: URL

    public init?(origin: URL) {
        guard let normalized = Self.normalize(origin) else { return nil }
        self.origin = normalized
    }

    public init?(urlString: String) {
        guard let url = Self.parse(urlString) else { return nil }
        self.init(origin: url)
    }

    /// Accepts what a person actually types: `notes.exscalia.art`,
    /// `http://192.168.1.4:8600`, trailing slashes and stray whitespace.
    /// Returns `nil` for anything that is not an http(s) origin.
    public static func parse(_ raw: String) -> URL? {
        var text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        if !text.contains("://") { text = "https://" + text }
        guard let url = URL(string: text) else { return nil }
        return normalize(url)
    }

    static func normalize(_ url: URL) -> URL? {
        guard var components = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
            return nil
        }
        let scheme = (components.scheme ?? "").lowercased()
        guard scheme == "http" || scheme == "https" else { return nil }
        guard let host = components.host, !host.isEmpty else { return nil }
        components.scheme = scheme
        components.host = host.lowercased()
        components.path = ""
        components.query = nil
        components.fragment = nil
        components.user = nil
        components.password = nil
        if (scheme == "https" && components.port == 443) || (scheme == "http" && components.port == 80) {
            components.port = nil
        }
        return components.url
    }

    /// `https://notes.exscalia.art` -- what goes into the store key and the
    /// cookie lookup.
    public var originString: String { origin.absoluteString }

    /// True when `__Host-` cookies cannot exist, so the server is expected to
    /// be running with auth off (LAN development).
    public var isPlaintext: Bool { origin.scheme == "http" }

    /// Stable per-server directory name.
    public var storeKey: String {
        let digest = Insecure.SHA1.hash(data: Data(originString.utf8))
        return digest.map { String(format: "%02x", $0) }.joined()
    }

    /// `Application Support/Paper/<sha1(origin)>` -- the root of everything this
    /// server owns on this device.
    public func storeRoot(
        in directory: FileManager.SearchPathDirectory = .applicationSupportDirectory,
        fileManager: FileManager = .default
    ) throws -> URL {
        let base = try fileManager.url(
            for: directory, in: .userDomainMask, appropriateFor: nil, create: true
        )
        return base.appending(path: "Paper", directoryHint: .isDirectory)
            .appending(path: storeKey, directoryHint: .isDirectory)
    }

    /// Absolute URL for an API path such as `/api/auth/status`.
    public func url(path: String) -> URL {
        let trimmed = path.hasPrefix("/") ? String(path.dropFirst()) : path
        return origin.appending(path: trimmed)
    }
}
