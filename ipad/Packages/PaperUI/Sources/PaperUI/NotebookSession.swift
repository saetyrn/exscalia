import Observation
import PaperCore
import SwiftUI
import UIKit

/// Where an added page goes, relative to the page showing.
public enum PagePosition: Sendable {
    case before, after, last
}

/// What an export covers.
public enum ExportScope: Sendable {
    case currentPage, all
}

/// What the Add menu brings in.
public enum InsertKind: Sendable {
    case photo, camera, files, pdf, scan
}

/// A zoom the chrome asks for, as opposed to one a pinch makes.
public enum ZoomRequest: Equatable, Sendable {
    case fitWidth, fitContent, percent(Double), stepIn, stepOut
}

/// What the editor's chrome may ask of the canvas. The navigation bar and its
/// menus act only through this, so none of them touches PencilKit.
@MainActor public protocol EditorCommands: AnyObject {
    func undo()
    func redo()
    func addPage(_ position: PagePosition, template: Template?)
    func clearPage(at index: Int)
    func setPageStyle(template: Template?, paperColor: PaperColor?, at index: Int)
    func export(_ scope: ExportScope, from source: UIView?)
    func presentInsert(_ kind: InsertKind, from source: UIView?)
    func zoom(_ request: ZoomRequest)
    func askOMNIA(from source: UIView?)
    func setReadOnly(_ readOnly: Bool)
}

/// What the chrome shows about the canvas, kept current by the canvas.
@MainActor @Observable public final class EditorChromeState {
    public var canUndo = false
    public var canRedo = false
    public var isReadOnly = false
}

/// A live document: which page is showing, what the sidebar lists, and the
/// paged-mode switch from the More menu.
///
/// The UIKit canvas and the SwiftUI chrome both hold this, which is how the
/// sidebar can move a page and the scroll view can follow.
@MainActor
@Observable
public final class NotebookSession {
    public let controller: PaperController
    public let tools: ToolState
    public private(set) var notebook: Notebook
    public private(set) var pages: [Page] = []
    public var currentIndex: Int = 0
    /// The scroll view stops a viewport at a time. The canvas follows through
    /// `onPagedModeChanged`.
    public var isPagedMode = false {
        didSet { if isPagedMode != oldValue { onPagedModeChanged?() } }
    }
    public var zoom: CGFloat = 1

    /// Deleting a page shows the same six-second toast the library uses.
    public var pendingUndo: UndoToastModel?

    /// Set by the view controller: what the chrome acts through.
    @ObservationIgnored public weak var commands: EditorCommands? {
        didSet { commandsGeneration &+= 1 }
    }
    /// Bumped when `commands` is set, which is not observable itself (it is
    /// weak), so the chrome redraws once the canvas has loaded.
    private var commandsGeneration = 0

    /// Whether a canvas is attached to act on: what the chrome enables its
    /// canvas commands by.
    var hasCommands: Bool {
        _ = commandsGeneration
        return commands != nil
    }
    /// Undo, redo and read-only, for the chrome to show.
    public let chrome = EditorChromeState()

    /// Set by the view controller: writes the ink and the reading position
    /// straight away, for the moment another notebook takes this one's place.
    @ObservationIgnored public var onFlush: (() -> Void)?
    /// Set by the view controller.
    @ObservationIgnored public var onScrollTo: ((Int) -> Void)?
    @ObservationIgnored public var onPagesChanged: (() -> Void)?
    /// Set by the view controller.
    @ObservationIgnored public var onPagedModeChanged: (() -> Void)?
    /// Set by the view controller: bring a page's region into the middle of
    /// the viewport and flash the accent outline round it.
    @ObservationIgnored public var onReveal: ((Int, CGRect?) -> Void)?
    /// Held when a reveal arrives before the canvas has laid out -- opening a
    /// notebook straight onto a search result. The controller drains it after
    /// restoring the reading position, so the two do not fight over the
    /// scroll offset.
    @ObservationIgnored private var deferredReveal: (index: Int, box: CGRect?)?

    public init(
        controller: PaperController,
        notebook: Notebook,
        tools: ToolState = ToolState()
    ) {
        self.controller = controller
        self.notebook = notebook
        self.tools = tools
        reload()
        try? controller.store.markOpened(notebookID: notebook.id)
    }

    public func reload() {
        notebook = (try? controller.store.notebook(id: notebook.id)) ?? notebook
        pages = (try? controller.store.pages(ofNotebook: notebook.id)) ?? []
        currentIndex = min(currentIndex, max(pages.count - 1, 0))
    }

    /// Search is navigation: go to the page, and to the spot on it.
    public func reveal(pageID: String, box: CGRect?) {
        guard let index = pages.firstIndex(where: { $0.id == pageID }) else { return }
        currentIndex = index
        if let onReveal {
            onReveal(index, box)
        } else {
            deferredReveal = (index, box)
        }
    }

    public func consumeDeferredReveal() -> (index: Int, box: CGRect?)? {
        defer { deferredReveal = nil }
        return deferredReveal
    }

    public func ref(at index: Int) -> PageRef? {
        guard pages.indices.contains(index) else { return nil }
        return PageRef(
            notebookID: notebook.id, notebookPath: notebook.path, pageID: pages[index].id)
    }

    public func addPage(after index: Int?) {
        let after = index.flatMap { pages.indices.contains($0) ? pages[$0].id : nil }
        _ = try? controller.store.createPage(notebookID: notebook.id, after: after)
        reload()
        onPagesChanged?()
        if let after, let position = pages.firstIndex(where: { $0.id == after }) {
            currentIndex = min(position + 1, pages.count - 1)
        } else {
            currentIndex = max(pages.count - 1, 0)
        }
        onScrollTo?(currentIndex)
    }

