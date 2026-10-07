import PaperCore
import SafariServices
import UIKit
import WebKit

extension Notification.Name {
    /// Posted by the failure view's Retry button.
    static let webShellReload = Notification.Name("art.exscalia.ipad.webShellReload")
    /// Posted by "Open on web": an app path for the web view to navigate to,
    /// under `userInfo["path"]`.
    static let webShellNavigate = Notification.Name("art.exscalia.ipad.webShellNavigate")
}

/// Hosts the existing web app.
///
/// Design notes that matter:
/// - the data store is `.default()`, so the session cookie survives a cold
///   launch and the user stays signed in;
/// - `contentInsetAdjustmentBehavior = .never` plus an ignored safe area means
///   `env(safe-area-inset-*)` inside the page behaves exactly as it does in the
///   installed PWA;
/// - navigations to any other origin open in `SFSafariViewController` instead
///   of replacing the app with somebody else's page.
@MainActor
final class WebShellController: UIViewController {
    private let model: AppModel
    private let scene: SceneModel
    private var webView: WKWebView?
    private var auth: CookieSessionAuth?
    private var reloadTask: Task<Void, Never>?
    private var navigateTask: Task<Void, Never>?
    private var loadedOrigin: String?

    init(model: AppModel, scene: SceneModel) {
        self.model = model
        self.scene = scene
        super.init(nibName: nil, bundle: nil)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("WebShellController is created in code only")
    }

    deinit {
        reloadTask?.cancel()
        navigateTask?.cancel()
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground

        let webView = makeWebView()
        webView.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(webView)
        NSLayoutConstraint.activate([
            webView.topAnchor.constraint(equalTo: view.topAnchor),
            webView.bottomAnchor.constraint(equalTo: view.bottomAnchor),
            webView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
        ])
        self.webView = webView

        let auth = CookieSessionAuth(cookieStore: webView.configuration.websiteDataStore.httpCookieStore)
        auth.onSignedOut { [weak self] in
            self?.model.signedOut()
        }
        auth.onCookiesChanged = { [weak self, weak auth] in
            guard let self, let auth, let origin = self.model.server?.origin else { return }
            Task {
                // A sign-out changes the cookies too; only a session that now
                // exists is a reason to wake a signed-out sync engine.
                guard await auth.refreshToken(for: origin) != nil else { return }
                self.model.openedPaperController?.credentialsMayHaveChanged()
            }
        }
        self.auth = auth
        // M3: hand the same cookie to the native client, so the sync engine
        // speaks with whatever session the user established in the web view.
        let provider = model.authProvider
        let model = model
        Task {
            await provider.setSource(
                { origin in await auth.refreshToken(for: origin) },
                onRejected: {
                    Task { @MainActor in model.signedOut() }
                })
        }

        reloadTask = Task { [weak self] in
            for await _ in NotificationCenter.default.notifications(named: .webShellReload) {
                guard let self else { return }
                await MainActor.run { self.loadHome(force: true) }
            }
        }

        navigateTask = Task { [weak self] in
            for await note in NotificationCenter.default.notifications(named: .webShellNavigate) {
                guard let path = note.userInfo?["path"] as? String else { continue }
                // SceneModel navigation is the normal path. If a compatibility
                // notification carries a scene id, never let another window
                // consume it.
                if let sceneID = note.userInfo?["sceneID"] as? UUID,
                    sceneID != self?.scene.id { continue }
                await MainActor.run { self?.navigate(to: path) }
            }
        }

        loadHome(force: true)
    }

    /// Native -> web, through the hook the web app publishes inside the shell.
    ///
    /// `path` is built by the shell from a notebook this device owns, never
    /// from anything a page said, and it is checked again here: only an app
    /// path, and only as a JSON string literal, ever reaches the evaluator.
    func navigate(to path: String) {
        guard let webView, path.hasPrefix("/"), !path.hasPrefix("//") else { return }
        guard let encoded = try? JSONSerialization.data(withJSONObject: [path]),
            let literal = String(data: encoded, encoding: .utf8)
        else { return }
        // A web view that has never loaded has no hook yet; the URL itself is
        // the same destination by the other road.
        guard webView.url != nil else {
            if let home = model.webHomeURL, let url = URL(string: path, relativeTo: home) {
                webView.load(URLRequest(url: url))
            }
            return
        }
        let script =
            "(function(){var p = " + literal + "[0];"
            + "if (typeof window.__exscaliaNavigate === 'function') "
            + "{ window.__exscaliaNavigate(p); } else { window.location.assign(p); }})()"
        webView.evaluateJavaScript(script)
    }

