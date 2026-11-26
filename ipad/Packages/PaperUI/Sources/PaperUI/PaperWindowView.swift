import PaperCore
import SwiftUI
import UIKit

/// The root of the Paper surface in one window: the library at Home, the
/// editor -- tabs, navigation bar, canvas -- once a document is open.
///
/// It holds the window's tab list and nothing about any document beyond it.
/// The list is kept in `@SceneStorage("paper.window")`, so a window comes back
/// with its tabs after a relaunch; read-only flags and Quick Notes are kept in
/// memory only. The library stays alive behind the editor, so Home returns to
/// it as it was left.
public struct PaperWindowView: View {
    /// `NSUserActivity` type of "Open in New Window", declared in the app's
    /// `NSUserActivityTypes`.
    public static let windowActivityType = "art.exscalia.ipad.paper.window"
    /// The activity's `targetContentIdentifier`.
    public static let windowTargetIdentifier = "paper/window"

    @State private var tabs: DocumentTabsModel
    @SceneStorage("paper.window") private var stored = ""
    @State private var hasStarted = false
    @State private var remoteChangeObserverID: UUID?
    @State private var libraryFolderID: String?

    private let deepLink: PaperDeepLink
    private let windowTarget: PaperWindowTarget?
    private let onOpenOnWeb: ((String, String?) -> Void)?
    private let onNavigateOnWeb: ((String) -> Void)?
    private let scanDocument: (@MainActor (UIViewController) async throws -> Data)?
    private let onBack: () -> Void

    public init(
        controller: PaperController,
        deepLink: PaperDeepLink = PaperDeepLink(),
        windowTarget: PaperWindowTarget? = nil,
        onOpenOnWeb: ((String, String?) -> Void)? = nil,
        onNavigateOnWeb: ((String) -> Void)? = nil,
        scanDocument: (@MainActor (UIViewController) async throws -> Data)? = nil,
        onBack: @escaping () -> Void
    ) {
        _tabs = State(initialValue: DocumentTabsModel(controller: controller))
        self.deepLink = deepLink
        self.windowTarget = windowTarget
        self.onOpenOnWeb = onOpenOnWeb
        self.onNavigateOnWeb = onNavigateOnWeb
        self.scanDocument = scanDocument
        self.onBack = onBack
    }

    public var body: some View {
        ZStack {
            LibraryView(
                controller: tabs.controller,
                onOpenOnWeb: onOpenOnWeb,
                onNavigateOnWeb: onNavigateOnWeb,
                scanDocument: scanDocument,
                onBack: onBack
            )
            .opacity(tabs.isHome ? 1 : 0)
            // Hidden is not enough: its buttons and shortcuts would still
            // answer under the editor.
            .disabled(!tabs.isHome)
            .accessibilityHidden(!tabs.isHome)

            if let session = tabs.session {
                EditorScreen(
                    tabs: tabs, session: session, onOpenOnWeb: onOpenOnWeb,
                    onNavigateOnWeb: onNavigateOnWeb, scanDocument: scanDocument)
                .transition(.identity)
            }
        }
        .environment(\.paperOpenDocument, PaperOpenDocumentAction(tabs: tabs))
        .environment(\.paperConvertDocument, PaperConvertDocumentAction(tabs: tabs))
        .environment(\.paperLibrarySearchRequest, tabs.librarySearchRequest)
        .environment(\.paperLibraryFolderID, libraryFolderID)
        .overlay(alignment: .bottom) { toast }
        .background { shortcuts }
        .onAppear(perform: start)
        .task { await bootstrap() }
        .onDisappear {
            if let remoteChangeObserverID {
                tabs.controller.removeRemoteChangeObserver(remoteChangeObserverID)
                self.remoteChangeObserverID = nil
            }
        }
        .onChange(of: tabs.storedJSON) { _, json in stored = json }
        .onChange(of: tabs.controller.notebooks.map(\.id)) { _, _ in tabs.closeMissing() }
        .onChange(of: deepLink) { _, link in follow(link) }
        .onChange(of: windowTarget) { _, target in open(target) }
    }

    // MARK: - Start

