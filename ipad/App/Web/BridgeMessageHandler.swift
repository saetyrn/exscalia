import Foundation
import WebKit

/// The only thing web content is allowed to ask the shell to do.
///
/// Everything arriving on the `exscalia` message channel is page data, not a
/// command: it is validated field by field here, and anything unrecognised is
/// dropped without side effects.
enum BridgeMessage: Equatable {
    case open(PaperTarget)

    /// Longest values we will look at. A hostile or buggy page cannot make the
    /// shell hold on to unbounded strings.
    static let maxFieldLength = 512

    static func parse(_ body: Any) -> BridgeMessage? {
        guard let dictionary = body as? [String: Any] else { return nil }
        guard let type = dictionary["type"] as? String, type == "open" else { return nil }
        guard let section = dictionary["section"] as? String, section == "paper" else {
            return nil
        }
        return .open(
            PaperTarget(
                notebook: sanitized(dictionary["notebook"]),
                page: sanitized(dictionary["page"]),
                notebookID: sanitizedIdentifier(
                    dictionary["notebookID"] ?? dictionary["notebook_id"]),
                region: sanitizedRegion(dictionary["region"])
            )
        )
    }

    private static func sanitized(_ value: Any?) -> String? {
        guard let text = value as? String else { return nil }
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, trimmed.count <= maxFieldLength else { return nil }
        // Paths are vault-relative; a leading slash or a traversal segment means
        // the page is not talking about a notebook we own.
        guard !trimmed.hasPrefix("/"), !trimmed.contains(".."), !trimmed.contains("\0") else {
            return nil
        }
        return trimmed
    }

    private static func sanitizedIdentifier(_ value: Any?) -> String? {
        guard let trimmed = sanitized(value),
            trimmed.unicodeScalars.allSatisfy({
                $0.isASCII
                    && ($0.properties.isAlphabetic
                        || (48...57).contains(Int($0.value))
                        || $0 == "_" || $0 == "-")
            })
        else { return nil }
        return trimmed
    }

    private static func sanitizedRegion(_ value: Any?) -> String? {
        guard let trimmed = sanitized(value) else { return nil }
        let values = trimmed.split(separator: ",").compactMap { Double($0) }
        guard values.count == 4, values.allSatisfy({ $0.isFinite }), values[2] >= 0, values[3] >= 0
        else { return nil }
        return trimmed
    }
}

/// Receives `window.webkit.messageHandlers.exscalia.postMessage(...)`.
///
/// Only the configured server's own top-level document may speak: a message
/// from a subframe, or from any other origin, is dropped before its body is
/// read.
@MainActor
final class BridgeMessageHandler: NSObject, WKScriptMessageHandler {
    static let name = "exscalia"

    /// Asked per message, because the server can change while the shell runs.
    private let expectedOrigin: () -> URL?
    private let onMessage: (BridgeMessage) -> Void

    init(expectedOrigin: @escaping () -> URL?, onMessage: @escaping (BridgeMessage) -> Void) {
        self.expectedOrigin = expectedOrigin
        self.onMessage = onMessage
    }

    func userContentController(
        _ userContentController: WKUserContentController, didReceive message: WKScriptMessage
    ) {
        let frame = message.frameInfo
        guard message.name == Self.name, let expected = expectedOrigin(),
            Self.isTrustedFrame(
                isMainFrame: frame.isMainFrame, scheme: frame.securityOrigin.protocol,
                host: frame.securityOrigin.host, port: frame.securityOrigin.port,
                expected: expected),
            let parsed = BridgeMessage.parse(message.body)
        else { return }
        onMessage(parsed)
    }

    /// Made on the parts of the frame's origin, since neither `WKScriptMessage`
    /// nor `WKSecurityOrigin` can be built outside WebKit.
    nonisolated static func isTrustedFrame(
        isMainFrame: Bool, scheme: String, host: String, port: Int, expected: URL
    ) -> Bool {
        isMainFrame && WebOrigin.matches(scheme: scheme, host: host, port: port, expected)
    }
}

/// Scheme + host + port, the way WebKit compares origins.
enum WebOrigin {
    static func matches(scheme: String?, host: String?, port: Int?, _ origin: URL) -> Bool {
        guard let scheme = scheme?.lowercased(), let host = host.map(normalizedHost),
            let expectedScheme = origin.scheme?.lowercased(),
            let expectedHost = origin.host().map(normalizedHost),
            let port = effectivePort(port, scheme: scheme),
            let expectedPort = effectivePort(origin.port, scheme: expectedScheme)
        else { return false }
        return scheme == expectedScheme && host == expectedHost && port == expectedPort
    }

    /// `WKSecurityOrigin.port` is 0 for the scheme's default port and `URL.port`
    /// is nil; both mean 443 for https and 80 for http. Any other scheme has no
    /// origin worth comparing.
    private static func effectivePort(_ port: Int?, scheme: String) -> Int? {
        let standard: Int
        switch scheme {
        case "https": standard = 443
        case "http": standard = 80
        default: return nil
        }
        guard let port, port != 0 else { return standard }
        return port
    }

    private static func normalizedHost(_ host: String) -> String {
        host.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
    }
}
