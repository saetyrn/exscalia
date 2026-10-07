import Foundation
import Observation
import PaperCore
import PaperUI

/// Which of the two surfaces the root is showing.
enum Surface: Equatable {
    /// The existing web app in a `WKWebView`.
    case web
    /// The native Paper section.
    case paper
}

/// A navigation request delivered to one scene. Scene-local state must never
/// be kept in the app-wide model: two windows may be on different surfaces or
/// different Paper pages at the same time.
enum AppRoute: Equatable {
    case showWeb
    case openPaper(PaperTarget)
}

/// Where Paper should open, as asked for by the web app's bridge message.
/// Both fields are optional: a bare "open Paper" is legitimate.
struct PaperTarget: Equatable {
    var notebook: String?
    var page: String?
    /// Durable links identify a notebook by its stable id because its folder
    /// or title may have changed since the link was copied.
    var notebookID: String?
    /// x,y,width,height in page points, as emitted by the web lasso action.
    var region: String?
    /// A folder, from the web's `/paper?folder=…`: `Work` or `paper/Work`.
    var folder: String?

    init(
        notebook: String? = nil,
        page: String? = nil,
        notebookID: String? = nil,
        region: String? = nil,
        folder: String? = nil
    ) {
        self.notebook = notebook
        self.page = page
        self.notebookID = notebookID
        self.region = region
        self.folder = folder
    }

    /// The bridge speaks the web route's spelling -- `Work/Ideas.paper`, with
    /// no vault root. Everything native is keyed by the full vault path, and
    /// anything that does not look like a notebook package is dropped rather
    /// than guessed at: the message is page data, not a command.
    var deepLink: PaperDeepLink {
        let box = Self.parseRegion(region)
        if let notebookID, !notebookID.isEmpty {
            // The id route is rename-proof. If a web client also included its
            // route-shaped notebook string (`id/<id>/<page>`), ignore it.
            return PaperDeepLink(notebookID: notebookID, page: page, box: box)
        }
        guard let notebook, !notebook.isEmpty else {
            return PaperDeepLink(page: nil, box: box, folder: Self.folderPath(folder))
        }
        let full = notebook.hasPrefix("paper/") ? notebook : "paper/" + notebook
        guard PaperPaths.isSafeNotebookPath(full) else { return PaperDeepLink() }
        return PaperDeepLink(notebook: full, page: page, box: box)
    }

    /// A folder the library can show, root kept: every component a real
    /// name and none of them a notebook package. Anything else is dropped.
    static func folderPath(_ raw: String?) -> String? {
        guard var path = raw?.trimmingCharacters(in: .whitespacesAndNewlines), !path.isEmpty else {
            return nil
        }
        while path.hasSuffix("/") { path.removeLast() }
        if path != "paper", !path.hasPrefix("paper/") { path = "paper/" + path }
        let parts = path.split(separator: "/", omittingEmptySubsequences: false)
        guard parts.allSatisfy({ part in
            !part.isEmpty && !part.hasPrefix(".") && !part.lowercased().hasSuffix(".paper")
                && !part.contains("\\")
        })
        else { return nil }
        return path
    }

    private static func parseRegion(_ raw: String?) -> CGRect? {
        guard let raw else { return nil }
        let values = raw.split(separator: ",").compactMap { Double($0) }
        guard values.count == 4, values.allSatisfy({ $0.isFinite }) else { return nil }
        let box = CGRect(x: values[0], y: values[1], width: values[2], height: values[3])
        guard box.width >= 0, box.height >= 0 else { return nil }
        return box
    }
}

/// Hands the native client whatever credential the web shell currently has.
///
/// The web shell owns the cookie and `PaperCore` must not import WebKit, so
/// this sits between them: `WebShellController` registers a source once its web
/// view exists, and everything built before that simply gets no token -- which
/// is exactly right for a LAN server running with authentication off.
actor ForwardingAuthProvider: AuthProvider {
    private var source: (@Sendable (URL) async -> String?)?
    private var onRejected: (@Sendable () -> Void)?

    func setSource(
        _ source: @escaping @Sendable (URL) async -> String?,
        onRejected: @escaping @Sendable () -> Void
    ) {
        self.source = source
        self.onRejected = onRejected
    }

    func bearerToken(for origin: URL) async -> String? {
        await source?(origin)
    }

    func tokenWasRejected() async {
        onRejected?()
    }
}