    /// The tabs this window had, unless it was opened for something else: a
    /// window target, or a DEBUG launch argument that opens a document.
    private func start() {
        guard !hasStarted else { return }
        hasStarted = true
        remoteChangeObserverID = tabs.controller.observeRemoteChanges { [tabs] paths in
            tabs.remoteChange(paths)
        }
        if windowTarget != nil {
            open(windowTarget)
        } else if !Self.debugOpensDocument {
            tabs.restore(json: stored)
        }
    }

    /// Repairs the index once, then opens what was asked for: a first
    /// launch's new notebook, a DEBUG launch argument, a deep link.
    private func bootstrap() async {
        if let created = await tabs.controller.bootstrapIfNeeded() {
            tabs.open(created)
        }
        tabs.closeMissing()
        #if DEBUG
            // The launch's arguments are for the launch's window, not for one
            // "Open in New Window" made later in the same process.
            if windowTarget == nil, runDebugDocumentArguments() { return }
        #endif
        follow(deepLink)
    }

    private func open(_ target: PaperWindowTarget?) {
        switch target {
        case .document(let id, let pageID):
            guard let notebook = try? tabs.controller.store.notebook(id: id) else { return }
            tabs.open(
                notebook,
                reveal: pageID.map { SearchDestination(notebookPath: notebook.path, pageID: $0) })
        case .library(let folderID):
            libraryFolderID = folderID
            tabs.goHome()
        case nil:
            break
        }
    }

    /// Opens what the web app (or a search result) pointed at. A notebook this
    /// device does not have is not an error worth a dialog: the sync engine
    /// may still be fetching it.
    private func follow(_ link: PaperDeepLink) {
        // `/paper?folder=…` on the web: the library at that folder.
        if link.notebook == nil, link.notebookID == nil, let path = link.folder,
            let folder = tabs.controller.libraryFolder(path: path)
        {
            // Through nil, so asking twice for one folder opens it twice.
            libraryFolderID = nil
            tabs.goHome()
            Task { @MainActor in libraryFolderID = folder.id }
            return
        }
        let notebook: Notebook?
        if let path = link.notebook {
            notebook = tabs.controller.notebook(atPath: path)
        } else if let notebookID = link.notebookID {
            notebook = try? tabs.controller.store.notebook(id: notebookID)
        } else {
            notebook = nil
        }
        guard let notebook else { return }
        tabs.open(
            notebook,
            reveal: SearchDestination(notebookPath: notebook.path, pageID: link.page, box: link.box))
    }

    // MARK: - New windows

    /// "Open in New Window": a new scene, which `RootView` points at
    /// `target` when the activity arrives.
    static func requestWindow(_ target: PaperWindowTarget) {
        let activity = NSUserActivity(activityType: windowActivityType)
        activity.targetContentIdentifier = windowTargetIdentifier
        if let data = try? JSONEncoder().encode(target) {
            activity.userInfo = ["target": String(decoding: data, as: UTF8.self)]
        }
        UIApplication.shared.requestSceneSessionActivation(
            nil, userActivity: activity, options: nil
        ) { error in
            print("[paper] new window refused: \(error.localizedDescription)")
        }
    }

    /// The target an "Open in New Window" activity carries.
    public static func windowTarget(from activity: NSUserActivity) -> PaperWindowTarget? {
        guard activity.activityType == windowActivityType,
            let json = activity.userInfo?["target"] as? String
        else { return nil }
        return try? JSONDecoder().decode(PaperWindowTarget.self, from: Data(json.utf8))
    }

    // MARK: - Shortcuts and toast

