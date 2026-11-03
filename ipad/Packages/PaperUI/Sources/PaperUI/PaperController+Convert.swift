import PaperCore
import SwiftUI
import UIKit

// MARK: - The controller's half

extension PaperController {
    /// What a document's Convert to Whiteboard entry says instead of its name
    /// when the document cannot be converted; nil when it can. Measures every
    /// page and its ink off the main actor and remembers the answer until the
    /// document changes.
    func whiteboardConversionRefusal(for notebook: Notebook) async -> String? {
        let key = Self.conversionCheckKey(notebook)
        if let known = Self.conversionChecks[key] { return known }
        let store = store
        let id = notebook.id
        let refusal: String? = await Task.detached(priority: .userInitiated) {
            do {
                _ = try store.planWhiteboardConversion(notebookID: id)
                return nil
            } catch {
                return PaperController.conversionRefusal(error)
            }
        }.value
        Self.conversionChecks[key] = .some(refusal)
        return refusal
    }

    /// The answer a check already gave for the document as it is now.
    func knownWhiteboardConversionRefusal(for notebook: Notebook) -> String? {
        Self.conversionChecks[Self.conversionCheckKey(notebook)] ?? nil
    }

    /// The one line a refusal is shown as.
    nonisolated static func conversionRefusal(_ error: Error) -> String {
        switch error {
        case WhiteboardConversionError.tooLarge: return "Too large to convert"
        // A PDF page's document: the one `notFound` a person can act on.
        case PaperStoreError.notFound(let missing) where missing.contains("/assets/"):
            return "PDF missing on this iPad"
        default: return "Cannot convert"
        }
    }

    /// Converts a notebook into a whiteboard and returns nil, or the line that
    /// says why it was left as it was.
    ///
    /// Every window showing the document goes Home first, as a window does
    /// when another tab is chosen: its ink and reading position are written
    /// and its canvas comes down, because a canvas still holding the old ink
    /// would write it back over the boards at the old coordinates. Nothing
    /// opens the document again until the conversion is over. Then a sync
    /// runs (for at most `syncWait`) so the newest pages are the ones
    /// converted, the store converts off the main actor, the windows that
    /// were showing it open it again on the whiteboard, and every board is
    /// drawn again -- its render, its cover, its reading by Vision -- and
    /// queued for the server.
    func convertToWhiteboard(_ notebook: Notebook, syncWait: Duration = .seconds(8)) async
        -> String?
    {
        let id = notebook.id
        Self.hold(id)
        let showing = Self.windows(of: self).filter { $0.takeDown(id) }
        let refusal = await convertTakenDown(notebook, syncWait: syncWait)
        Self.unhold(id)
        for window in Self.windows(of: self) {
            window.bringBack(id, wasShowing: showing.contains { $0 === window })
        }
        return refusal
    }

