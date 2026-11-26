import Observation
import PaperCore
import PencilKit
import UIKit

/// Which list the editor's sidebar shows.
enum EditorSidebarTab: String, CaseIterable, Sendable {
    case pages, outline, boards

    var title: String {
        switch self {
        case .pages: return "Pages"
        case .outline: return "Outline"
        case .boards: return "Boards"
        }
    }

    /// Pages and Outline on a notebook, Boards on a whiteboard.
    static func tabs(for kind: DocumentKind) -> [EditorSidebarTab] {
        kind == .whiteboard ? [.boards] : [.pages, .outline]
    }
}

/// The documents open in one window, as tabs, and the one editor that is live.
///
/// Every window has its own: two windows may show different documents, or the
/// same one twice. Only the active tab has a `NotebookSession`; switching
/// flushes the one being left and builds the next, so the tab strip costs
/// nothing per tab beyond an id. The window's `ToolState` is the controller's,
/// shared by every session.
@MainActor
@Observable
final class DocumentTabsModel {
    /// What `@SceneStorage("paper.window")` keeps, as JSON.
    struct Stored: Codable, Equatable {
        var tabs: [String]
        var active: String?
        var sidebar: Bool
        var sidebarTab: String
    }

    let controller: PaperController

    /// Open documents, by notebook id, left to right.
    private(set) var ids: [String] = []
    /// The tab showing; nil at Home, where the library is.
    private(set) var activeID: String?
    /// The active tab's session. Nil at Home.
    private(set) var session: NotebookSession?
    var isSidebarVisible = false
    var sidebarTab: EditorSidebarTab = .pages
    /// Read Only, per tab, for as long as the app runs.
    private(set) var readOnlyIDs: Set<String> = []
    /// Quick Notes opened in this window, with the title each was made with.
    /// In memory only: a Quick Note left by a killed app stays a notebook.
    private(set) var quickNotes: [String: String] = [:]
    /// The undo toast for a document trashed from its tab.
    var pendingUndo: UndoToastModel?
    /// Bumped by ⌘O, for the library to focus its search field.
    private(set) var librarySearchRequest = 0

    /// A document asked for while it was being converted, shown once the
    /// conversion is done.
    @ObservationIgnored private var heldActivation: String?

    init(controller: PaperController) {
        self.controller = controller
        controller.addWindow(self)
    }

    var isHome: Bool { activeID == nil }

    func isReadOnly(_ id: String) -> Bool { readOnlyIDs.contains(id) }

    func isQuickNote(_ id: String) -> Bool { quickNotes[id] != nil }

    /// A tab's title: the notebook's, as the library has it.
    func title(of id: String) -> String {
        if let notebook = controller.notebooks.first(where: { $0.id == id }) { return notebook.title }
        return (try? controller.store.notebook(id: id).title) ?? ""
    }

    func kind(of id: String) -> DocumentKind {
        controller.notebooks.first(where: { $0.id == id })?.kind
            ?? (try? controller.store.notebook(id: id).kind) ?? .notebook
    }

    // MARK: - Opening and switching

    /// Opens a document in a tab, or activates the tab it already has. A
    /// search result or a link carries the spot to show.
    func open(_ notebook: Notebook, reveal: SearchDestination? = nil, isQuickNote: Bool = false) {
        if !ids.contains(notebook.id) { ids.append(notebook.id) }
        if isQuickNote { quickNotes[notebook.id] = notebook.title }
        activate(notebook.id)
        if let reveal, reveal.notebookPath == session?.notebook.path || reveal.notebookPath.isEmpty,
            let pageID = reveal.pageID
        {
            session?.reveal(pageID: pageID, box: reveal.box)
        }
    }

