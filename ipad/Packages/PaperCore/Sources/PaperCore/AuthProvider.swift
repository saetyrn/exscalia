import Foundation

/// Supplies the credential the native client puts on every request to the
/// OMNIA server.
///
/// M1 has exactly one implementation, in the app target: it lifts the
/// `__Host-omnia_session` cookie out of the web shell's `WKHTTPCookieStore`.
/// Keeping this a protocol lets `PaperCore` (and its tests) stay free of
/// WebKit.
public protocol AuthProvider: Sendable {
    /// The bearer token for `origin`, or `nil` when there is no session.
    ///
    /// `origin` is a scheme+host+port string such as `https://notes.exscalia.art`.
    func bearerToken(for origin: URL) async -> String?

    /// Called when the server rejects the token, so the provider can drop it
    /// and the UI can move to `SaveState.signedOut`.
    func tokenWasRejected() async
}

/// A provider that never has a token -- used for local dev servers over http,
/// where `__Host-` cookies cannot exist and auth is therefore off.
public struct AnonymousAuthProvider: AuthProvider {
    public init() {}
    public func bearerToken(for origin: URL) async -> String? { nil }
    public func tokenWasRejected() async {}
}