    private func convertTakenDown(_ notebook: Notebook, syncWait: Duration) async -> String? {
        autosave.flushAll()
        if let sync { await Self.waitAtMost(syncWait) { await sync.syncNow() } }
        // From here no pull lands -- one still out from the sync above gives
        // up at its next step -- and no push starts, until the boards are in.
        await sync?.hold()
        let sync = sync
        defer { if let sync { Task { await sync.release() } } }
        // Anything written during the sync's wait.
        autosave.flushAll()
        let store = store
        let id = notebook.id
        // A reading of a page is about to describe ink that has moved.
        if let recognizer {
            let pages = notebook.pages
            Task.detached(priority: .utility) {
                for pageID in pages { await recognizer.cancel(pageID: pageID) }
            }
        }
        let result: Result<Notebook, Error> = await Task.detached(priority: .userInitiated) {
            Result { try store.convertToWhiteboard(notebookID: id) }
        }.value
        let converted: Notebook
        switch result {
        case .failure(let error):
            refresh()
            // A refusal changed nothing; a write that failed half way left
            // whole pages, some of them boards, which other windows reload.
            if !(error is WhiteboardConversionError) { notifyRemoteChange([notebook.path]) }
            return Self.conversionRefusal(error)
        case .success(let value):
            converted = value
        }
        Self.conversionChecks = Self.conversionChecks.filter { !$0.key.hasPrefix("\(id)@") }

        // The thumbnail is board 1's render. It is drawn here, before the
        // library hears of the change -- its row loads the cover when the
        // document's date moves -- and the rest straight after.
        if let first = converted.pages.first,
            let page = try? store.page(notebookPath: converted.path, id: first)
        {
            await renderer.render(
                PageRenderJob(
                    notebookPath: converted.path, page: page,
                    drawing: store.drawingData(notebookPath: converted.path, pageID: first)))
            _ = try? store.journal.append(
                .init(kind: .pageRender, notebookPath: converted.path, pageID: first))
            if let recognizer,
                let drawn = try? store.page(notebookPath: converted.path, id: first)
            {
                let job = RecognitionJob(notebookPath: converted.path, page: drawn)
                Task.detached(priority: .background) { await recognizer.schedule(job) }
            }
        }
        refresh()
        // No window holds a canvas on it: they open it again afterwards, on
        // the whiteboard and at the reading position the conversion wrote.
        notifyRemoteChange([converted.path])

        // Every other board, without being opened: until each is drawn and
        // sent, the server shows its sheet's picture at the board's size.
        renderOwedBoards(of: [id])
        if let sync { Task { await sync.kick() } }
        return nil
    }

    /// Draws every board that has no render -- in these documents, or in
    /// every whiteboard -- one after another off the main actor, and queues
    /// each picture for the server and the board for Vision, as autosave
    /// does for a board that has rested. A board without a render is owed
    /// one whether this launch converted it or one killed half way did, so
    /// the launch reconcile calls this too.
    func renderOwedBoards(of notebookIDs: [String]? = nil) {
        let store = store
        let renderer = renderer
        let box = WeakPaperController(self)
        Task.detached(priority: .utility) {
            for ref in store.boardsOwingRenders(notebookIDs: notebookIDs) {
                guard let page = try? store.page(notebookPath: ref.notebookPath, id: ref.pageID)
                else { continue }
                await renderer.render(
                    PageRenderJob(
                        notebookPath: ref.notebookPath, page: page,
                        drawing: store.drawingData(notebookPath: ref.notebookPath, pageID: ref.pageID)))
                guard
                    FileManager.default.fileExists(
                        atPath: store.paths.render(ref.notebookPath, ref.pageID).path(percentEncoded: false))
                else { continue }
                await MainActor.run { box.value?.renderDidFinish(ref) }
            }
        }
    }

    // MARK: Windows

    /// A window's tabs, which a conversion takes down and brings back. Held
    /// weakly: a window that goes away leaves on its own.
    func addWindow(_ tabs: DocumentTabsModel) {
        let key = ObjectIdentifier(self)
        let table = Self.windowTables[key] ?? NSHashTable<DocumentTabsModel>.weakObjects()
        table.add(tabs)
        Self.windowTables[key] = table
    }

    /// Whether the document is being converted, or a launch has still to
    /// finish its conversion: nothing opens it meanwhile.
    func isConverting(_ id: String) -> Bool { (Self.converting[id] ?? 0) > 0 }

    // MARK: Launch

    /// Keeps every editor off the documents a conversion a kill stopped may
    /// have left half done, from before the first window restores its tabs
    /// -- at the controller's birth, from the index alone -- until the launch
    /// reconcile has finished them (`endLaunchConversions`). A window that
    /// asks for one meanwhile, by tab restore, the library or Recents, gets
    /// it then. Any found only once the reconcile has read the tree are held
    /// here too, and a window already showing one goes Home first.
    func holdLaunchConversions(_ ids: [String]) {
        let key = ObjectIdentifier(self)
        var held = Self.launchHeld[key] ?? []
        for id in ids where !held.contains(id) {
            held.insert(id)
            Self.hold(id)
            for window in Self.windows(of: self) { _ = window.takeDown(id) }
        }
        Self.launchHeld[key] = held
    }

