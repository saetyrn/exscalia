import PaperCore
import SwiftUI
import UIKit

/// What the editor's menus ask of the screen around them: the sheets and the
/// prompts they open. Everything that touches the canvas goes through
/// `session.commands` instead.
struct EditorMenuActions {
    var goToPage: () -> Void
    var showHistory: () -> Void
    var showTrash: () -> Void
    var showInfo: () -> Void
    var correctText: () -> Void
    var openOnWeb: (() -> Void)?
    /// A scanner is wired in by the shell.
    var canScan: Bool
}

/// A UIKit view behind a SwiftUI control, for a share sheet or a picker to
/// point its popover at.
@MainActor
final class PopoverAnchor {
    weak var view: UIView?
}

struct PopoverAnchorView: UIViewRepresentable {
    let anchor: PopoverAnchor

    func makeUIView(context: Context) -> UIView {
        let view = UIView()
        view.isUserInteractionEnabled = false
        view.backgroundColor = .clear
        anchor.view = view
        return view
    }

    func updateUIView(_ view: UIView, context: Context) {
        anchor.view = view
    }
}

/// The glyph every navigation bar button wears: white, monochrome, on a 44pt
/// target, on the light square when it is switched on.
struct NavigationBarGlyph: View {
    let symbol: String
    var isOn = false

    var body: some View {
        Image(systemName: symbol)
            .font(.system(size: 19, weight: .regular))
            .symbolRenderingMode(.monochrome)
            .foregroundStyle(isOn ? PaperTokens.toolSelectedGlyph : Color.white)
            .frame(width: 36, height: 36)
            .background(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .fill(isOn ? PaperTokens.toolSelected : Color.clear))
            .frame(width: PaperTokens.minimumTarget, height: PaperTokens.minimumTarget)
            .contentShape(Rectangle())
    }
}

// MARK: - Add

/// Add Page: where the page goes, then the paper it is, then what to bring
/// in. On a whiteboard it is a single button that adds a board.
struct EditorAddMenu: View {
    @Bindable var session: NotebookSession
    let actions: EditorMenuActions
    let isReadOnly: Bool

    @State private var position: PagePosition = .after

    var body: some View {
        if session.notebook.kind == .whiteboard {
            Button(action: addBoard) {
                NavigationBarGlyph(symbol: "plus.circle")
            }
            .buttonStyle(.plain)
            .disabled(isReadOnly)
            .opacity(isReadOnly ? 0.4 : 1)
            .help("Add Board")
            .accessibilityLabel("Add Board")
        } else {
            Menu {
                Picker("Position", selection: $position) {
                    Text("Before").tag(PagePosition.before)
                    Text("After").tag(PagePosition.after)
                    Text("Last").tag(PagePosition.last)
                }
                .pickerStyle(.inline)
                Section {
                    ForEach(templates, id: \.self) { template in
                        Button {
                            session.commands?.addPage(position, template: template)
                        } label: {
                            Label(template.displayName, systemImage: Self.symbol(for: template))
                        }
                    }
                }
                Section {
                    Button {
                        session.commands?.presentInsert(.pdf, from: nil)
                    } label: {
                        Label("Import PDF", systemImage: "doc.badge.plus")
                    }
                    Button {
                        session.commands?.presentInsert(.photo, from: nil)
                    } label: {
                        Label("Photo", systemImage: "photo")
                    }
                    Button {
                        session.commands?.presentInsert(.camera, from: nil)
                    } label: {
                        Label("Camera", systemImage: "camera")
                    }
                    // The simulator has no camera; the entry stays and says so.
                    .disabled(!UIImagePickerController.isSourceTypeAvailable(.camera))
                    Button {
                        session.commands?.presentInsert(.scan, from: nil)
                    } label: {
                        Label("Scan Document", systemImage: "doc.viewfinder")
                    }
                    .disabled(!actions.canScan)
                }
                Section {
                    Button {
                        session.tools.select(.text)
                    } label: {
                        Label("Text", systemImage: "character.textbox")
                    }
                    Button {
                        // A link is a text object holding a URL; the prompt
                        // and the placing are the canvas's.
                        (session.commands as? NotebookViewController)?.presentLinkPrompt()
                    } label: {
                        Label("Link", systemImage: "link")
                    }
                }
            } label: {
                NavigationBarGlyph(symbol: "plus.circle")
            }
            .menuStyle(.button)
            .buttonStyle(.plain)
            .disabled(isReadOnly || !session.hasCommands)
            .opacity(isReadOnly ? 0.4 : 1)
            .help("Add Page")
            .accessibilityLabel("Add Page")
        }
    }

    /// The notebook's own paper first: it is what a new page would be anyway.
    private var templates: [Template] {
        let own = session.notebook.template
        return Template.paperTemplates.contains(own)
            ? [own] + Template.paperTemplates.filter { $0 != own } : Template.paperTemplates
    }

    private func addBoard() {
        if let commands = session.commands {
            commands.addPage(.last, template: nil)
        } else {
            session.addPage(after: session.pages.count - 1)
        }
    }

    static func symbol(for template: Template) -> String {
        switch template {
        case .blank, .cover: return "doc"
        case .ruled: return "line.3.horizontal"
        case .grid: return "squareshape.split.3x3"
        case .dotted: return "circle.grid.3x3"
        }
    }
}

// MARK: - Share

/// Share: this page or board, everything, a link to the page, and the web.
struct EditorShareMenu: View {
    @Bindable var session: NotebookSession
    let actions: EditorMenuActions

