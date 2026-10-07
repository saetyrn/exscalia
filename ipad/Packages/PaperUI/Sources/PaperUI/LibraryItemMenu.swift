import PaperCore
import SwiftUI

/// Everything a row, a tile or a menu can do to a library item. The library
/// builds one and hands it down, so a row holds no logic of its own.
struct LibraryItemActions {
    var open: (PaperCore.LibraryItem) -> Void
    var toggleFavorite: (PaperCore.LibraryItem) -> Void
    var toggleSelection: (PaperCore.LibraryItem) -> Void
    var rename: (PaperCore.LibraryItem, String) -> Void
    var duplicate: (PaperCore.LibraryItem) -> Void
    var move: (PaperCore.LibraryItem) -> Void
    var openInNewWindow: (PaperCore.LibraryItem) -> Void
    var export: (PaperCore.LibraryItem) -> Void
    var info: (PaperCore.LibraryItem) -> Void
    var trash: (PaperCore.LibraryItem) -> Void
    var setColor: (PaperFolder, FolderColor) -> Void
    var setIcon: (PaperFolder, FolderIcon?) -> Void
    /// Items dragged onto a folder. True when they moved.
    var drop: ([String], PaperFolder) -> Bool
    /// Opens the item's menu, at a page of it.
    var showMenu: (PaperCore.LibraryItem, LibraryItemMenu.Page) -> Void
}

/// What the `chevron.down` on a row or a tile opens: the name, which renames
/// on Return or when the menu closes, and what can be done with the item.
///
/// | Entry | Notebook | Whiteboard | Folder |
/// |---|---|---|---|
/// | Duplicate | yes | yes | |
/// | Move | yes | yes | yes |
/// | Convert to Whiteboard | yes | | |
/// | Open in New Window, Export, Info | yes | yes | yes |
/// | Color, Icon | | | yes |
/// | Move to Trash | yes | yes | yes |
struct LibraryItemMenu: View {
    enum Page: Equatable {
        case main, color, icon
    }

    let item: PaperCore.LibraryItem
    let actions: LibraryItemActions
    let onDone: () -> Void

    @Environment(\.paperConvertDocument) private var convertAction

    @State private var name: String
    @State private var page: Page
    @State private var color: FolderColor
    @State private var icon: FolderIcon?
    @FocusState private var nameFocused: Bool

    init(
        item: PaperCore.LibraryItem, page: Page = .main, actions: LibraryItemActions,
        onDone: @escaping () -> Void
    ) {
        self.item = item
        self.actions = actions
        self.onDone = onDone
        _name = State(initialValue: item.name)
        _page = State(initialValue: page)
        if case .folder(let folder) = item {
            _color = State(initialValue: folder.color)
            _icon = State(initialValue: folder.icon)
        } else {
            _color = State(initialValue: .blue)
            _icon = State(initialValue: nil)
        }
    }

    var body: some View {
        Group {
            switch page {
            case .main: main
            case .color: colorPage
            case .icon: iconPage
            }
        }
        .frame(width: 320)
        .onDisappear { actions.rename(item, name) }
    }

    // MARK: Pages

    private var main: some View {
        VStack(alignment: .leading, spacing: 0) {
            TextField("Name", text: $name)
                .font(.headline)
                .textFieldStyle(.plain)
                .submitLabel(.done)
                .focused($nameFocused)
                .onSubmit {
                    actions.rename(item, name)
                    onDone()
                }
                .padding(.horizontal, 16)
                .frame(height: 52)
                .accessibilityLabel("Name")
                .accessibilityIdentifier("itemMenu.name")
            Divider()
            if isDocument {
                row("Duplicate", "plus.square.on.square") { run(actions.duplicate) }
            }
            row("Move", "folder") { run(actions.move) }
            if case .document(let notebook) = item,
                convertAction.isOffered(notebook)
            {
                ConvertToWhiteboardRow(notebook: notebook) {
                    actions.rename(item, name)
                    onDone()
                }
            }
            row("Open in New Window", "macwindow.badge.plus") { run(actions.openInNewWindow) }
            row("Export", "square.and.arrow.up") { run(actions.export) }
            row("Info", "info.circle") { run(actions.info) }
            if case .folder = item {
                row("Color", "paintpalette", chevron: true) { page = .color }
                row("Icon", "face.smiling", chevron: true) { page = .icon }
            }
            Divider()
            // No question asked: the toast that follows offers it back.
            row("Move to Trash", "trash", role: .destructive) { run(actions.trash) }
        }
        .padding(.bottom, 6)
    }

    private var colorPage: some View {
        VStack(alignment: .leading, spacing: 0) {
            subpageHeader("Color")
            FolderColorPicker(selection: $color)
                .padding(.horizontal, 12)
                .padding(.bottom, 10)
        }
        .onChange(of: color) { _, value in
            if case .folder(let folder) = item { actions.setColor(folder, value) }
        }
    }