    public func deletePage(at index: Int) {
        guard pages.indices.contains(index), pages.count > 1 else { return }
        let page = pages[index]
        let thumbnail = controller.store.paths.render(notebook.path, page.id)
        guard let token = try? controller.store.deletePage(notebookID: notebook.id, pageID: page.id)
        else { return }
        reload()
        onPagesChanged?()
        pendingUndo = UndoToastModel(
            message: "Page \(index + 1) deleted", token: token,
            thumbnailURL: FileManager.default.fileExists(
                atPath: thumbnail.path(percentEncoded: false)) ? thumbnail : nil)
    }

    public func movePage(from source: Int, to destination: Int) {
        guard pages.indices.contains(source) else { return }
        var order = pages.map(\.id)
        let moved = order.remove(at: source)
        order.insert(moved, at: min(max(destination, 0), order.count))
        try? controller.store.reorderPages(notebookID: notebook.id, order: order)
        reload()
        onPagesChanged?()
    }

    /// A copy of the page, right after it, which then shows.
    public func duplicatePage(at index: Int) {
        guard pages.indices.contains(index) else { return }
        // The copy is read from disk: ink still in the canvas -- the last
        // second's strokes -- is written first, or the copy goes without it.
        controller.autosave.flushAll()
        guard let copy = try? controller.store.duplicatePage(
                notebookID: notebook.id, pageID: pages[index].id)
        else { return }
        pagesChangedHere()
        if let position = pages.firstIndex(where: { $0.id == copy.id }) {
            currentIndex = position
            onScrollTo?(position)
        }
    }

    /// A page's title, which is what the Outline lists; empty removes it.
    public func setTitle(_ title: String, ofPageAt index: Int) {
        guard pages.indices.contains(index) else { return }
        controller.setPageTitle(title, notebookPath: notebook.path, pageID: pages[index].id)
        pagesChangedHere()
    }

    public func setBookmarked(_ bookmarked: Bool, pageAt index: Int) {
        guard pages.indices.contains(index) else { return }
        controller.setPageBookmarked(
            bookmarked, notebookPath: notebook.path, pageID: pages[index].id)
        pagesChangedHere()
    }

    /// Shows a page, the way a tap on its thumbnail does.
    public func show(pageAt index: Int) {
        guard pages.indices.contains(index) else { return }
        currentIndex = index
        onScrollTo?(index)
    }

    private func pagesChangedHere() {
        reload()
        onPagesChanged?()
        if let sync = controller.sync { Task { await sync.kick() } }
    }

    public func undoPendingDelete() {
        guard let pending = pendingUndo else { return }
        controller.restore(pending.token)
        pendingUndo = nil
        reload()
        onPagesChanged?()
    }

    /// Saves the reading position: the page showing, the zoom, and `offset`,
    /// the viewport's top-left in page points from that page's top-left. The
    /// canvas calls it on a 500ms debounce, and once more when the document
    /// is left -- another tab, Home, or the window going away.
    public func persistViewState(offset: CGPoint) {
        let state = NotebookViewState(
            lastPageID: pages.indices.contains(currentIndex) ? pages[currentIndex].id : nil,
            zoom: zoom, offset: offset)
        try? controller.store.setViewState(state, notebookID: notebook.id)
    }
}

/// What a six-second undo toast needs to know.
public struct UndoToastModel: Identifiable, Equatable {
    public let id = UUID()
    public let message: String
    public let token: TrashToken
    public var thumbnailURL: URL?
    /// A document trashed from its tab, which Undo opens again.
    public var reopensNotebookID: String?

    public init(
        message: String, token: TrashToken, thumbnailURL: URL? = nil,
        reopensNotebookID: String? = nil
    ) {
        self.message = message
        self.token = token
        self.thumbnailURL = thumbnailURL
        self.reopensNotebookID = reopensNotebookID
    }
}

/// The six-second undo. Persistent under VoiceOver, because six seconds is not
/// enough time to find a toast you cannot see.
struct UndoToast: View {
    let model: UndoToastModel
    let onUndo: () -> Void
    let onExpire: () -> Void

    @Environment(\.accessibilityVoiceOverEnabled) private var voiceOverEnabled

    var body: some View {
        HStack(spacing: 12) {
            if let url = model.thumbnailURL, let image = UIImage(contentsOfFile: url.path(percentEncoded: false)) {
                Image(uiImage: image)
                    .resizable()
                    .aspectRatio(contentMode: .fill)
                    .frame(width: 28, height: 38)
                    .clipShape(RoundedRectangle(cornerRadius: 2))
                    .accessibilityHidden(true)
            }
            Text(model.message)
                .font(.subheadline)
                .foregroundStyle(PaperTokens.text)
            Button("Undo", action: onUndo)
                .font(.subheadline.weight(.medium))
                .buttonStyle(.plain)
                .foregroundStyle(PaperTokens.accent)
                .frame(minWidth: PaperTokens.minimumTarget, minHeight: PaperTokens.minimumTarget)
                .accessibilityHint("Restores what was just deleted")
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
        .background(Capsule(style: .continuous).fill(PaperTokens.panel))
        .overlay(Capsule(style: .continuous).strokeBorder(PaperTokens.border, lineWidth: 1))
        .shadow(color: .black.opacity(0.12), radius: 10, y: 4)
        .task {
            guard !voiceOverEnabled else { return }
            try? await Task.sleep(for: .seconds(6))
            onExpire()
        }
    }
}