    /// The tab keys. Hidden buttons rather than key commands on the canvas,
    /// so they work at Home as well; the canvas claims none of them.
    private var shortcuts: some View {
        VStack(spacing: 0) {
            ForEach(1...9, id: \.self) { number in
                Button("Tab \(number)") { tabs.select(number: number) }
                    .keyboardShortcut(KeyEquivalent(Character("\(number)")), modifiers: .command)
            }
            Button("Close Tab") { tabs.closeActive() }
                .keyboardShortcut("w", modifiers: .command)
                .disabled(tabs.isHome)
            Button("Close All Tabs") { tabs.closeAll() }
                .keyboardShortcut("w", modifiers: [.command, .option])
                .disabled(tabs.ids.isEmpty)
            Button("Next Tab") { tabs.cycle(by: 1) }
                .keyboardShortcut(.tab, modifiers: .control)
            Button("Previous Tab") { tabs.cycle(by: -1) }
                .keyboardShortcut(.tab, modifiers: [.control, .shift])
            Button("Open") { tabs.goHomeToSearch() }
                .keyboardShortcut("o", modifiers: .command)
        }
        .opacity(0)
        .accessibilityHidden(true)
    }

    @ViewBuilder
    private var toast: some View {
        if let pending = tabs.pendingUndo {
            UndoToast(
                model: pending,
                onUndo: { tabs.undoTrash() },
                onExpire: { tabs.pendingUndo = nil })
            .padding(.bottom, 24)
            .id(pending.id)
        }
    }

    // MARK: - DEBUG

    /// The launch arguments that open a document. With any of them a window
    /// starts with no restored tabs, so a UI test starts from what it asked
    /// for rather than from whatever an earlier run left open.
    private static var debugOpensDocument: Bool {
        #if DEBUG
            let arguments = CommandLine.arguments
            return [
                "-debugNewNotebook", "-debugOpenNotebook", "-debugStroke", "-debugHandwriting",
                "-debugReveal", "-debugTabs", "-debugNewWhiteboard",
            ].contains(where: arguments.contains)
        #else
            return false
        #endif
    }

    #if DEBUG
        /// The simulator has no Pencil and `simctl` cannot tap, so the scripts
        /// get to a document the way a finger would have. Returns whether one
        /// was opened. These moved here from the library unchanged.
        private func runDebugDocumentArguments() -> Bool {
            let arguments = CommandLine.arguments
            let controller = tabs.controller
            func value(after flag: String) -> String? {
                guard let index = arguments.firstIndex(of: flag),
                    arguments.indices.contains(index + 1), !arguments[index + 1].hasPrefix("-")
                else { return nil }
                return arguments[index + 1]
            }
            var opened = false
            if arguments.contains("-debugNewNotebook") {
                // A notebook made the way the New notebook sheet makes one,
                // so a UI test starts from the real default rather than from
                // whatever an earlier run left in the container. With no size
                // it is still Ruled + Infinite, whatever new notebooks default
                // to: `InfiniteCanvasUITests` start here.
                let named = value(after: "-debugNewNotebook").flatMap(PaperSize.init(rawValue:))
                if let notebook = controller.createNotebook(
                    title: "UI test \(UUID().uuidString.prefix(8))", folder: "",
                    template: .ruled, size: named ?? .infinite)
                {
                    tabs.open(notebook)
                    opened = true
                }
            } else if arguments.contains("-debugNewWhiteboard") {
                // `-debugNewWhiteboard [grid|dotted|blank] [cream|white|dark|...]`:
                // a whiteboard as the New Whiteboard sheet makes one, Square
                // Grid on Cream unless named.
                var options = NewDocumentOptions.whiteboardFactory
                if let index = arguments.firstIndex(of: "-debugNewWhiteboard") {
                    for word in arguments.dropFirst(index + 1).prefix(2) where !word.hasPrefix("-") {
                        if let template = Template(rawValue: word) { options.template = template }
                        if let color = PaperColor(rawValue: word) { options.paperColor = color }
                    }
                }
                if let board = controller.createDocument(
                    title: "UI test \(UUID().uuidString.prefix(8))",
                    folderPath: PaperController.rootFolderPath, kind: .whiteboard, options: options)
                {
                    tabs.open(board)
                    opened = true
                }
            } else if let count = value(after: "-debugTabs").flatMap(Int.init), count > 0 {
                // `-debugTabs <n>`: the first n documents, each in a tab, made
                // up to n if there are fewer, and the first one showing.
                var notebooks = Array(controller.notebooks.prefix(count))
                while notebooks.count < count,
                    let made = controller.createNotebook(
                        title: "Tab \(notebooks.count + 1)", folder: "", template: .ruled,
                        size: .defaultForNewNotebooks)
                {
                    notebooks.append(made)
                }
                for notebook in notebooks { tabs.open(notebook) }
                if let first = notebooks.first { tabs.activate(first.id) }
                opened = !notebooks.isEmpty
            } else if tabs.isHome,
                arguments.contains("-debugOpenNotebook") || arguments.contains("-debugStroke")
                    || arguments.contains("-debugHandwriting"),
                let first = controller.notebooks.first
            {
                tabs.open(first)
                opened = true
            }
            if let query = value(after: "-debugReveal") {
                // Run that search, open the first hit at its page and hold the
                // outline open for a screenshot.
                HighlightLayer.duration = 120
                if let group = controller.search(query).first(where: { !$0.pages.isEmpty }),
                    let match = group.pages.first,
                    let notebook = controller.notebook(atPath: match.notebookPath)
                {
                    tabs.open(
                        notebook,
                        reveal: SearchDestination(
                            notebookPath: match.notebookPath, pageID: match.pageID,
                            box: match.unionBox))
                    opened = true
                }
            }
            if let session = tabs.session {
                // `-debugReadOnly`: the open tab in Read Only.
                if arguments.contains("-debugReadOnly") {
                    tabs.setReadOnly(true, for: session.notebook.id)
                }
                // `-debugSidebar [pages|outline|boards]`: the sidebar open.
                if arguments.contains("-debugSidebar") {
                    tabs.isSidebarVisible = true
                    if let tab = value(after: "-debugSidebar").flatMap(EditorSidebarTab.init(rawValue:)) {
                        tabs.sidebarTab = tab
                    }
                }
                // `-debugNewWindow`: the open document in a second window too.
                if arguments.contains("-debugNewWindow"),
                    UIApplication.shared.connectedScenes.count < 2
                {
                    let id = session.notebook.id
                    Task { @MainActor in
                        try? await Task.sleep(for: .seconds(1))
                        Self.requestWindow(.document(id: id, pageID: nil))
                    }
                }
            }
            return opened
        }
    #endif
}