/// The single piece of app-wide state. Owned by the main actor because every
/// reader of it is a view.
@MainActor
@Observable
final class AppModel {
    /// App Intents can be invoked while a scene is already running. The
    /// SwiftUI App installs this reference when it creates its one shared
    /// model; tests can still construct isolated models normally.
    private(set) static weak var shared: AppModel?

    /// Compatibility state for callers that predate multi-window routing.
    /// It is not read by RootView; each scene owns its own `SceneModel`.
    private(set) var surface: Surface = .web
    private(set) var paperTarget = PaperTarget()

    /// Server the app talks to. `nil` only if the stored value is unparseable
    /// and the compiled-in default somehow is too, which cannot happen with the
    /// current default -- callers still handle it rather than force-unwrapping.
    private(set) var server: ServerSettings?

    /// What the pill says. M1 only ever moves this between `.savedLocally` and
    /// `.signedOut`; M3 drives it properly.
    var saveState: SaveState = .savedLocally

    /// The Paper section's single owner of state, built for whichever server is
    /// selected: the store root is keyed by `sha1(origin)` so two servers never
    /// share notebooks. Built lazily because a person who never opens Paper
    /// should never pay for a SQLite file.
    @ObservationIgnored private var cachedPaperController: PaperController?
    @ObservationIgnored private var cachedPaperStoreKey: String?
    /// The store that could not be opened, remembered so a view update does
    /// not reopen SQLite on every pass. `retryPaperController()` forgets it.
    @ObservationIgnored private var failedPaperStoreKey: String?

    /// Why Paper's storage for this server could not be opened; the Paper
    /// surface shows it in place of the library. Nil once it opens.
    private(set) var paperControllerError: String?

    /// Registered scenes are weak so a closed window cannot keep its view
    /// hierarchy alive. The most recently active scene receives App Intent
    /// navigation. If the app is backgrounded, the request is held until the
    /// next scene becomes active rather than opening Paper in every window.
    @ObservationIgnored private var scenes: [UUID: WeakSceneModel] = [:]
    @ObservationIgnored private var activeSceneID: UUID?
    @ObservationIgnored private var sceneActivationOrder: [UUID: UInt64] = [:]
    @ObservationIgnored private var activationCounter: UInt64 = 0
    @ObservationIgnored private var pendingRoute: AppRoute?
    @ObservationIgnored private var initialSurfaceConsumed = false

    /// One credential source for the whole app, shared by every API client.
    @ObservationIgnored let authProvider = ForwardingAuthProvider()
    /// One session too: the sync engine's requests and its `/api/live` stream
    /// have to agree about cookies (there are none) and credentials.
    @ObservationIgnored let apiSession = PaperAPI.makeSession()
    /// Per install, not per server: it identifies this iPad on the change feed.
    @ObservationIgnored let clientID = PaperClientID.current()

    /// `nil` if the store root or its database cannot be opened; the reason is
    /// in `paperControllerError`, which the caller shows rather than crashing.
    var paperController: PaperController? {
        guard let server else { return nil }
        if let cachedPaperController, cachedPaperStoreKey == server.storeKey {
            return cachedPaperController
        }
        if failedPaperStoreKey == server.storeKey { return nil }
        let controller: PaperController
        do {
            controller = try PaperController(root: try server.storeRoot())
        } catch {
            failedPaperStoreKey = server.storeKey
            paperControllerError = error.localizedDescription
            return nil
        }
        controller.configureSync(
            api: PaperAPI(
                settings: server, auth: authProvider, session: apiSession,
                userAgentSuffix: "EXSCALIA-iPad"),
            clientID: clientID, session: apiSession)
        cachedPaperController = controller
        cachedPaperStoreKey = server.storeKey
        failedPaperStoreKey = nil
        if paperControllerError != nil { paperControllerError = nil }
        return controller
    }

    /// The controller only when Paper is already open for this server.
    /// Credential changes go through this so they never create a store that
    /// nobody opened.
    var openedPaperController: PaperController? {
        guard let server, cachedPaperStoreKey == server.storeKey else { return nil }
        return cachedPaperController
    }

    /// The failure state's Retry: forget the failure, so the next read of
    /// `paperController` tries to open the store again.
    func retryPaperController() {
        failedPaperStoreKey = nil
        paperControllerError = nil
    }

    private let defaults: UserDefaults

