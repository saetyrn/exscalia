import Foundation

/// What the user is told about the durability of their ink, at any moment.
///
/// The research invariant behind Paper is "never wonder whether it saved", so
/// this is a small closed set that always has an honest answer -- there is no
/// "unknown" case and no spinner.
public enum SaveState: Equatable, Sendable {
    /// Written to the device journal; nothing is pending upstream.
    case savedLocally
    /// `count` operations are queued for the server right now.
    case syncing(count: Int)
    /// Everything is upstream; `at` is when that last became true.
    case synced(at: Date)
    /// No route to the server. Local writes still happen.
    case offline
    /// The server kept a conflicting sibling of a page; the user must look.
    case conflict(pageNumber: Int)
    /// The session cookie is gone or was rejected.
    case signedOut
    /// Something upstream needs a person: an op the server refuses, a rename
    /// that collided. The local files are safe; the queue is not moving.
    case attention(message: String)

    /// Short status text for the pill. Deliberately not localized yet.
    public var title: String {
        switch self {
        case .savedLocally:
            return "Saved on iPad"
        case .syncing(let count):
            return "Syncing \(count)"
        case .synced(let at):
            return "Synced \(Self.relative(since: at))"
        case .offline:
            return "Offline — saved on iPad"
        case .conflict(let pageNumber):
            return "Attention: conflict on page \(pageNumber)"
        case .signedOut:
            return "Sign in to sync"
        case .attention(let message):
            return "Attention: \(message)"
        }
    }

    /// Monochrome SF Symbol shown beside the title. Never animated.
    public var symbolName: String {
        switch self {
        case .savedLocally: return "internaldrive"
        case .syncing: return "arrow.triangle.2.circlepath"
        case .synced: return "checkmark.circle"
        case .offline: return "bolt.horizontal.circle"
        case .conflict: return "exclamationmark.triangle"
        case .signedOut: return "person.crop.circle.badge.questionmark"
        case .attention: return "exclamationmark.circle"
        }
    }

    /// States a VoiceOver user is told about when they are entered.
    public var deservesAnnouncement: Bool {
        switch self {
        case .offline, .conflict, .signedOut, .attention: return true
        case .savedLocally, .syncing, .synced: return false
        }
    }

    /// "3s ago" / "2h ago", used by the pill and by the library cards.
    public static func relative(since date: Date, now: Date = Date()) -> String {
        let seconds = Int(max(0, now.timeIntervalSince(date)))
        if seconds < 5 { return "just now" }
        if seconds < 60 { return "\(seconds)s ago" }
        let minutes = seconds / 60
        if minutes < 60 { return "\(minutes)m ago" }
        let hours = minutes / 60
        if hours < 24 { return "\(hours)h ago" }
        return "\(hours / 24)d ago"
    }
}