    /// The launch reconcile is done with them: every window is told, and the
    /// ones that asked for one, or were showing it, open it again.
    func endLaunchConversions() {
        let key = ObjectIdentifier(self)
        let held = Self.launchHeld.removeValue(forKey: key) ?? []
        guard !held.isEmpty else { return }
        for id in held { Self.unhold(id) }
        let paths = Set(held.compactMap { try? store.notebook(id: $0).path })
        refresh()
        notifyRemoteChange(paths)
        for id in held.sorted() {
            for window in Self.windows(of: self) { window.bringBack(id, wasShowing: false) }
        }
    }

    private static func hold(_ id: String) { converting[id, default: 0] += 1 }

    private static func unhold(_ id: String) {
        let count = (converting[id] ?? 0) - 1
        converting[id] = count > 0 ? count : nil
    }

    private static func windows(of controller: PaperController) -> [DocumentTabsModel] {
        windowTables[ObjectIdentifier(controller)]?.allObjects ?? []
    }

    @MainActor private static var windowTables: [ObjectIdentifier: NSHashTable<DocumentTabsModel>] = [:]
    /// Holds by document: a conversion from a menu, the launch's.
    @MainActor private static var converting: [String: Int] = [:]
    @MainActor private static var launchHeld: [ObjectIdentifier: Set<String>] = [:]

    // MARK: Pieces

    /// Check results by document and the date it last changed.
    @MainActor private static var conversionChecks: [String: String?] = [:]

    private static func conversionCheckKey(_ notebook: Notebook) -> String {
        "\(notebook.id)@\(notebook.modified.timeIntervalSince1970)"
    }

    /// Runs `work`, returning when it ends or after `limit`, whichever is
    /// first. The work is not cancelled: a sync that outlasts the wait goes on.
    private static func waitAtMost(_ limit: Duration, _ work: @escaping @Sendable () async -> Void)
        async
    {
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            let once = ResumeOnce(continuation)
            Task {
                await work()
                once.resume()
            }
            Task {
                try? await Task.sleep(for: limit)
                once.resume()
            }
        }
    }
}

/// Resumes a continuation the first time it is asked to, and never again.
private final class ResumeOnce: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<Void, Never>?

    init(_ continuation: CheckedContinuation<Void, Never>) {
        self.continuation = continuation
    }

    func resume() {
        lock.lock()
        let pending = continuation
        continuation = nil
        lock.unlock()
        pending?.resume()
    }
}

// MARK: - The window's half

/// Convert to Whiteboard for the menus of one window: the tab menu, the
/// library's item menu and its long-press menu.
///
/// Every entry asks the same question and takes the same path. A document
/// open in the window's active tab has its canvas taken down first -- its
/// ink and reading position written as when another tab is chosen -- because
/// a canvas still holding the old ink would write it back at the old place;
/// the tab then opens again on the whiteboard.
struct PaperConvertDocumentAction: Sendable {
    fileprivate let tabs: DocumentTabsModel?

    init(tabs: DocumentTabsModel?) {
        self.tabs = tabs
    }

    /// The confirmation's title, and its one sentence.
    static let question = "Convert to Whiteboard?"
    static let consequence = "Every page becomes a board, with its ink, objects and history."

    /// Whether a menu offers the entry at all: on a notebook, and on a
    /// whiteboard that still holds a page that is not a board -- one a
    /// conversion did not reach, or another device's sheet kept as the
    /// original of a conflict -- so no document is left half converted.
    @MainActor static func isOffered(_ notebook: Notebook, store: PaperStore) -> Bool {
        notebook.kind == .notebook || store.holdsSheet(notebookID: notebook.id)
    }

    @MainActor func isOffered(_ notebook: Notebook) -> Bool {
        guard let tabs else { return false }
        return Self.isOffered(notebook, store: tabs.controller.store)
    }

