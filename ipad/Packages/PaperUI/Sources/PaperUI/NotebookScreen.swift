import PaperCore
import SwiftUI
import UIKit

/// The notebook: header, canvas, optional thumbnail strip, undo toast.
///
/// SwiftUI owns the chrome; the canvas itself is the UIKit controller, because
/// PencilKit, hover, Pencil interactions and virtualised recycling are all
/// UIKit APIs.
public struct NotebookScreen: View {
    @Environment(\.scenePhase) private var scenePhase
    @State private var session: NotebookSession
    @State private var toastDeadline: Date?
    @State private var isShowingHistory = false
    @State private var isShowingTrash = false
    @State private var isSearching = false
    @State private var remoteChangeObserverID: UUID?
    @State private var isShowingOutline = false
    @State private var isShowingInspector = false
    @State private var isEditingPageTitle = false
    @State private var pageTitle = ""
    @State private var isCorrectingTranscript = false
    @State private var isSwitchingNotebook = false
    private let onBack: () -> Void
    private let reveal: SearchDestination?
    /// Set by the shell: switches to the web surface and navigates it there.
    private let onOpenOnWeb: ((String, String?) -> Void)?
    private let onNavigateOnWeb: ((String) -> Void)?
    private let scanDocument: (@MainActor (UIViewController) async throws -> Data)?

    public init(
        controller: PaperController,
        notebook: Notebook,
        reveal: SearchDestination? = nil,
        onOpenOnWeb: ((String, String?) -> Void)? = nil,
        onNavigateOnWeb: ((String) -> Void)? = nil,
        scanDocument: (@MainActor (UIViewController) async throws -> Data)? = nil,
        onBack: @escaping () -> Void
    ) {
        _session = State(initialValue: NotebookSession(controller: controller, notebook: notebook))
        self.reveal = reveal
        self.onOpenOnWeb = onOpenOnWeb
        self.onNavigateOnWeb = onNavigateOnWeb
        self.scanDocument = scanDocument
        self.onBack = onBack
    }

