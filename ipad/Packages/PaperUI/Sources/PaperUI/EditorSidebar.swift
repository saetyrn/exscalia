import PaperCore
import SwiftUI
import UIKit

/// The 280-point panel at the left of the editor, which pushes the canvas
/// aside: Pages and Outline for a notebook, Boards for a whiteboard.
///
/// It reads the session's pages and changes them through the session and
/// `session.commands`; it never touches a canvas.
struct EditorSidebar: View {
    let tabs: DocumentTabsModel
    @Bindable var session: NotebookSession
    /// Asks the screen for the title prompt for a page.
    let onEditTitle: (Int) -> Void

    static let width: CGFloat = 280

    @State private var bookmarksOnly = false
    @State private var draggingID: String?

    var body: some View {
        let choices = EditorSidebarTab.tabs(for: session.notebook.kind)
        VStack(spacing: 0) {
            if choices.count > 1 {
                Picker("Sidebar", selection: tabSelection) {
                    ForEach(choices, id: \.self) { tab in
                        Text(tab.title).tag(tab)
                    }
                }
                .pickerStyle(.segmented)
                .padding(.horizontal, 12)
                .padding(.vertical, 10)
                .accessibilityIdentifier("SidebarTabs")
            } else {
                Text(choices[0].title)
                    .font(.headline)
                    .foregroundStyle(PaperTokens.text)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 16)
                    .frame(height: 50)
                    .accessibilityAddTraits(.isHeader)
            }
            Divider().overlay(PaperTokens.border)
            switch shownTab {
            case .pages: pages
            case .outline: outline
            case .boards: boards
            }
        }
        .frame(width: Self.width)
        .frame(maxHeight: .infinity)
        .background(PaperTokens.panel)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("Sidebar")
    }

    private var isReadOnly: Bool { session.chrome.isReadOnly }

    /// The window remembers one tab; a whiteboard shows Boards whatever it is.
    private var shownTab: EditorSidebarTab {
        let choices = EditorSidebarTab.tabs(for: session.notebook.kind)
        return choices.contains(tabs.sidebarTab) ? tabs.sidebarTab : choices[0]
    }

    private var tabSelection: Binding<EditorSidebarTab> {
        Binding(get: { shownTab }, set: { tabs.sidebarTab = $0 })
    }

    // MARK: - Pages

    private var pages: some View {
        VStack(spacing: 0) {
            Picker("Show", selection: $bookmarksOnly) {
                Text("All").tag(false)
                Text("Bookmarks").tag(true)
            }
            .pickerStyle(.segmented)
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .accessibilityLabel("Show")
            ScrollViewReader { scroller in
                ScrollView {
                    LazyVGrid(
                        columns: [GridItem(.flexible(), spacing: 14), GridItem(.flexible())],
                        spacing: 16
                    ) {
                        ForEach(Array(session.pages.enumerated()), id: \.element.id) { index, page in
                            if !bookmarksOnly || page.bookmarked {
                                PageCell(
                                    session: session, page: page, index: index,
                                    isReadOnly: isReadOnly, onEditTitle: onEditTitle
                                )
                                .id(page.id)
                                .modifier(
                                    ReorderModifier(
                                        session: session, page: page, index: index,
                                        enabled: !isReadOnly, draggingID: $draggingID))
                            }
                        }
                    }
                    .padding(14)
                }
                .onAppear { scrollToCurrent(scroller) }
                .onChange(of: session.currentIndex) { _, _ in scrollToCurrent(scroller) }
            }
        }
    }

    private func scrollToCurrent(_ scroller: ScrollViewProxy) {
        guard session.pages.indices.contains(session.currentIndex) else { return }
        scroller.scrollTo(session.pages[session.currentIndex].id, anchor: .center)
    }

    // MARK: - Outline

    private var outline: some View {
        let titled = session.pages.enumerated().filter {
            !($0.element.title ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }
        return Group {
            if titled.isEmpty {
                Text("No titled pages")
                    .font(.callout)
                    .foregroundStyle(PaperTokens.textMuted)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                List {
                    ForEach(titled, id: \.element.id) { index, page in
                        OutlineRow(
                            session: session, page: page, index: index, isReadOnly: isReadOnly,
                            onEditTitle: onEditTitle)
                    }
                }
                .listStyle(.plain)
                .scrollContentBackground(.hidden)
            }
        }
    }

    // MARK: - Boards

    private var boards: some View {
        VStack(spacing: 0) {
            Button {
                if let commands = session.commands {
                    commands.addPage(.last, template: nil)
                } else {
                    session.addPage(after: session.pages.count - 1)
                }
            } label: {
                Label("Add Board", systemImage: "plus")
                    .font(.body)
                    .foregroundStyle(PaperTokens.text)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 16)
                    .frame(height: PaperTokens.minimumTarget)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(isReadOnly)
            .accessibilityLabel("Add Board")
            Divider().overlay(PaperTokens.border)
            ScrollView {
                LazyVStack(spacing: 16) {
                    ForEach(Array(session.pages.enumerated()), id: \.element.id) { index, page in
                        BoardCell(
                            session: session, page: page, index: index, isReadOnly: isReadOnly,
                            onEditTitle: onEditTitle
                        )
                        .modifier(
                            ReorderModifier(
                                session: session, page: page, index: index, enabled: !isReadOnly,
                                draggingID: $draggingID))
                    }
                }
                .padding(14)
            }
        }
    }
}

