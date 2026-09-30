import Foundation
import PaperCore
import WebKit

/// Turns the web shell's session cookie into a bearer token for native
/// requests.
///
/// `token_from_request` on the server accepts `__Host-omnia_session` as either
/// a cookie or `Authorization: Bearer <same token>`, so there is no separate
/// native login: whatever the user did in the web view is what the native side
/// uses. `__Host-` cookies cannot exist over http, so a plaintext dev server is
/// expected to be running with auth off.
@MainActor
final class CookieSessionAuth: NSObject {
    nonisolated static let cookieName = "__Host-omnia_session"

    private let cookieStore: WKHTTPCookieStore
    private var onSignedOut: (() -> Void)?
    /// Called on the main actor whenever the store's cookies change -- a
    /// sign-in, a sign-out, or anything else the page set.
    var onCookiesChanged: (@MainActor () -> Void)?

    init(cookieStore: WKHTTPCookieStore) {
        self.cookieStore = cookieStore
        super.init()
        cookieStore.add(self)
    }

    func onSignedOut(_ handler: @escaping () -> Void) {
        onSignedOut = handler
    }

    /// Reads the store fresh. `WKHTTPCookieStore` is lazy on a cold start, so
    /// this must never be answered from a cache that was populated before the
    /// web view existed.
    ///
    /// Only over https: the cookie is never sent in the clear by the web view,
    /// so it is not sent in the clear as a bearer token either.
    func refreshToken(for origin: URL) async -> String? {
        guard origin.scheme?.lowercased() == "https", let host = origin.host() else { return nil }
        let cookies = await cookieStore.allCookies()
        let match = cookies.first { Self.isUsable($0, host: host) }
        return match?.value
    }

    /// The session cookie for `host`, and only a `Secure` one.
    nonisolated static func isUsable(_ cookie: HTTPCookie, host: String) -> Bool {
        cookie.name == cookieName && cookie.isSecure && Self.cookie(cookie, matches: host)
    }

    nonisolated private static func cookie(_ cookie: HTTPCookie, matches host: String) -> Bool {
        let domain = cookie.domain.hasPrefix(".") ? String(cookie.domain.dropFirst()) : cookie.domain
        return host == domain || host.hasSuffix("." + domain)
    }
}

extension CookieSessionAuth: WKHTTPCookieStoreObserver {
    /// Fires on login and on logout. Every request already re-reads the store;
    /// this is only the nudge that lets a signed-out sync engine notice.
    nonisolated func cookiesDidChange(in cookieStore: WKHTTPCookieStore) {
        Task { @MainActor [weak self] in self?.onCookiesChanged?() }
    }
}

/// `AuthProvider` face of `CookieSessionAuth`, so `PaperCore` never imports
/// WebKit.
struct WebViewAuthProvider: AuthProvider {
    private let session: CookieSessionAuth

    init(session: CookieSessionAuth) {
        self.session = session
    }

    func bearerToken(for origin: URL) async -> String? {
        await session.refreshToken(for: origin)
    }

    func tokenWasRejected() async {
        await MainActor.run { session.signalSignedOut() }
    }
}

extension CookieSessionAuth {
    func signalSignedOut() {
        onSignedOut?()
    }
}
