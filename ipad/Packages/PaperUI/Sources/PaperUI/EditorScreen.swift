import PaperCore
import SwiftUI
import UIKit

/// The editor of one window, top to bottom: the tab strip, the navigation bar
/// and the canvas, with the sidebar beside the canvas when it is open and the
/// undo pill and the tool menu floating over it.
///
/// SwiftUI owns the chrome; the canvas is a UIKit controller, because
/// PencilKit, hover, Pencil interactions and virtualised recycling are UIKit
/// APIs. The canvas is rebuilt whenever the session changes -- another tab --
/// and whenever the document's path does, after its folder moved on another
/// device, since its page views are built on the path.
struct EditorScreen: View {
    let tabs: DocumentTabsModel
    @Bindable var session: NotebookSession
    /// Set by the shell: switches to the web surface and navigates it there.
    let onOpenOnWeb: ((String, String?) -> Void)?
    let onNavigateOnWeb: ((String) -> Void)?
    let scanDocument: (@MainActor (UIViewController) async throws -> Data)?

    @Environment(\.scenePhase) private var scenePhase
    @State private var sheet: EditorSheet?
    @State private var isGoingToPage = false
    @State private var pageNumber = ""
    @State private var titledPage: Int?
    @State private var pageTitle = ""

    var body: some View {
        VStack(spacing: 0) {
            DocumentTabBar(
                tabs: tabs,
                onSaveStateTapped: saveStateTapped,
                onExport: { session.commands?.export(.all, from: nil) },
                onInfo: { sheet = .info },
                onMove: {
                    session.onFlush?()
                    sheet = .move
                },
                onOpenInNewWindow: { id in
                    session.onFlush?()
                    PaperWindowView.requestWindow(
                        .document(
                            id: id,
                            pageID: id == session.notebook.id ? currentPage?.id : nil))
                })
            EditorNavigationBar(
                tabs: tabs, session: session, actions: menuActions,
                onSearch: { sheet = .search })
            HStack(spacing: 0) {
                if tabs.isSidebarVisible {
                    EditorSidebar(tabs: tabs, session: session, onEditTitle: editTitle)
                    Rectangle()
                        .fill(PaperTokens.border)
                        .frame(width: 1)
                        // Down to the bottom edge, as the canvas and the
                        // sidebar's panel run.
                        .ignoresSafeArea(.container, edges: .bottom)
                        .accessibilityHidden(true)
                }
                canvas
            }
        }
        .background(PaperTokens.background)
        .overlay(alignment: .bottom) { toast }
        .onChange(of: scenePhase) { _, phase in
            if phase != .active {
                session.onFlush?()
                session.controller.flushForBackground()
            } else {
                // Coming back to the app is the moment to find out what the
                // other device did while this one was asleep.
                session.controller.syncNow()
            }
        }
        .onChange(of: ObjectIdentifier(session)) { _, _ in
            // Another tab: what was open belonged to the one before.
            sheet = nil
            isGoingToPage = false
            titledPage = nil
        }
        .background {
            // Cmd-F searches the document. The UIKit canvas owns the other
            // editing shortcuts; it does not claim this one.
            Button("Find") { sheet = .search }
                .keyboardShortcut("f", modifiers: .command)
                .opacity(0)
                .accessibilityHidden(true)
        }
        .sheet(item: $sheet) { sheet in
            sheetContent(sheet)
        }
        .alert("Go to \(noun)", isPresented: $isGoingToPage) {
            TextField("\(noun) number", text: $pageNumber)
                .keyboardType(.numberPad)
            Button("Cancel", role: .cancel) {}
            Button("Go") {
                guard let number = Int(pageNumber.trimmingCharacters(in: .whitespaces)) else {
                    return
                }
                session.show(pageAt: min(max(number, 1), session.pages.count) - 1)
            }
        }
        .alert(
            session.notebook.kind == .whiteboard ? "Board name" : "Page title",
            isPresented: Binding(
                get: { titledPage != nil }, set: { if !$0 { titledPage = nil } })
        ) {
            TextField("Title", text: $pageTitle)
            Button("Cancel", role: .cancel) { titledPage = nil }
            Button("Save") {
                if let titledPage { session.setTitle(pageTitle, ofPageAt: titledPage) }
                titledPage = nil
            }
        }
    }

    // MARK: - Canvas

    private var canvas: some View {
        Group {
            if session.notebook.kind == .whiteboard {
                BoardCanvas(
                    session: session, onNavigateOnWeb: onNavigateOnWeb,
                    scanDocument: scanDocument)
            } else {
                NotebookCanvas(
                    session: session, onNavigateOnWeb: onNavigateOnWeb,
                    scanDocument: scanDocument)
            }
        }
        .id(CanvasIdentity(session: ObjectIdentifier(session), path: session.notebook.path))
        // The paper runs to the bottom edge of the screen, under the home
        // indicator. The controllers keep their own pills to the safe area,
        // and the floating tools over it, laid on afterwards, keep to it too.
        .ignoresSafeArea(.container, edges: .bottom)
        .overlay {
            FloatingToolLayer(
                tools: session.tools, chrome: session.chrome,
                isDarkPaper: currentPage.map(PaperAppearance.isDark(page:)) ?? false,
                onUndo: { session.commands?.undo() },
                onRedo: { session.commands?.redo() },
                onClearPage: { session.commands?.clearPage(at: session.currentIndex) })
        }
    }