// MARK: - Seams for the views inside the window

/// How a view inside the Paper window opens a document: in a tab of that
/// window, activating the tab it already has. A Quick Note is remembered as
/// one, so an untouched one goes away with its tab.
struct PaperOpenDocumentAction: Sendable {
    private let perform: @MainActor @Sendable (Notebook, SearchDestination?, Bool) -> Void

    init(perform: @escaping @MainActor @Sendable (Notebook, SearchDestination?, Bool) -> Void) {
        self.perform = perform
    }

    @MainActor init(tabs: DocumentTabsModel) {
        self.init { notebook, reveal, isQuickNote in
            tabs.open(notebook, reveal: reveal, isQuickNote: isQuickNote)
        }
    }

    @MainActor
    func callAsFunction(
        _ notebook: Notebook, reveal: SearchDestination? = nil, isQuickNote: Bool = false
    ) {
        perform(notebook, reveal, isQuickNote)
    }
}

private struct PaperOpenDocumentKey: EnvironmentKey {
    static let defaultValue = PaperOpenDocumentAction { _, _, _ in }
}

private struct PaperLibrarySearchRequestKey: EnvironmentKey {
    static let defaultValue = 0
}

private struct PaperLibraryFolderKey: EnvironmentKey {
    static let defaultValue: String? = nil
}

extension EnvironmentValues {
    /// Opens a document in a tab of this window.
    var paperOpenDocument: PaperOpenDocumentAction {
        get { self[PaperOpenDocumentKey.self] }
        set { self[PaperOpenDocumentKey.self] = newValue }
    }

    /// Changes when ⌘O asks the library to focus its search field.
    var paperLibrarySearchRequest: Int {
        get { self[PaperLibrarySearchRequestKey.self] }
        set { self[PaperLibrarySearchRequestKey.self] = newValue }
    }

    /// The folder a window opened with "Open in New Window" on a folder shows.
    var paperLibraryFolderID: String? {
        get { self[PaperLibraryFolderKey.self] }
        set { self[PaperLibraryFolderKey.self] = newValue }
    }
}