    public var body: some View {
        VStack(spacing: 0) {
            header
            Divider().overlay(PaperTokens.border)
            NotebookCanvas(
                session: session,
                onNavigateOnWeb: onNavigateOnWeb,
                scanDocument: scanDocument)
                // The screen stays; only the notebook inside it is replaced.
                .id(session.notebook.id)
            if session.isShowingThumbnails {
                Divider().overlay(PaperTokens.border)
                PageThumbnailStrip(session: session)
            }
        }
        .background(PaperTokens.background)
        .overlay(alignment: .bottom) { toast }
        .onChange(of: scenePhase) { _, phase in
            if phase != .active {
                session.controller.flushForBackground()
            } else {
                // Coming back to the app is the moment to find out what the
                // other device did while this one was asleep.
                session.controller.syncNow()
            }
        }
        .onAppear {
            apply(reveal)
            remoteChangeObserverID = session.controller.observeRemoteChanges { paths in
                guard paths.contains(session.notebook.path) else { return }
                session.reload()
                session.onPagesChanged?()
            }
        }
        .onDisappear {
            if let remoteChangeObserverID {
                session.controller.removeRemoteChangeObserver(remoteChangeObserverID)
                self.remoteChangeObserverID = nil
            }
        }
        .onChange(of: reveal) { _, destination in apply(destination) }
        .background {
            // Cmd-F opens search from inside a notebook too. The UIKit canvas
            // owns the other shortcuts; it does not claim these.
            VStack(spacing: 0) {
                Button("Find") { isSearching = true }
                    .keyboardShortcut("f", modifiers: .command)
                Button("Switch notebook") { isSwitchingNotebook = true }
                    .keyboardShortcut("o", modifiers: .command)
                Button("Next notebook") { cycleNotebook(by: 1) }
                    .keyboardShortcut(.tab, modifiers: .control)
                Button("Previous notebook") { cycleNotebook(by: -1) }
                    .keyboardShortcut(.tab, modifiers: [.control, .shift])
            }
            .opacity(0)
            .accessibilityHidden(true)
        }
        .sheet(isPresented: $isSearching) {
            NotebookSearchSheet(controller: session.controller) { destination in
                isSearching = false
                guard destination.notebookPath == session.notebook.path else { return }
                apply(destination)
            }
        }
        .sheet(isPresented: $isShowingHistory) {
            if let page = session.pages.indices.contains(session.currentIndex)
                ? session.pages[session.currentIndex] : nil
            {
                HistorySheet(
                    controller: session.controller, notebookPath: session.notebook.path,
                    pageID: page.id, pageNumber: session.currentIndex + 1
                ) {
                    session.reload()
                    session.onPagesChanged?()
                }
            }
        }
        .sheet(isPresented: $isShowingTrash) {
            TrashView(controller: session.controller)
        }
        .sheet(isPresented: $isShowingOutline) {
            PageOutlineSheet(
                pages: session.pages,
                currentPageID: currentPage?.id
            ) { pageID in
                session.reveal(pageID: pageID, box: nil)
            }
        }
        .sheet(isPresented: $isShowingInspector) {
            NotebookInspectorSheet(
                notebook: session.notebook,
                pages: session.pages,
                knownFolders: session.controller.folders
            ) { draft in
                session.controller.updateNotebook(
                    session.notebook,
                    folder: draft.folder,
                    tags: draft.parsedTags,
                    favorite: draft.favorite,
                    coverPageID: draft.coverPageID.isEmpty ? nil : draft.coverPageID,
                    template: draft.template,
                    paperSize: draft.paperSize)
                session.reload()
                session.onPagesChanged?()
            }
        }
        .alert("Page title", isPresented: $isEditingPageTitle) {
            TextField("Optional title", text: $pageTitle)
            Button("Cancel", role: .cancel) {}
            Button("Save") {
                guard let currentPage else { return }
                session.controller.setPageTitle(
                    pageTitle, notebookPath: session.notebook.path, pageID: currentPage.id)
                session.reload()
                session.onPagesChanged?()
            }
        } message: {
            Text("Leave it empty to use the page number in the outline.")
        }
        .sheet(isPresented: $isCorrectingTranscript) {
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

    /// Jumps to the page a search result or a deep link named and, when it
    /// carries one, puts the accent outline round the words themselves.
    private func apply(_ destination: SearchDestination?) {
        guard let destination, destination.notebookPath == session.notebook.path,
            let pageID = destination.pageID
        else { return }
        session.reveal(pageID: pageID, box: destination.box)
    }

    private var header: some View {
        HStack(spacing: 12) {
            Button(action: {
                session.controller.autosave.flushAll()
                onBack()
            }) {
                Image(systemName: "chevron.backward")
                    .font(.system(size: 17, weight: .regular))
                    .symbolRenderingMode(.monochrome)
                    .frame(width: PaperTokens.minimumTarget, height: PaperTokens.minimumTarget)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .foregroundStyle(PaperTokens.text)
            .accessibilityLabel("Back to the library")
            .accessibilityHint("Leaves this notebook")

            VStack(alignment: .leading, spacing: 0) {
                Text(session.notebook.title)
                    .font(.headline)
                    .foregroundStyle(PaperTokens.text)
                    .lineLimit(1)
                let label = (currentPage?.title ?? "")
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                Text(
                    label.isEmpty
                        ? "Page \(session.currentIndex + 1) of \(session.pages.count)"
                        : "\(label) · Page \(session.currentIndex + 1) of \(session.pages.count)")
                    .font(.caption)
                    .foregroundStyle(PaperTokens.textMuted)
                    .monospacedDigit()
            }
            .accessibilityElement(children: .combine)

            Spacer(minLength: 0)

            Button(action: { isSwitchingNotebook = true }) {
                Image(systemName: "books.vertical")
                    .font(.system(size: 16, weight: .regular))
                    .symbolRenderingMode(.monochrome)
                    .frame(width: PaperTokens.minimumTarget, height: PaperTokens.minimumTarget)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .foregroundStyle(PaperTokens.text)
            .accessibilityLabel("Switch notebook")
            .accessibilityHint("Opens another notebook without leaving this page")
            .popover(isPresented: $isSwitchingNotebook) {
                NotebookSwitcherView(
                    controller: session.controller,
                    currentID: session.notebook.id
                ) { notebook in
                    isSwitchingNotebook = false
                    switchTo(notebook)
                }
            }

            Button(action: { isShowingOutline = true }) {
                Image(systemName: "list.bullet.rectangle")
                    .font(.system(size: 16, weight: .regular))
                    .symbolRenderingMode(.monochrome)
                    .frame(width: PaperTokens.minimumTarget, height: PaperTokens.minimumTarget)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .foregroundStyle(PaperTokens.text)
            .accessibilityLabel("Notebook outline")
            .accessibilityHint("Shows titled and bookmarked pages")

            Button(action: { session.isShowingThumbnails.toggle() }) {
                Image(systemName: session.isShowingThumbnails ? "square.grid.2x2.fill" : "square.grid.2x2")
                    .font(.system(size: 16, weight: .regular))
                    .symbolRenderingMode(.monochrome)
                    .frame(width: PaperTokens.minimumTarget, height: PaperTokens.minimumTarget)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .foregroundStyle(session.isShowingThumbnails ? PaperTokens.accent : PaperTokens.text)
            .accessibilityLabel("Page thumbnails")
            .accessibilityValue(session.isShowingThumbnails ? "shown" : "hidden")
            .accessibilityHint("Shows the strip of pages for reordering")

            overflowMenu

            SaveStatePill(state: session.controller.saveState, action: pillTapped)
                // Re-rendered on the controller's five-second tick so
                // "Synced 3s ago" keeps counting without the state changing.
                .id(session.controller.saveStateVersion)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(PaperTokens.panel)
    }

    /// The `⋯` menu: everything about this notebook that is not the writing.
    private var overflowMenu: some View {
        Menu {
            Button("Search") { isSearching = true }
            Button("Edit page title") {
                pageTitle = currentPage?.title ?? ""
                isEditingPageTitle = true
            }
            if let currentPage {
                Button(currentPage.bookmarked ? "Remove bookmark" : "Bookmark page") {
                    session.controller.setPageBookmarked(
                        !currentPage.bookmarked,
                        notebookPath: session.notebook.path, pageID: currentPage.id)
                    session.reload()
                    session.onPagesChanged?()
                }
            }
            Button("Notebook inspector") { isShowingInspector = true }
            Button("Correct recognized text") { isCorrectingTranscript = true }
            Button("Copy page link") {
                guard let currentPage,
                    let url = session.controller.durableLink(
                        notebookID: session.notebook.id,
                        pageID: currentPage.id)
                else { return }
                UIPasteboard.general.url = url
                UIAccessibility.post(
                    notification: .announcement, argument: "Paper page link copied")
            }
            Button("Page history") { isShowingHistory = true }
            Button("Trash") { isShowingTrash = true }
            Divider()
            if let onOpenOnWeb {
                Button("Open on web") {
                    session.controller.autosave.flushAll()
                    onOpenOnWeb(
                        session.notebook.path,
                        session.pages.indices.contains(session.currentIndex)
                            ? session.pages[session.currentIndex].id : nil)
                }
            }
            Button("Sync now") { session.controller.syncNow() }
        } label: {
            Image(systemName: "ellipsis")
                .font(.system(size: 16, weight: .regular))
                .symbolRenderingMode(.monochrome)
                .frame(width: PaperTokens.minimumTarget, height: PaperTokens.minimumTarget)
                .contentShape(Rectangle())
        }
        .foregroundStyle(PaperTokens.text)
        .accessibilityLabel("More")
        .accessibilityHint("Search, page history, trash, the web viewer and sync")
    }

    /// Puts another notebook in this screen's place.
    ///
    /// The ink and the reading position of the notebook being left are written
    /// first, then the session is replaced. No navigation happens: the screen,
    /// the toolbar and the tool selection are the same objects afterwards.
    private func switchTo(_ notebook: Notebook) {
        guard notebook.id != session.notebook.id else { return }
        session.onFlush?()
        session.controller.autosave.flushAll()
        if let remoteChangeObserverID {
            session.controller.removeRemoteChangeObserver(remoteChangeObserverID)
            self.remoteChangeObserverID = nil
        }
        // The tool state travels with the window rather than with the book: the
        // pen you were writing with is the pen you keep.
        session = NotebookSession(
            controller: session.controller, notebook: notebook, tools: session.tools)
        session.controller.refresh()
        remoteChangeObserverID = session.controller.observeRemoteChanges { paths in
            guard paths.contains(session.notebook.path) else { return }
            session.reload()
            session.onPagesChanged?()
        }
        UIAccessibility.post(notification: .screenChanged, argument: notebook.title)
    }

    /// Control-Tab through the notebooks in the order they were last opened.
    private func cycleNotebook(by step: Int) {
        var order = session.controller.recentNotebooks(limit: 12)
        if !order.contains(where: { $0.id == session.notebook.id }) {
            order.insert(session.notebook, at: 0)
        }
        guard order.count > 1,
            let index = order.firstIndex(where: { $0.id == session.notebook.id })
        else { return }
        let next = (index + step + order.count) % order.count
        switchTo(order[next])
    }

    private var currentPage: Page? {
        session.pages.indices.contains(session.currentIndex)
            ? session.pages[session.currentIndex] : nil
    }

    /// The pill is a button because every one of its states has something the
    /// user might reasonably want to do about it.
    private func pillTapped() {
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
                session.currentIndex = index
                session.onScrollTo?(index)
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

/// Search from inside a notebook. A sheet rather than a bar, because the
/// canvas is the screen and a search field over the top of it would move the
/// page under the pen.
struct NotebookSearchSheet: View {
    let controller: PaperController
    let onOpen: (SearchDestination) -> Void

    @State private var query = ""
    @Environment(\.dismiss) private var dismiss
    @FocusState private var focused: Bool

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                HStack(spacing: 8) {
                    Image(systemName: "magnifyingglass")
                        .symbolRenderingMode(.monochrome)
                        .foregroundStyle(PaperTokens.textMuted)
                        .accessibilityHidden(true)
                    TextField("Search handwriting", text: $query)
                        .textFieldStyle(.plain)
                        .focused($focused)
                        .submitLabel(.search)
                        .accessibilityLabel("Search handwriting")
                }
                .padding(.horizontal, 20)
                .frame(height: PaperTokens.minimumTarget)
                Divider().overlay(PaperTokens.border)
                if query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    Spacer()
                } else {
                    SearchView(controller: controller, query: query, onOpen: onOpen)
                }
            }
            .background(PaperTokens.background)
            .navigationTitle("Search")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Done") { dismiss() }
                }
            }
            .onAppear { focused = true }
        }
    }
}

/// The UIKit canvas, wrapped.
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

/// The strip of pages: add, reorder by dragging, swipe a page up to delete.
struct PageThumbnailStrip: View {
    @Bindable var session: NotebookSession
    @State private var draggingID: String?

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 12) {
                    ForEach(Array(session.pages.enumerated()), id: \.element.id) { index, page in
                        thumbnail(page: page, index: index)
                            .id(page.id)
                            .onDrag {
                                draggingID = page.id
                                return NSItemProvider(object: page.id as NSString)
                            }
                            .onDrop(
                                of: [.text],
                                delegate: ThumbnailDropDelegate(
                                    session: session, destination: index,
                                    draggingID: $draggingID))
                    }
                    addButton
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 10)
            }
            .onChange(of: session.currentIndex) { _, index in
                guard session.pages.indices.contains(index) else { return }
                proxy.scrollTo(session.pages[index].id, anchor: .center)
            }
        }
        .frame(height: 132)
        .background(PaperTokens.panel)
    }

    private func thumbnail(page: Page, index: Int) -> some View {
        let isCurrent = index == session.currentIndex
        let title = (page.title ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return VStack(spacing: 4) {
            PageThumbnail(session: session, page: page)
                .frame(width: 72, height: 96)
                .overlay(
                    RoundedRectangle(cornerRadius: 3)
                        .strokeBorder(
                            isCurrent ? PaperTokens.accent : PaperTokens.border,
                            lineWidth: isCurrent ? 2 : 1))
            HStack(spacing: 3) {
                if page.bookmarked {
                    Image(systemName: "bookmark.fill")
                        .font(.system(size: 8))
                        .symbolRenderingMode(.monochrome)
                        .accessibilityHidden(true)
                }
                Text(title.isEmpty ? "\(index + 1)" : title)
                    .font(.caption2)
                    .lineLimit(1)
            }
            .foregroundStyle(isCurrent ? PaperTokens.accent : PaperTokens.textMuted)
            .frame(width: 72)
        }
        .contentShape(Rectangle())
        .onTapGesture {
            session.currentIndex = index
            session.onScrollTo?(index)
        }
        // Swipe a page up to delete it, the way the plan describes, with a
        // context menu as the alternative for anyone who cannot swipe.
        .gesture(
            DragGesture(minimumDistance: 30)
                .onEnded { value in
                    if value.translation.height < -40 { session.deletePage(at: index) }
                }
        )
        .contextMenu {
            Button("Add page after") { session.addPage(after: index) }
            Button("Delete page", role: .destructive) { session.deletePage(at: index) }
                .disabled(session.pages.count < 2)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(title.isEmpty ? "Page \(index + 1)" : title)
        .accessibilityValue(isCurrent ? "current page" : "")
        .accessibilityHint("Opens this page. Swipe up to delete it.")
        .accessibilityAddTraits(isCurrent ? [.isSelected] : [])
    }

    private var addButton: some View {
        Button(action: { session.addPage(after: session.pages.count - 1) }) {
            RoundedRectangle(cornerRadius: 3)
                .strokeBorder(PaperTokens.border, style: StrokeStyle(lineWidth: 1, dash: [4, 4]))
                .frame(width: 72, height: 96)
                .overlay(
                    Image(systemName: "plus")
                        .font(.system(size: 18, weight: .regular))
                        .symbolRenderingMode(.monochrome)
                        .foregroundStyle(PaperTokens.textMuted))
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Add page")
        .accessibilityHint("Adds a page at the end of the notebook")
    }
}

/// Reorder target for one thumbnail.
struct ThumbnailDropDelegate: DropDelegate {
    let session: NotebookSession
    let destination: Int
    @Binding var draggingID: String?

    func performDrop(info: DropInfo) -> Bool {
        guard let draggingID,
            let source = session.pages.firstIndex(where: { $0.id == draggingID })
        else { return false }
        if source != destination { session.movePage(from: source, to: destination) }
        self.draggingID = nil
        return true
    }

    func dropEntered(info: DropInfo) {}
}

/// One page's picture: its render if it has one, otherwise the bare template so
/// the strip never shows an empty grey box.
struct PageThumbnail: View {
    let session: NotebookSession
    let page: Page

    var body: some View {
        Group {
            if let image = renderImage {
                Image(uiImage: image)
                    .resizable()
                    .aspectRatio(contentMode: .fill)
            } else {
                TemplatePreview(template: page.template)
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: 3))
    }

    private var renderImage: UIImage? {
        let url = session.controller.store.paths.render(session.notebook.path, page.id)
        guard let data = try? Data(contentsOf: url) else { return nil }
        return UIImage(data: data)
    }
}

/// The paper ruling drawn directly, used for the template picker and for pages
/// with no render yet.
struct TemplatePreview: View {
    let template: Template

    var body: some View {
        Canvas { context, size in
            context.fill(Path(CGRect(origin: .zero, size: size)), with: .color(PaperTokens.paper))
            // The preview stands in for an A4 page, so the ruling is drawn at
            // the same relative spacing the real page uses.
            let reference = PaperSize.a4.points
            let scale = size.width / reference.width
            var path = Path()
            for line in TemplateGeometry.lines(template: template, size: reference) {
                path.move(to: CGPoint(x: line.0.x * scale, y: line.0.y * scale))
                path.addLine(to: CGPoint(x: line.1.x * scale, y: line.1.y * scale))
            }
            context.stroke(path, with: .color(ruleColor), lineWidth: 0.5)
            for dot in TemplateGeometry.dots(template: template, size: reference) {
                let point = CGPoint(x: dot.x * scale, y: dot.y * scale)
                context.fill(
                    Path(ellipseIn: CGRect(x: point.x - 0.7, y: point.y - 0.7, width: 1.4, height: 1.4)),
                    with: .color(ruleColor))
            }
        }
        .background(PaperTokens.paper)
    }

    private var ruleColor: Color {
        Color(
            red: TemplateGeometry.ruleComponents.red,
            green: TemplateGeometry.ruleComponents.green,
            blue: TemplateGeometry.ruleComponents.blue
        ).opacity(TemplateGeometry.ruleComponents.alpha)
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