    override func viewWillAppear(_ animated: Bool) {
        super.viewWillAppear(animated)
        loadHome(force: false)
    }

    /// Loads the configured server, and reloads when the server has changed.
    func loadHome(force: Bool) {
        guard let webView, let home = model.webHomeURL else { return }
        if !force, loadedOrigin == home.absoluteString, webView.url != nil { return }
        loadedOrigin = home.absoluteString
        scene.webLoadFailure = nil
        webView.load(URLRequest(url: home))
    }

    /// M1 proof that the cookie-as-bearer path works end to end.
    /// The result only drives the pill; it never blocks the UI.
    func checkAuthStatus() async {
        guard let settings = model.server, let auth else { return }
        let api = PaperAPI(
            settings: settings,
            auth: WebViewAuthProvider(session: auth),
            session: PaperAPI.makeSession(),
            userAgentSuffix: Self.userAgentSuffix
        )
        do {
            let status = try await api.authStatus()
            if status.authRequired && !status.authenticated {
                model.signedOut()
            } else {
                if case .signedOut = model.saveState { model.saveState = .savedLocally }
                model.credentialsChanged()
                // A web sign-in lands here too: the sync engine resumes now
                // rather than at its next poll.
                model.openedPaperController?.credentialsMayHaveChanged()
            }
        } catch PaperAPIError.unauthorized {
            model.signedOut()
        } catch {
            // Offline or server down: M3 owns the offline pill; M1 stays quiet.
        }
    }

    static var appVersion: String {
        let info = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String
        return info ?? "0"
    }

    static var userAgentSuffix: String { "EXSCALIA-iPad/\(appVersion)" }

    private func makeWebView() -> WKWebView {
        let configuration = WKWebViewConfiguration()
        // Persistent store: the session cookie must outlive the process.
        configuration.websiteDataStore = .default()
        configuration.allowsInlineMediaPlayback = true
        configuration.defaultWebpagePreferences.allowsContentJavaScript = true
        configuration.applicationNameForUserAgent = Self.userAgentSuffix

        let controller = WKUserContentController()
        controller.addUserScript(
            WKUserScript(
                source: "window.__EXSCALIA_IPAD__ = true;",
                injectionTime: .atDocumentStart,
                forMainFrameOnly: true
            )
        )
        controller.add(
            BridgeMessageHandler(
                expectedOrigin: { [weak self] in self?.model.server?.origin },
                onMessage: { [weak self] message in
                    switch message {
                    case .open(let target):
                        self?.scene.openPaper(target: target)
                    }
                }),
            name: BridgeMessageHandler.name
        )
        configuration.userContentController = controller

        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = true
        webView.allowsLinkPreview = false
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        #if DEBUG
            webView.isInspectable = true
        #endif
        return webView
    }

    private func isInternal(_ url: URL) -> Bool {
        guard let origin = model.server?.origin else { return false }
        return Self.isInternal(url, origin: origin)
    }

    /// Same scheme, host and port as the server. Another port on the same
    /// host, http beside https, or a file is not the app.
    nonisolated static func isInternal(_ url: URL, origin: URL) -> Bool {
        WebOrigin.matches(scheme: url.scheme, host: url.host(), port: url.port, origin)
    }

    private func openExternally(_ url: URL) {
        guard let scheme = url.scheme?.lowercased(), scheme == "http" || scheme == "https" else {
            UIApplication.shared.open(url)
            return
        }
        let safari = SFSafariViewController(url: url)
        safari.modalPresentationStyle = .pageSheet
        present(safari, animated: true)
    }
}

extension WebShellController: WKNavigationDelegate {
    func webView(
        _ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping @MainActor (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.cancel)
            return
        }
        if isInternal(url) || url.scheme == "about" {
            decisionHandler(.allow)
            return
        }
        decisionHandler(.cancel)
        openExternally(url)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        scene.webLoadFailure = nil
        Task { await checkAuthStatus() }
    }

    func webView(
        _ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
        withError error: Error
    ) {
        // A cancelled navigation is not a failure; anything else means we have
        // nothing on screen and must say so natively.
        let nsError = error as NSError
        guard !(nsError.domain == NSURLErrorDomain && nsError.code == NSURLErrorCancelled) else {
            return
        }
        scene.webLoadFailure = nsError.localizedDescription
    }
}

extension WebShellController: WKUIDelegate {
    func webView(
        _ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        // target=_blank: open outside rather than spawning a second web view.
        if let url = navigationAction.request.url {
            if isInternal(url) {
                webView.load(URLRequest(url: url))
            } else {
                openExternally(url)
            }
        }
        return nil
    }
}