// MARK: - Cells

/// One page in the Pages grid: its picture, its number, a bookmark and a menu.
private struct PageCell: View {
    let session: NotebookSession
    let page: Page
    let index: Int
    let isReadOnly: Bool
    let onEditTitle: (Int) -> Void

    var body: some View {
        let current = index == session.currentIndex
        let title = (page.title ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let name = title.isEmpty ? "Page \(index + 1)" : title
        VStack(spacing: 6) {
            Button {
                session.show(pageAt: index)
            } label: {
                PageThumbnail(session: session, page: page)
                    .aspectRatio(aspect, contentMode: .fit)
                    .overlay(
                        RoundedRectangle(cornerRadius: 4)
                            .strokeBorder(
                                current ? PaperTokens.navigationBar : PaperTokens.border,
                                lineWidth: current ? 2 : 1))
            }
            .buttonStyle(.plain)
            .accessibilityLabel(name)
            .accessibilityValue(current ? "current" : "")
            .accessibilityAddTraits(current ? [.isSelected] : [])
            .overlay(alignment: .topTrailing) {
                Button {
                    session.setBookmarked(!page.bookmarked, pageAt: index)
                } label: {
                    Image(systemName: page.bookmarked ? "bookmark.fill" : "bookmark")
                        .font(.system(size: 13, weight: .regular))
                        .symbolRenderingMode(.monochrome)
                        .foregroundStyle(page.bookmarked ? PaperTokens.navigationBar : PaperTokens.textMuted)
                        .frame(width: 32, height: 32)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help("Bookmark")
                .accessibilityLabel("Bookmark \(name)")
                .accessibilityValue(page.bookmarked ? "on" : "off")
            }
            HStack(spacing: 2) {
                Text(title.isEmpty ? "\(index + 1)" : title)
                    .font(.caption)
                    .monospacedDigit()
                    .lineLimit(1)
                    .foregroundStyle(current ? PaperTokens.text : PaperTokens.textMuted)
                Spacer(minLength: 0)
                PageMenu(
                    session: session, index: index, isReadOnly: isReadOnly,
                    onEditTitle: onEditTitle, name: name)
            }
        }
    }

    private var aspect: CGFloat {
        let size = page.pointSize
        guard size.width > 0, size.height > 0 else { return 0.75 }
        return size.width / size.height
    }
}

/// A page's menu, in the Pages grid.
private struct PageMenu: View {
    let session: NotebookSession
    let index: Int
    let isReadOnly: Bool
    let onEditTitle: (Int) -> Void
    let name: String

    var body: some View {
        let page = session.pages.indices.contains(index) ? session.pages[index] : nil
        Menu {
            Button {
                session.show(pageAt: index)
                session.commands?.addPage(.after, template: nil)
            } label: {
                Label("Add Page After", systemImage: "plus")
            }
            .disabled(isReadOnly || !session.hasCommands)
            Button {
                session.duplicatePage(at: index)
            } label: {
                Label("Duplicate", systemImage: "plus.square.on.square")
            }
            .disabled(isReadOnly)
            Button {
                onEditTitle(index)
            } label: {
                Label("Title…", systemImage: "textformat")
            }
            .disabled(isReadOnly)
            Button {
                session.setBookmarked(!(page?.bookmarked ?? false), pageAt: index)
            } label: {
                Label(
                    page?.bookmarked == true ? "Remove Bookmark" : "Bookmark",
                    systemImage: page?.bookmarked == true ? "bookmark.slash" : "bookmark")
            }
            Menu {
                ForEach(Template.paperTemplates, id: \.self) { template in
                    Button(template.displayName) {
                        session.commands?.setPageStyle(template: template, paperColor: nil, at: index)
                    }
                }
            } label: {
                Label("Change Template", systemImage: "squareshape.split.3x3")
            }
            .disabled(isReadOnly || !session.hasCommands)
            Menu {
                ForEach(PaperColor.paperChoices, id: \.self) { color in
                    Button(color.displayName) {
                        session.commands?.setPageStyle(template: nil, paperColor: color, at: index)
                    }
                }
            } label: {
                Label("Paper Color", systemImage: "paintpalette")
            }
            .disabled(isReadOnly || !session.hasCommands)
            Button {
                session.commands?.clearPage(at: index)
            } label: {
                Label("Clear Page", systemImage: "eraser")
            }
            .disabled(isReadOnly || !session.hasCommands)
            Button {
                session.show(pageAt: index)
                session.commands?.export(.currentPage, from: nil)
            } label: {
                Label("Export Page", systemImage: "square.and.arrow.up")
            }
            .disabled(!session.hasCommands)
            Button(role: .destructive) {
                session.deletePage(at: index)
            } label: {
                Label("Move to Trash", systemImage: "trash")
            }
            .disabled(isReadOnly || session.pages.count < 2)
        } label: {
            Image(systemName: "chevron.down")
                .font(.system(size: 12, weight: .semibold))
                .symbolRenderingMode(.monochrome)
                .foregroundStyle(PaperTokens.textMuted)
                .frame(width: 32, height: 28)
                .contentShape(Rectangle())
        }
        .menuStyle(.button)
        .buttonStyle(.plain)
        .help("Page Menu")
        .accessibilityLabel("\(name) menu")
    }
}

/// One titled page in the Outline.
private struct OutlineRow: View {
    let session: NotebookSession
    let page: Page
    let index: Int
    let isReadOnly: Bool
    let onEditTitle: (Int) -> Void

    var body: some View {
        let current = index == session.currentIndex
        HStack(spacing: 8) {
            Button {
                session.show(pageAt: index)
            } label: {
                VStack(alignment: .leading, spacing: 2) {
                    Text(page.title ?? "")
                        .font(.body)
                        .foregroundStyle(PaperTokens.text)
                        .lineLimit(2)
                    Text("Page \(index + 1)")
                        .font(.caption)
                        .monospacedDigit()
                        .foregroundStyle(PaperTokens.textMuted)
                }
                .frame(maxWidth: .infinity, minHeight: PaperTokens.minimumTarget, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("\(page.title ?? ""), page \(index + 1)")
            .accessibilityAddTraits(current ? [.isSelected] : [])
            Menu {
                Button {
                    onEditTitle(index)
                } label: {
                    Label("Rename", systemImage: "pencil")
                }
                Button(role: .destructive) {
                    session.setTitle("", ofPageAt: index)
                } label: {
                    Label("Remove Title", systemImage: "minus.circle")
                }
            } label: {
                Image(systemName: "ellipsis")
                    .symbolRenderingMode(.monochrome)
                    .foregroundStyle(PaperTokens.textMuted)
                    .frame(width: 36, height: PaperTokens.minimumTarget)
                    .contentShape(Rectangle())
            }
            .menuStyle(.button)
            .buttonStyle(.plain)
            .disabled(isReadOnly)
            .accessibilityLabel("\(page.title ?? "") menu")
        }
        .listRowBackground(current ? PaperTokens.toolSelected.opacity(0.35) : Color.clear)
    }
}

/// One board in the Boards list: its picture, its name and its menu.
private struct BoardCell: View {
    let session: NotebookSession
    let page: Page
    let index: Int
    let isReadOnly: Bool
    let onEditTitle: (Int) -> Void

    var body: some View {
        let current = index == session.currentIndex
        let title = (page.title ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let name = title.isEmpty ? "Board \(index + 1)" : title
        VStack(alignment: .leading, spacing: 6) {
            Button {
                session.show(pageAt: index)
            } label: {
                PageThumbnail(session: session, page: page)
                    .aspectRatio(4 / 3, contentMode: .fit)
                    .overlay(
                        RoundedRectangle(cornerRadius: 4)
                            .strokeBorder(
                                current ? PaperTokens.navigationBar : PaperTokens.border,
                                lineWidth: current ? 2 : 1))
            }
            .buttonStyle(.plain)
            .accessibilityLabel(name)
            .accessibilityValue(current ? "current" : "")
            .accessibilityAddTraits(current ? [.isSelected] : [])
            HStack(spacing: 2) {
                Text(name)
                    .font(.caption)
                    .lineLimit(1)
                    .foregroundStyle(current ? PaperTokens.text : PaperTokens.textMuted)
                Spacer(minLength: 0)
                Menu {
                    Button {
                        onEditTitle(index)
                    } label: {
                        Label("Rename", systemImage: "pencil")
                    }
                    .disabled(isReadOnly)
                    Button {
                        session.duplicatePage(at: index)
                    } label: {
                        Label("Duplicate", systemImage: "plus.square.on.square")
                    }
                    .disabled(isReadOnly)
                    Button {
                        session.show(pageAt: index)
                        session.commands?.export(.currentPage, from: nil)
                    } label: {
                        Label("Export", systemImage: "square.and.arrow.up")
                    }
                    .disabled(!session.hasCommands)
                    Button(role: .destructive) {
                        session.deletePage(at: index)
                    } label: {
                        Label("Move to Trash", systemImage: "trash")
                    }
                    .disabled(isReadOnly || session.pages.count < 2)
                } label: {
                    Image(systemName: "chevron.down")
                        .font(.system(size: 12, weight: .semibold))
                        .symbolRenderingMode(.monochrome)
                        .foregroundStyle(PaperTokens.textMuted)
                        .frame(width: 32, height: 28)
                        .contentShape(Rectangle())
                }
                .menuStyle(.button)
                .buttonStyle(.plain)
                .accessibilityLabel("\(name) menu")
            }
        }
    }
}

// MARK: - Reordering

/// Drag a thumbnail onto another to move it there.
private struct ReorderModifier: ViewModifier {
    let session: NotebookSession
    let page: Page
    let index: Int
    let enabled: Bool
    @Binding var draggingID: String?

    func body(content: Content) -> some View {
        if enabled {
            content
                .onDrag {
                    draggingID = page.id
                    return NSItemProvider(object: page.id as NSString)
                }
                .onDrop(
                    of: [.text],
                    delegate: ThumbnailDropDelegate(
                        session: session, destination: index, draggingID: $draggingID))
        } else {
            content
        }
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

/// One page's picture: its render if it has one, otherwise the bare paper so
/// the sidebar never shows an empty grey box.
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
                TemplatePreview(
                    template: page.template, paperColor: page.paperColor,
                    // A board without a render shows what an empty one renders.
                    pageSize: page.size == .board ? CGSize(width: 1366, height: 1024) : page.pointSize)
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: 4))
        .accessibilityHidden(true)
    }

    private var renderImage: UIImage? {
        let url = session.controller.store.paths.render(session.notebook.path, page.id)
        guard let data = try? Data(contentsOf: url) else { return nil }
        return UIImage(data: data)
    }
}