    /// Whether the entry is off before anything is measured: in a tab that is
    /// Read Only (the tab's own menu only), or for a Quick Note still as it
    /// was made, which goes away with its tab.
    @MainActor func isBlocked(_ notebook: Notebook, inTab: Bool) -> Bool {
        guard let tabs, Self.isOffered(notebook, store: tabs.controller.store) else { return true }
        if inTab, tabs.isReadOnly(notebook.id) { return true }
        return isUntouchedQuickNote(notebook, in: tabs)
    }

    @MainActor func knownRefusal(_ notebook: Notebook) -> String? {
        tabs?.controller.knownWhiteboardConversionRefusal(for: notebook)
    }

    @MainActor func check(_ notebook: Notebook) async -> String? {
        await tabs?.controller.whiteboardConversionRefusal(for: notebook)
    }

    /// The entry was chosen. Once the menu has gone (`after`), says why the
    /// document cannot be converted, or asks, and converts on Convert.
    @MainActor func ask(_ notebook: Notebook, after delay: Duration = .milliseconds(450)) {
        guard let tabs else { return }
        Task { @MainActor in
            try? await Task.sleep(for: delay)
            if let refusal = await tabs.controller.whiteboardConversionRefusal(for: notebook) {
                ConvertAlert.present(title: refusal, actions: [("OK", .cancel, {})])
                return
            }
            ConvertAlert.present(
                title: Self.question, message: Self.consequence,
                actions: [
                    ("Cancel", .cancel, {}),
                    ("Convert", .default, { Task { @MainActor in await convert(notebook) } }),
                ])
        }
    }

    /// Converts without asking: what Convert does, and `-debugConvert`.
    @MainActor func convert(_ notebook: Notebook) async {
        guard let tabs else { return }
        // Every window showing it, this one included, goes Home for the
        // conversion and comes back to it on the whiteboard.
        let refusal = await tabs.controller.convertToWhiteboard(notebook)
        if let refusal {
            ConvertAlert.present(title: refusal, actions: [("OK", .cancel, {})])
        } else {
            UIAccessibility.post(notification: .announcement, argument: "Converted")
        }
    }

    /// `DocumentTabsModel.isUnchangedQuickNote`, read-only: one page, no
    /// strokes, no objects, its first title, and nothing waiting to be saved.
    @MainActor private func isUntouchedQuickNote(_ notebook: Notebook, in tabs: DocumentTabsModel) -> Bool {
        guard let title = tabs.quickNotes[notebook.id], notebook.title == title,
            notebook.pages.count == 1, let pageID = notebook.pages.first,
            !tabs.controller.autosave.hasUnsavedChanges
        else { return false }
        let store = tabs.controller.store
        guard let page = try? store.page(notebookPath: notebook.path, id: pageID),
            page.objects.isEmpty
        else { return false }
        guard let ink = store.drawingData(notebookPath: notebook.path, pageID: pageID), !ink.isEmpty
        else { return true }
        return WhiteboardConversion.inkBounds(of: ink) == nil
    }
}

private struct PaperConvertDocumentKey: EnvironmentKey {
    static let defaultValue = PaperConvertDocumentAction(tabs: nil)
}

extension EnvironmentValues {
    /// Converts a document of this window into a whiteboard.
    var paperConvertDocument: PaperConvertDocumentAction {
        get { self[PaperConvertDocumentKey.self] }
        set { self[PaperConvertDocumentKey.self] = newValue }
    }
}

// MARK: - Entries

/// "Convert to Whiteboard" as a row of a document's popover menu, drawn like
/// the rows beside it. Off in a Read Only tab and for an untouched Quick
/// Note; once measured, a document that cannot be converted shows why in
/// place of the name.
struct ConvertToWhiteboardRow: View {
    let notebook: Notebook
    /// The tab's own menu: Read Only there turns the entry off.
    var inTab = false
    /// Closes the menu.
    let onDone: () -> Void

    @Environment(\.paperConvertDocument) private var action
    @State private var refusal: String?