    private var iconPage: some View {
        VStack(alignment: .leading, spacing: 0) {
            subpageHeader("Icon")
            ScrollView {
                VStack(alignment: .leading, spacing: 8) {
                    FolderIconPicker(selection: $icon, onSubmit: onDone)
                }
                .padding(.horizontal, 12)
                .padding(.bottom, 10)
            }
            .frame(maxHeight: 420)
        }
        .onChange(of: icon) { _, value in
            if case .folder(let folder) = item { actions.setIcon(folder, value) }
        }
    }

    private func subpageHeader(_ title: String) -> some View {
        HStack(spacing: 4) {
            Button {
                page = .main
            } label: {
                Image(systemName: "chevron.backward")
                    .font(.system(size: 17, weight: .medium))
                    .symbolRenderingMode(.monochrome)
                    .frame(width: 44, height: 44)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .foregroundStyle(PaperTokens.text)
            .help("Back")
            .accessibilityLabel("Back")
            Text(title)
                .font(.headline)
                .foregroundStyle(PaperTokens.text)
                .accessibilityAddTraits(.isHeader)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 4)
        .frame(height: 52)
    }

    // MARK: Pieces

    private var isDocument: Bool {
        if case .document = item { return true }
        return false
    }

    /// Saves the name, acts, and closes the menu. A sheet the action asks for
    /// waits until the popover has gone (`LibraryView.present`).
    private func run(_ action: @escaping (PaperCore.LibraryItem) -> Void) {
        actions.rename(item, name)
        action(item)
        onDone()
    }

    private func row(
        _ title: String, _ symbol: String, role: ButtonRole? = nil, disabled: Bool = false,
        chevron: Bool = false, action: @escaping () -> Void
    ) -> some View {
        Button(role: role, action: action) {
            HStack(spacing: 12) {
                Text(title)
                Spacer(minLength: 0)
                if chevron {
                    Image(systemName: "chevron.forward")
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(PaperTokens.textMuted)
                        .accessibilityHidden(true)
                }
                Image(systemName: symbol)
                    .symbolRenderingMode(.monochrome)
                    .frame(width: 22)
                    .accessibilityHidden(true)
            }
            .font(.body)
            .foregroundStyle(role == .destructive ? Color.red : PaperTokens.text)
            .padding(.horizontal, 16)
            .frame(height: PaperTokens.minimumTarget)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(disabled)
        .opacity(disabled ? 0.4 : 1)
        .accessibilityLabel(title)
    }
}

/// The same entries on a long press, for a row or a tile. Rename and Icon
/// open the item's menu, which holds the name field and the emoji field.
struct LibraryItemContextMenu: View {
    let item: PaperCore.LibraryItem
    let actions: LibraryItemActions

    @Environment(\.paperConvertDocument) private var convertAction

    var body: some View {
        Button {
            actions.open(item)
        } label: {
            Label("Open", systemImage: "arrow.up.forward.square")
        }
        Button {
            actions.showMenu(item, .main)
        } label: {
            Label("Rename", systemImage: "pencil")
        }
        if case .document = item {
            Button {
                actions.duplicate(item)
            } label: {
                Label("Duplicate", systemImage: "plus.square.on.square")
            }
        }
        Button {
            actions.move(item)
        } label: {
            Label("Move", systemImage: "folder")
        }
        if case .document(let notebook) = item,
            convertAction.isOffered(notebook)
        {
            ConvertToWhiteboardMenuButton(notebook: notebook)
        }
        Button {
            actions.toggleFavorite(item)
        } label: {
            Label(
                item.isFavorite ? "Remove from Favorites" : "Add to Favorites",
                systemImage: item.isFavorite ? "star.slash" : "star")
        }
        Button {
            actions.openInNewWindow(item)
        } label: {
            Label("Open in New Window", systemImage: "macwindow.badge.plus")
        }
        Button {
            actions.export(item)
        } label: {
            Label("Export", systemImage: "square.and.arrow.up")
        }
        Button {
            actions.info(item)
        } label: {
            Label("Info", systemImage: "info.circle")
        }
        if case .folder(let folder) = item {
            Menu {
                ForEach(FolderColor.allCases, id: \.self) { color in
                    Button {
                        actions.setColor(folder, color)
                    } label: {
                        if color == folder.color {
                            Label(color.displayName, systemImage: "checkmark")
                        } else {
                            Text(color.displayName)
                        }
                    }
                }
            } label: {
                Label("Color", systemImage: "paintpalette")
            }
            Button {
                actions.showMenu(item, .icon)
            } label: {
                Label("Icon", systemImage: "face.smiling")
            }
        }
        Divider()
        Button(role: .destructive) {
            actions.trash(item)
        } label: {
            Label("Move to Trash", systemImage: "trash")
        }
    }
}