    init(defaults: UserDefaults = .standard, launchArguments: [String] = CommandLine.arguments) {
        self.defaults = defaults
        self.server = Self.storedServer(in: defaults)
        if let index = launchArguments.firstIndex(of: "-surface"),
            launchArguments.indices.contains(index + 1),
            launchArguments[index + 1] == "paper"
        {
            surface = .paper
        }
        if let index = launchArguments.firstIndex(of: "-server"),
            launchArguments.indices.contains(index + 1),
            let override = ServerSettings(urlString: launchArguments[index + 1])
        {
            server = override
        }
        Self.shared = self
    }

    static func storedServer(in defaults: UserDefaults) -> ServerSettings? {
        if let stored = defaults.string(forKey: ServerSettings.Key.serverURL),
            let settings = ServerSettings(urlString: stored)
        {
            return settings
        }
        guard let fallback = ServerSettings.defaultOrigin else { return nil }
        return ServerSettings(origin: fallback)
    }

    /// The URL the web shell loads.
    var webHomeURL: URL? { server?.origin }

    func showWeb() {
        surface = .web
        route(.showWeb)
    }

    /// Native -> web: switch to the web surface and drive it to `path`.
    ///
    /// The web app publishes `window.__exscaliaNavigate` inside the shell; the
    /// path is built here, from a notebook this device owns, and never from
    /// anything the page said.
    func openOnWeb(notebookPath: String, pageID: String?) {
        surface = .web
        // The web route's parameter is the notebook path *without* the vault's
        // `paper/` root; the store's key includes it.
        let relative =
            notebookPath.hasPrefix("paper/")
            ? String(notebookPath.dropFirst("paper/".count)) : notebookPath
        var path = "/paper/" + relative
        if let pageID, !pageID.isEmpty { path += "#" + pageID }
        route(.showWeb)
        // Legacy callers have no scene id. New callers use SceneModel's
        // scene-scoped pending navigation request; this notification remains
        // for old integrations but is never emitted by the shell itself.
        NotificationCenter.default.post(
            name: .webShellNavigate, object: nil, userInfo: ["path": path])
    }

    /// Called by the bridge when the web app asks for Paper.
    func openPaper(target: PaperTarget) {
        paperTarget = target
        surface = .paper
        route(.openPaper(target))
    }

    func setServer(_ settings: ServerSettings) {
        if cachedPaperStoreKey != settings.storeKey,
            let oldSync = cachedPaperController?.sync
        {
            // A server switch must not leave the old live stream/push loop
            // running while the next scene creates its controller.
            Task { await oldSync.stop() }
            cachedPaperController = nil
            cachedPaperStoreKey = nil
        }
        server = settings
        defaults.set(settings.originString, forKey: ServerSettings.Key.serverURL)
    }

    func signedOut() {
        saveState = .signedOut
    }

    /// The web shell has a good session again. Only an engine that already
    /// exists is told: `paperController` would build a store and an engine
    /// for someone who never opened Paper.
    func credentialsChanged() {
        if case .signedOut = saveState { saveState = .savedLocally }
        cachedPaperController?.credentialsChanged()
    }

    // MARK: - Multi-window routing

    func register(_ scene: SceneModel) {
        scenes[scene.id] = WeakSceneModel(scene)
        activate(scene)
        if let pendingRoute {
            self.pendingRoute = nil
            scene.apply(pendingRoute)
        }
    }

    func unregister(_ scene: SceneModel) {
        scenes[scene.id] = nil
        sceneActivationOrder[scene.id] = nil
        if activeSceneID == scene.id { activeSceneID = nil }
        chooseMostRecentlyActiveScene()
    }

    func setActive(_ scene: SceneModel, isActive: Bool) {
        guard scenes[scene.id]?.value != nil else { return }
        if isActive {
            activate(scene)
        } else {
            sceneActivationOrder[scene.id] = nil
            if activeSceneID == scene.id { chooseMostRecentlyActiveScene() }
        }
    }

    private func activate(_ scene: SceneModel) {
        activationCounter &+= 1
        sceneActivationOrder[scene.id] = activationCounter
        activeSceneID = scene.id
    }

    private func chooseMostRecentlyActiveScene() {
        activeSceneID = sceneActivationOrder
            .filter { scenes[$0.key]?.value != nil }
            .max { $0.value < $1.value }?.key
    }

    /// Delivers a route to one scene when requested by a view, or to the
    /// foreground scene when requested by App Intents.
    func route(_ route: AppRoute, to sceneID: UUID? = nil) {
        let recipientID = sceneID ?? activeSceneID
        if let recipientID, let scene = scenes[recipientID]?.value {
            scene.apply(route)
            return
        }
        pendingRoute = route
    }