    var body: some View {
        let title = refusal ?? "Convert to Whiteboard"
        let disabled = refusal != nil || action.isBlocked(notebook, inTab: inTab)
        Button {
            onDone()
            action.ask(notebook)
        } label: {
            HStack(spacing: 12) {
                Text(title)
                Spacer(minLength: 0)
                Image(systemName: "rectangle.dashed")
                    .symbolRenderingMode(.monochrome)
                    .frame(width: 22)
                    .accessibilityHidden(true)
            }
            .font(.body)
            .foregroundStyle(PaperTokens.text)
            .padding(.horizontal, 16)
            .frame(height: PaperTokens.minimumTarget)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(disabled)
        .opacity(disabled ? 0.4 : 1)
        .accessibilityLabel(title)
        .accessibilityIdentifier("convertToWhiteboard")
        .onAppear { refusal = action.knownRefusal(notebook) }
        .task(id: notebook.modified) { refusal = await action.check(notebook) }
    }
}

/// The same entry in a long-press menu, which cannot wait for a measurement:
/// it shows what an earlier check found, and a choice is checked again
/// before anything is asked.
struct ConvertToWhiteboardMenuButton: View {
    let notebook: Notebook

    @Environment(\.paperConvertDocument) private var action

    var body: some View {
        let refusal = action.knownRefusal(notebook)
        Button {
            action.ask(notebook)
        } label: {
            Label(refusal ?? "Convert to Whiteboard", systemImage: "rectangle.dashed")
        }
        .disabled(refusal != nil || action.isBlocked(notebook, inTab: false))
    }
}

// MARK: - Alerts

/// The question and the refusals, presented over whatever the key window
/// shows: the menu that asked has gone by then.
@MainActor
enum ConvertAlert {
    static func present(
        title: String, message: String? = nil,
        actions: [(String, UIAlertAction.Style, @MainActor () -> Void)]
    ) {
        guard let presenter = topViewController() else { return }
        let alert = UIAlertController(title: title, message: message, preferredStyle: .alert)
        for (name, style, handler) in actions {
            alert.addAction(UIAlertAction(title: name, style: style) { _ in handler() })
        }
        presenter.present(alert, animated: !UIAccessibility.isReduceMotionEnabled)
    }

    private static func topViewController() -> UIViewController? {
        let scenes = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
        let window =
            scenes.filter { $0.activationState == .foregroundActive }.flatMap(\.windows)
            .first(where: \.isKeyWindow)
            ?? scenes.flatMap(\.windows).first(where: \.isKeyWindow)
        var top = window?.rootViewController
        while let presented = top?.presentedViewController, !presented.isBeingDismissed {
            top = presented
        }
        return top
    }
}

// MARK: - DEBUG

#if DEBUG
    /// `-debugConvert [seconds]`: converts the document open in the active
    /// tab, without asking, after that many seconds (2 by default), through
    /// the path Convert takes. Once per launch.
    @MainActor
    enum DebugConvert {
        private static var hasRun = false

        /// Started by the tab strip. Runs in a task of its own: going Home
        /// takes the strip, and anything tied to it, away half way.
        static func runIfRequested(tabs: DocumentTabsModel) {
            let arguments = CommandLine.arguments
            guard !hasRun, let index = arguments.firstIndex(of: "-debugConvert") else { return }
            hasRun = true
            let seconds =
                arguments.indices.contains(index + 1) ? Double(arguments[index + 1]) ?? 2 : 2
            Task { @MainActor in
                try? await Task.sleep(for: .seconds(seconds))
                guard let id = tabs.activeID,
                    let notebook = try? tabs.controller.store.notebook(id: id)
                else { return }
                print("[paper] -debugConvert: converting \(notebook.title)")
                await PaperConvertDocumentAction(tabs: tabs).convert(notebook)
                let kind = (try? tabs.controller.store.notebook(id: id).kind.rawValue) ?? "gone"
                print("[paper] -debugConvert: \(notebook.title) is a \(kind)")
            }
        }
    }
#endif