    /// Shows a tab. The document being left is written first -- its ink and
    /// where it was being read -- and then a new session takes its place.
    func activate(_ id: String) {
        guard ids.contains(id) else { return }
        if activeID == id, session?.notebook.id == id { return }
        // A canvas on a document being converted would write its old ink
        // back over the boards; it opens once they are done.
        guard !controller.isConverting(id) else {
            heldActivation = id
            return
        }
        leaveSession()
        guard let notebook = try? controller.store.notebook(id: id) else {
            activeID = nil
            close([id])
            return
        }
        let next = NotebookSession(controller: controller, notebook: notebook, tools: controller.tools)
        next.chrome.isReadOnly = readOnlyIDs.contains(id)
        if EditorSidebarTab.tabs(for: notebook.kind).contains(sidebarTab) == false {
            sidebarTab = EditorSidebarTab.tabs(for: notebook.kind)[0]
        }
        session = next
        activeID = id
        // Opening it made it the most recent; the library's Recents follows.
        controller.refresh()
        UIAccessibility.post(notification: .screenChanged, argument: notebook.title)
    }

    /// Home: the library, with every tab kept. A Quick Note nobody wrote in
    /// goes away on the way.
    func goHome() {
        leaveSession()
        activeID = nil
        let untouched = quickNotes.keys.filter(isUnchangedQuickNote)
        if !untouched.isEmpty { close(untouched) }
    }

    /// ⌘1 to ⌘9: the tab at that position, counting from 1.
    func select(number: Int) {
        guard number >= 1, ids.indices.contains(number - 1) else { return }
        activate(ids[number - 1])
    }

    /// ⌃Tab and ⌃⇧Tab, wrapping round. From Home the first step lands on the
    /// first or the last tab.
    func cycle(by step: Int) {
        guard !ids.isEmpty else { return }
        guard let activeID, let index = ids.firstIndex(of: activeID) else {
            activate(step >= 0 ? ids[0] : ids[ids.count - 1])
            return
        }
        activate(ids[((index + step) % ids.count + ids.count) % ids.count])
    }

    /// ⌘O: Home, with the library's search field focused.
    func goHomeToSearch() {
        goHome()
        librarySearchRequest += 1
    }

    // MARK: - Closing

    func close(_ id: String) { close([id]) }

    func closeOthers(keeping id: String) { close(ids.filter { $0 != id }) }

    /// ⌥⌘W: every tab, and Home.
    func closeAll() {
        close(ids)
        goHome()
    }

    func closeActive() {
        guard let activeID else { return }
        close([activeID])
    }

    /// Takes tabs away. Closing the active tab shows the one that took its
    /// place, else the one before it, else Home. A Quick Note still as it was
    /// made is deleted silently once its tab has gone.
    func close(_ closing: [String]) {
        let closing = Set(closing).intersection(ids)
        guard !closing.isEmpty else { return }
        var next: String?
        let closesActive = activeID.map(closing.contains) ?? false
        if closesActive, let activeID, let index = ids.firstIndex(of: activeID) {
            let kept = ids.enumerated().filter { !closing.contains($0.element) }
            next = (kept.first { $0.offset > index } ?? kept.last { $0.offset < index })?.element
            leaveSession()
            self.activeID = nil
        }
        ids.removeAll(where: closing.contains)
        readOnlyIDs.subtract(closing)
        let quick = closing.filter { quickNotes[$0] != nil }
        for id in quick where isUnchangedQuickNote(id) {
            if let notebook = try? controller.store.notebook(id: id) { _ = controller.delete(notebook) }
        }
        for id in closing { quickNotes[id] = nil }
        if closesActive {
            if let next { activate(next) } else { goHome() }
        }
    }

    /// Closes the tabs of documents that no longer exist: trashed or deleted
    /// here, in another window or on another device.
    func closeMissing() {
        let known = Set(controller.notebooks.map(\.id))
        let gone = ids.filter { id in
            !known.contains(id) && (try? controller.store.notebook(id: id)) == nil
        }
        close(gone)
    }

    /// Move to Trash from a tab's menu: the tab closes and the toast offers
    /// the document back.
    func trash(_ id: String) {
        guard let notebook = try? controller.store.notebook(id: id) else {
            close([id])
            return
        }
        close([id])
        controller.autosave.flushAll()
        guard let token = controller.delete(notebook) else { return }
        pendingUndo = UndoToastModel(
            message: "\(notebook.kind.displayName) deleted", token: token,
            reopensNotebookID: notebook.id)
    }