    private struct CanvasIdentity: Hashable {
        let session: ObjectIdentifier
        let path: String
    }

    // MARK: - Menus and sheets

    private var noun: String { session.notebook.kind == .whiteboard ? "Board" : "Page" }

    private var currentPage: Page? {
        session.pages.indices.contains(session.currentIndex)
            ? session.pages[session.currentIndex] : nil
    }

    private var menuActions: EditorMenuActions {
        EditorMenuActions(
            goToPage: {
                pageNumber = "\(session.currentIndex + 1)"
                isGoingToPage = true
            },
            showHistory: { sheet = .history },
            showTrash: { sheet = .trash },
            showInfo: { sheet = .info },
            correctText: { sheet = .transcript },
            openOnWeb: onOpenOnWeb.map { open in
                {
                    session.onFlush?()
                    session.controller.autosave.flushAll()
                    open(session.notebook.path, currentPage?.id)
                }
            },
            canScan: scanDocument != nil)
    }

    private func editTitle(_ index: Int) {
        guard session.pages.indices.contains(index) else { return }
        pageTitle = session.pages[index].title ?? ""
        titledPage = index
    }

    @ViewBuilder
    private func sheetContent(_ sheet: EditorSheet) -> some View {
        switch sheet {
        case .search:
            DocumentSearchSheet(session: session) { pageID, box in
                session.reveal(pageID: pageID, box: box)
            }
        case .history:
            if let page = currentPage {
                HistorySheet(
                    controller: session.controller, notebookPath: session.notebook.path,
                    pageID: page.id, pageNumber: session.currentIndex + 1
                ) {
                    session.reload()
                    session.onPagesChanged?()
                }
            }
        case .trash:
            TrashView(controller: session.controller)
        case .info:
            ItemInfoSheet(controller: session.controller, item: .document(session.notebook)) {
                self.sheet = nil
            }
        case .move:
            // The library's picker. The canvas follows the document to its
            // new path: the window hears of the move and rebuilds it.
            MovePickerSheet(
                controller: session.controller, items: [.document(session.notebook)],
                onMoved: { _ in self.sheet = nil },
                onCancel: { self.sheet = nil })
        case .transcript:
            TranscriptCorrectionSheet(
                text: currentPage.flatMap {
                    session.controller.store.pageText(
                        notebookPath: session.notebook.path, pageID: $0.id)
                }
            ) { corrected in
                guard let currentPage else { return }
                session.controller.setCorrectedTranscript(
                    corrected,
                    notebookPath: session.notebook.path,
                    pageID: currentPage.id)
            }
        }
    }

    // MARK: - Save state and toast

    /// The save state is a button because every one of its states has
    /// something the person might reasonably want to do about it.
    private func saveStateTapped() {
        let controller = session.controller
        switch controller.saveState {
        case .conflict:
            guard let target = controller.conflict,
                target.notebookPath == session.notebook.path
            else {
                controller.clearConflict()
                return
            }
            session.reload()
            session.onPagesChanged?()
            if let index = session.pages.firstIndex(where: { $0.id == target.siblingPageID }) {
                session.show(pageAt: index)
            }
            controller.clearConflict()
        case .attention:
            controller.retryParkedOperations()
        case .signedOut:
            // Sign-in lives in the web app; the new session cookie wakes the
            // sync engine from there.
            if let onNavigateOnWeb {
                controller.autosave.flushAll()
                onNavigateOnWeb("/")
            } else {
                controller.syncNow()
            }
        default:
            controller.syncNow()
        }
    }

    @ViewBuilder
    private var toast: some View {
        if let pending = session.pendingUndo {
            UndoToast(
                model: pending,
                onUndo: { session.undoPendingDelete() },
                onExpire: { session.pendingUndo = nil })
            .padding(.bottom, 24)
            .id(pending.id)
        }
    }
}

/// What the editor shows in a sheet.
enum EditorSheet: String, Identifiable {
    case search, history, trash, info, move, transcript

    var id: String { rawValue }
}

/// The notebook canvas, wrapped.
struct NotebookCanvas: UIViewControllerRepresentable {
    let session: NotebookSession
    let onNavigateOnWeb: ((String) -> Void)?
    let scanDocument: (@MainActor (UIViewController) async throws -> Data)?

    func makeUIViewController(context: Context) -> NotebookViewController {
        NotebookViewController(
            session: session,
            onNavigateOnWeb: onNavigateOnWeb,
            scanDocument: scanDocument)
    }

    func updateUIViewController(_ controller: NotebookViewController, context: Context) {}
}