    /// The DEBUG launch surface seeds only the first scene. Without consuming
    /// it, opening a second WindowGroup scene would unexpectedly open Paper in
    /// that window too.
    func consumeInitialPaperSurface() -> Bool {
        guard surface == .paper, !initialSurfaceConsumed else { return false }
        initialSurfaceConsumed = true
        return true
    }

    /// Creates a page on the shared PaperController, then routes the native
    /// surface to it. All windows share this one controller/store/sync engine;
    /// no scene is allowed to create a second writer for the same server.
    func performNewPaperPage() {
        guard let controller = paperController else {
            route(.openPaper(PaperTarget()))
            return
        }
        let recently = (try? controller.store.recentlyOpened(limit: 1))?.first
        let notebook = recently ?? controller.notebooks.first ?? controller.bootstrap()
        guard let notebook else {
            route(.openPaper(PaperTarget()))
            return
        }
        let after = recently.flatMap { $0.id == notebook.id ? $0.pages.last : nil }
            ?? notebook.pages.last
        guard let page = try? controller.store.createPage(notebookID: notebook.id, after: after)
        else {
            route(.openPaper(PaperTarget(notebook: notebook.path)))
            return
        }
        controller.refresh()
        controller.syncNow()
        route(.openPaper(PaperTarget(notebook: notebook.path, page: page.id)))
    }

    /// Routes the most recently opened notebook without creating a page.
    func performOpenLastNotebook() {
        guard let controller = paperController else {
            route(.openPaper(PaperTarget()))
            return
        }
        let notebook = (try? controller.store.recentlyOpened(limit: 1))?.first
            ?? controller.notebooks.first
        guard let notebook else {
            route(.openPaper(PaperTarget()))
            return
        }
        let state = try? controller.store.viewState(notebookID: notebook.id)
        route(.openPaper(PaperTarget(notebook: notebook.path, page: state?.lastPageID)))
    }
}

/// State owned by one WindowGroup scene. `AppModel` remains the shared home
/// for server settings, Paper storage and the one SyncEngine; this object owns
/// only navigation and transient web-shell UI for its window.
@MainActor
@Observable
final class SceneModel: Identifiable {
    let id = UUID()
    private(set) var surface: Surface = .web
    private(set) var paperTarget = PaperTarget()
    var webLoadFailure: String?
    var isShowingSettings = false

    @ObservationIgnored private weak var appModel: AppModel?
    @ObservationIgnored private(set) var pendingWebNavigation: String?

    var app: AppModel? { appModel }
    var paperController: PaperController? { appModel?.paperController }
    var saveState: SaveState { appModel?.saveState ?? .savedLocally }

    func attach(to appModel: AppModel) {
        self.appModel = appModel
        appModel.register(self)
    }

    func detach() {
        appModel?.unregister(self)
        appModel = nil
    }

    func setActive(_ active: Bool) {
        appModel?.setActive(self, isActive: active)
    }

    func showWeb() {
        pendingWebNavigation = nil
        if let appModel { appModel.route(.showWeb, to: id) }
        else { apply(.showWeb) }
    }

    func openPaper(target: PaperTarget) {
        if let appModel { appModel.route(.openPaper(target), to: id) }
        else { apply(.openPaper(target)) }
    }

    /// Native Paper calls this when it wants its own window to show a web
    /// viewer URL. The request is consumed only by this scene's WebShellView.
    func openOnWeb(notebookPath: String, pageID: String?) {
        let relative = notebookPath.hasPrefix("paper/")
            ? String(notebookPath.dropFirst("paper/".count)) : notebookPath
        var path = "/paper/" + relative
        if let pageID, !pageID.isEmpty { path += "#" + pageID }
        openOnWeb(path: path)
    }

    /// Generic native-to-web navigation for Paper actions such as OMNIA. A
    /// path is data, not script: accept only an in-app absolute path and keep
    /// the request scoped to this scene.
    func openOnWeb(path: String) {
        guard path.hasPrefix("/"), !path.hasPrefix("//"), !path.contains("\0") else { return }
        pendingWebNavigation = path
        apply(.showWeb)
    }

    func consumeWebNavigation() -> String? {
        defer { pendingWebNavigation = nil }
        return pendingWebNavigation
    }

    fileprivate func apply(_ route: AppRoute) {
        switch route {
        case .showWeb:
            surface = .web
        case .openPaper(let target):
            paperTarget = target
            surface = .paper
        }
    }
}

private final class WeakSceneModel {
    weak var value: SceneModel?

    init(_ value: SceneModel) { self.value = value }
}