    /// The toast's Undo: the document comes back, in its tab.
    func undoTrash() {
        guard let pending = pendingUndo else { return }
        pendingUndo = nil
        controller.restore(pending.token)
        if let id = pending.reopensNotebookID, let notebook = try? controller.store.notebook(id: id) {
            open(notebook)
        }
    }

    // MARK: - Per tab

    /// Read Only for one tab. The canvas is told when the tab is showing.
    func setReadOnly(_ readOnly: Bool, for id: String) {
        if readOnly { readOnlyIDs.insert(id) } else { readOnlyIDs.remove(id) }
        guard let session, session.notebook.id == id else { return }
        if let commands = session.commands {
            commands.setReadOnly(readOnly)
        } else {
            session.chrome.isReadOnly = readOnly
        }
    }

    /// Another device changed documents. The open one follows: gone means its
    /// tab closes; moved (a folder rename elsewhere) means the canvas is
    /// rebuilt on the new path, which `EditorScreen` keys it by.
    func remoteChange(_ paths: Set<String>) {
        guard let session, paths.contains(session.notebook.path) else { return }
        guard (try? controller.store.notebook(id: session.notebook.id)) != nil else {
            close([session.notebook.id])
            return
        }
        let path = session.notebook.path
        session.reload()
        if session.notebook.path == path { session.onPagesChanged?() }
    }

    /// Convert to Whiteboard is about to rewrite this document: a window
    /// showing it goes Home, which writes its ink and reading position and
    /// takes its canvas down. True when it was showing it.
    func takeDown(_ id: String) -> Bool {
        guard activeID == id || session?.notebook.id == id else { return false }
        goHome()
        heldActivation = id
        return true
    }

    /// The conversion is over: the document opens again where it was
    /// showing, or where it was asked for meanwhile, unless the window has
    /// moved on.
    func bringBack(_ id: String, wasShowing: Bool) {
        let asked = heldActivation == id
        if asked { heldActivation = nil }
        guard wasShowing || asked, isHome, ids.contains(id) else { return }
        activate(id)
    }

    private func leaveSession() {
        guard let session else { return }
        session.onFlush?()
        controller.autosave.flushAll()
        self.session = nil
    }

    /// One page, no strokes, no objects, and the title it was made with.
    private func isUnchangedQuickNote(_ id: String) -> Bool {
        guard let title = quickNotes[id] else { return false }
        controller.autosave.flushAll()
        guard let notebook = try? controller.store.notebook(id: id), notebook.title == title,
            notebook.pages.count == 1, let pageID = notebook.pages.first,
            let page = try? controller.store.page(notebookPath: notebook.path, id: pageID),
            page.objects.isEmpty
        else { return false }
        guard let data = controller.store.drawingData(notebookPath: notebook.path, pageID: pageID),
            !data.isEmpty
        else { return true }
        return (try? PKDrawing(data: data))?.strokes.isEmpty == true
    }

    // MARK: - Persistence

    var stored: Stored {
        Stored(
            tabs: ids, active: activeID, sidebar: isSidebarVisible, sidebarTab: sidebarTab.rawValue)
    }

    var storedJSON: String {
        guard let data = try? JSONEncoder().encode(stored) else { return "" }
        return String(decoding: data, as: UTF8.self)
    }

    /// Brings back what a window had open. Documents that have gone since are
    /// dropped; only the active tab gets an editor.
    func restore(json: String) {
        guard let data = json.data(using: .utf8),
            let stored = try? JSONDecoder().decode(Stored.self, from: data)
        else { return }
        var seen = Set<String>()
        ids = stored.tabs.filter { id in
            seen.insert(id).inserted && (try? controller.store.notebook(id: id)) != nil
        }
        isSidebarVisible = stored.sidebar
        sidebarTab = EditorSidebarTab(rawValue: stored.sidebarTab) ?? .pages
        if let active = stored.active, ids.contains(active) {
            activate(active)
        }
    }
}