    @State private var anchor = PopoverAnchor()

    var body: some View {
        let board = session.notebook.kind == .whiteboard
        Menu {
            Button {
                session.commands?.export(.currentPage, from: anchor.view)
            } label: {
                Label(board ? "Export This Board" : "Export This Page", systemImage: "doc")
            }
            Button {
                session.commands?.export(.all, from: anchor.view)
            } label: {
                Label("Export All", systemImage: "doc.on.doc")
            }
            Divider()
            Button(action: copyLink) {
                Label(board ? "Copy Board Link" : "Copy Page Link", systemImage: "link")
            }
            .disabled(link == nil)
            if let openOnWeb = actions.openOnWeb {
                Button(action: openOnWeb) {
                    Label("Open on Web", systemImage: "safari")
                }
            }
        } label: {
            NavigationBarGlyph(symbol: "square.and.arrow.up")
                .background(PopoverAnchorView(anchor: anchor))
        }
        .menuStyle(.button)
        .buttonStyle(.plain)
        .help("Share")
        .accessibilityLabel("Share")
    }

    private var link: URL? {
        guard session.pages.indices.contains(session.currentIndex) else { return nil }
        return session.controller.durableLink(
            notebookID: session.notebook.id, pageID: session.pages[session.currentIndex].id)
    }

    private func copyLink() {
        guard let link else { return }
        UIPasteboard.general.url = link
        UIAccessibility.post(notification: .announcement, argument: "Link copied")
    }
}

// MARK: - More

/// More: the page showing, the document, and the app's housekeeping. Every
/// entry the notebook's old `⋯` menu had is here or in the sidebar.
struct EditorMoreMenu: View {
    @Bindable var session: NotebookSession
    let actions: EditorMenuActions
    let isReadOnly: Bool

    var body: some View {
        let board = session.notebook.kind == .whiteboard
        let noun = board ? "Board" : "Page"
        Menu {
            Section {
                Button(action: actions.goToPage) {
                    Label("Go to \(noun)…", systemImage: "arrow.right.doc.on.clipboard")
                }
                Picker(selection: templateBinding) {
                    ForEach(templateChoices, id: \.self) { template in
                        Text(template.displayName(for: session.notebook.kind)).tag(template)
                    }
                } label: {
                    Label(board ? "Change Background" : "Change Template", systemImage: "squareshape.split.3x3")
                }
                .pickerStyle(.menu)
                .disabled(isReadOnly || !session.hasCommands)
                Picker(selection: colorBinding) {
                    ForEach(PaperColor.paperChoices, id: \.self) { color in
                        Text(color.displayName).tag(color)
                    }
                } label: {
                    Label("Paper Color", systemImage: "paintpalette")
                }
                .pickerStyle(.menu)
                .disabled(isReadOnly || !session.hasCommands)
                Button {
                    session.commands?.clearPage(at: session.currentIndex)
                } label: {
                    Label("Clear \(noun)", systemImage: "eraser")
                }
                .disabled(isReadOnly || !session.hasCommands)
                if !board, let page = currentPage {
                    Button {
                        session.setBookmarked(!page.bookmarked, pageAt: session.currentIndex)
                    } label: {
                        Label(
                            page.bookmarked ? "Remove Bookmark" : "Bookmark Page",
                            systemImage: page.bookmarked ? "bookmark.slash" : "bookmark")
                    }
                }
                Button(role: .destructive) {
                    session.deletePage(at: session.currentIndex)
                } label: {
                    Label("Move \(noun) to Trash", systemImage: "trash")
                }
                .disabled(isReadOnly || session.pages.count < 2)
            }
            Section {
                // A board is one plane with nothing to page through.
                if !board {
                    Toggle(isOn: $session.isPagedMode) {
                        Label("Paged Mode", systemImage: "rectangle.split.1x2")
                    }
                    .disabled(!session.hasCommands)
                }
                Button(action: actions.correctText) {
                    Label("Correct Recognized Text", systemImage: "text.viewfinder")
                }
                .disabled(isReadOnly)
                Button(action: actions.showHistory) {
                    Label("\(noun) History", systemImage: "clock.arrow.circlepath")
                }
                Button(action: actions.showInfo) {
                    Label("Info", systemImage: "info.circle")
                }
            }
            Section {
                Button(action: actions.showTrash) {
                    Label("Trash", systemImage: "trash.circle")
                }
                Button {
                    session.controller.syncNow()
                } label: {
                    Label("Sync Now", systemImage: "arrow.triangle.2.circlepath")
                }
            }
        } label: {
            NavigationBarGlyph(symbol: "ellipsis.circle")
        }
        .menuStyle(.button)
        .buttonStyle(.plain)
        .help("More")
        .accessibilityLabel("More")
    }

    private var currentPage: Page? {
        session.pages.indices.contains(session.currentIndex)
            ? session.pages[session.currentIndex] : nil
    }

    private var templateChoices: [Template] {
        session.notebook.kind == .whiteboard ? Template.boardTemplates : Template.paperTemplates
    }

    private var templateBinding: Binding<Template> {
        Binding(
            get: { currentPage?.template ?? session.notebook.template },
            set: { template in
                session.commands?.setPageStyle(
                    template: template, paperColor: nil, at: session.currentIndex)
            })
    }

    private var colorBinding: Binding<PaperColor> {
        Binding(
            get: { currentPage?.paperColor ?? session.notebook.paperColor },
            set: { color in
                session.commands?.setPageStyle(
                    template: nil, paperColor: color, at: session.currentIndex)
            })
    }
}
