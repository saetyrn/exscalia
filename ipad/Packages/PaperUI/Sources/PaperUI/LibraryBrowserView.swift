import PaperCore
import SwiftUI

/// What the library's chrome asks of it beyond a single item.
struct LibraryCommands {
    var new: (NewItemKind) -> Void
    var canScan: Bool
    var canTakePhoto: Bool
    var saveStateTapped: () -> Void
    var exit: () -> Void
    var openSearchResult: (SearchDestination) -> Void
    var exportSelection: () -> Void
    var duplicateSelection: () -> Void
    var moveSelection: () -> Void
    var trashSelection: () -> Void
    /// Items dropped on the back chip go up one folder. True when they moved.
    var dropOnBack: ([String]) -> Bool
}

/// The library's main column, laid out on the owner's screenshots of the
/// reference: a top bar (the sidebar chip, a back chip inside a folder with
/// the folder's name centred, search on the right), the large title at the
/// top of a section, the control row (filter, "+ New", views, save state),
/// then the rows or tiles.
struct LibraryBrowserView: View {
    let controller: PaperController
    @Bindable var state: LibraryState
    @Binding var sort: LibrarySort
    @Binding var filter: LibraryFilter
    @Binding var layout: LibraryLayout
    let items: [PaperCore.LibraryItem]
    let actions: LibraryItemActions
    let commands: LibraryCommands

    @FocusState private var searchFocused: Bool

    static let topBarHeight: CGFloat = 60
    static let gridSpacing: CGFloat = 16
    static let gridPadding: CGFloat = 12

    var body: some View {
        VStack(spacing: 0) {
            topBar
            ScrollViewReader { scroller in
                ScrollView {
                    VStack(alignment: .leading, spacing: 0) {
                        if state.isSearching {
                            searchField
                        } else {
                            if currentFolder == nil { largeTitle }
                            if state.section != .trash {
                                if state.isSelecting { selectionRow } else { controlRow }
                            }
                        }
                        content
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .scrollDismissesKeyboard(.interactively)
                .onChange(of: state.keyboardItemID) { _, id in
                    guard let id else { return }
                    withAnimation(UIAccessibility.isReduceMotionEnabled ? nil : .default) {
                        scroller.scrollTo(id)
                    }
                }
                .onAppear {
                    // Back from the editor: where the list was.
                    if let id = state.returnItemID ?? state.keyboardItemID {
                        scroller.scrollTo(id, anchor: .center)
                    }
                }
            }
        }
        .background(PaperTokens.libraryBackground)
        .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { width in
            let usable = width - Self.gridPadding * 2
            state.gridColumns = max(
                1, Int((usable + Self.gridSpacing) / (LibraryTile.minimumWidth + Self.gridSpacing)))
        }
        .onChange(of: state.searchFocusRequest) { _, _ in searchFocused = true }
        .onAppear { if state.isSearching { searchFocused = true } }
    }

    private var currentFolder: PaperFolder? { state.currentFolder(controller) }

    // MARK: Top bar

    private var topBar: some View {
        ZStack {
            HStack(spacing: 13) {
                chip("sidebar.left", "Sidebar") {
                    withAnimation(UIAccessibility.isReduceMotionEnabled ? nil : .snappy) {
                        state.isSidebarVisible.toggle()
                    }
                }
                .accessibilityValue(state.isSidebarVisible ? "shown" : "hidden")
                if currentFolder != nil {
                    chip("chevron.left", "Back", weight: .semibold) { state.goUp(controller) }
                        .dropDestination(for: String.self) { payloads, _ in
                            commands.dropOnBack(LibraryDrag.ids(from: payloads))
                        }
                }
                Spacer(minLength: 0)
                trailingPill
            }
            if let folder = currentFolder, !state.isSearching {
                Text(folder.name)
                    .font(.system(size: 17, weight: .semibold))
                    .foregroundStyle(PaperTokens.text)
                    .lineLimit(1)
                    .frame(maxWidth: 520)
                    .accessibilityAddTraits(.isHeader)
                    .accessibilityIdentifier("library.folderTitle")
            }
        }
        .padding(.horizontal, 10)
        .frame(height: Self.topBarHeight)
    }

    private func chip(
        _ symbol: String, _ label: String, weight: Font.Weight = .regular,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 19, weight: weight))
                .symbolRenderingMode(.monochrome)
                .foregroundStyle(PaperTokens.text)
                .frame(width: 44, height: 44)
                .background(Circle().fill(PaperTokens.libraryChip))
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .help(label)
        .accessibilityLabel(label)
    }

    /// Search, and the way back to the rest of EXSCALIA, where the reference
    /// has its bell and account.
    private var trailingPill: some View {
        HStack(spacing: 0) {
            pillButton("magnifyingglass", "Search") { state.beginSearch() }
            pillButton("globe", "Back to EXSCALIA", action: commands.exit)
        }
        .padding(.horizontal, 4)
        .background(Capsule().fill(PaperTokens.libraryChip))
    }

    private func pillButton(_ symbol: String, _ label: String, action: @escaping () -> Void)
        -> some View
    {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 19, weight: .regular))
                .symbolRenderingMode(.monochrome)
                .foregroundStyle(PaperTokens.text)
                .frame(width: 48, height: 44)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help(label)
        .accessibilityLabel(label)
    }

    // MARK: Title and control row

    private var largeTitle: some View {
        Text(state.section.title)
            .font(.system(size: 34, weight: .bold))
            .foregroundStyle(PaperTokens.text)
            .padding(.leading, 20)
            .padding(.top, 8)
            .padding(.bottom, 8)
            .accessibilityAddTraits(.isHeader)
            .accessibilityIdentifier("library.title")
    }

    private var controlRow: some View {
        HStack(spacing: 0) {
            filterMenu
            Spacer(minLength: 12)
            newButton
                .padding(.trailing, 4)
            viewsMenu
            Rectangle()
                .fill(PaperTokens.libraryHairline)
                .frame(width: 1, height: 26)
                .padding(.horizontal, 5)
                .accessibilityHidden(true)
            SaveStatePill(state: controller.saveState, style: .glyph, action: commands.saveStateTapped)
                .foregroundStyle(PaperTokens.text)
                .frame(width: 44, height: 44)
                .id(controller.saveStateVersion)
        }
        .padding(.leading, 20)
        .padding(.trailing, 19)
        .frame(height: 52)
        .padding(.top, currentFolder == nil ? 0 : 6)
    }

    private var filterMenu: some View {
        Menu {
            Picker("Show", selection: $filter) {
                ForEach(LibraryFilter.allCases, id: \.self) { option in
                    Text(option.displayName).tag(option)
                }
            }
            .pickerStyle(.inline)
            if state.section != .recents {
                Picker("Sort By", selection: $sort) {
                    ForEach(LibrarySort.allCases, id: \.self) { option in
                        Text(option.displayName).tag(option)
                    }
                }
                .pickerStyle(.inline)
            }
        } label: {
            HStack(spacing: 12) {
                Image(systemName: "line.3.horizontal.decrease")
                    .font(.system(size: 20, weight: .regular))
                    .symbolRenderingMode(.monochrome)
                Text(filter.displayName)
                    .font(.system(size: 20, weight: .regular))
            }
            .foregroundStyle(PaperTokens.text)
            .frame(height: 44)
            .contentShape(Rectangle())
        }
        .help("Filter and Sort")
        .accessibilityLabel("Filter")
        .accessibilityValue("\(filter.displayName), by \(sort.displayName)")
    }

    private var newButton: some View {
        Button {
            state.isShowingNewMenu = true
        } label: {
            HStack(spacing: 7) {
                Image(systemName: "plus")
                    .font(.system(size: 18, weight: .medium))
                Text("New")
                    .font(.system(size: 18, weight: .medium))
            }
            .foregroundStyle(.white)
            .padding(.horizontal, 16)
            .frame(height: 36)
            .background(Capsule().fill(PaperTokens.newButton))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("New")
        .popover(isPresented: $state.isShowingNewMenu) {
            NewItemMenu(canScan: commands.canScan, canTakePhoto: commands.canTakePhoto) { kind in
                commands.new(kind)
            }
            .presentationCompactAdaptation(.popover)
            .presentationBackground(PaperTokens.background)
        }
    }

    private var viewsMenu: some View {
        Menu {
            Button {
                layout = .grid
            } label: {
                Label("Grid View", systemImage: layout == .grid ? "checkmark" : "square.grid.2x2")
            }
            Button {
                layout = .list
            } label: {
                Label("List View", systemImage: layout == .list ? "checkmark" : "list.bullet")
            }
            Divider()
            Button {
                state.beginSelecting()
            } label: {
                Label("Select Items", systemImage: "checkmark.circle")
            }
            .disabled(items.isEmpty)
        } label: {
            Image(systemName: layout == .list ? "list.bullet" : "square.grid.2x2")
                .font(.system(size: 20, weight: .regular))
                .symbolRenderingMode(.monochrome)
                .foregroundStyle(PaperTokens.text)
                .frame(width: 44, height: 44)
                .contentShape(Rectangle())
        }
        .help("View")
        .accessibilityLabel("View")
        .accessibilityValue(layout == .list ? "List" : "Grid")
    }

    private var selectionRow: some View {
        let count = state.selection.count
        let allSelected = !items.isEmpty && items.allSatisfy { state.selection.contains($0.id) }
        let selected = items.filter { state.selection.contains($0.id) }
        let hasDocuments = selected.contains { $0.documentKind != nil }
        return HStack(spacing: 2) {
            Button(allSelected ? "Deselect All" : "Select All") {
                state.selection = allSelected ? [] : Set(items.map(\.id))
            }
            .font(.system(size: 17))
            .foregroundStyle(PaperTokens.newButton)
            .frame(height: 44)
            .accessibilityLabel(allSelected ? "Deselect All" : "Select All")
            Text(count == 1 ? "1 Selected" : "\(count) Selected")
                .font(.system(size: 15))
                .monospacedDigit()
                .foregroundStyle(PaperTokens.textMuted)
                .padding(.leading, 14)
            Spacer(minLength: 12)
            selectionAction("Export", "square.and.arrow.up", enabled: count > 0, commands.exportSelection)
            selectionAction(
                "Duplicate", "plus.square.on.square", enabled: hasDocuments, commands.duplicateSelection)
            selectionAction("Move", "folder", enabled: count > 0, commands.moveSelection)
            selectionAction("Trash", "trash", enabled: count > 0, commands.trashSelection)
            Button("Done") { state.endSelecting() }
                .font(.system(size: 17, weight: .semibold))
                .foregroundStyle(PaperTokens.newButton)
                .frame(minWidth: 56, minHeight: 44)
                .accessibilityLabel("Done")
        }
        .padding(.leading, 20)
        .padding(.trailing, 12)
        .frame(height: 52)
    }

    private func selectionAction(
        _ title: String, _ symbol: String, enabled: Bool, _ action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            Label(title, systemImage: symbol)
                .labelStyle(.titleAndIcon)
                .font(.system(size: 15))
                .foregroundStyle(PaperTokens.text)
                .padding(.horizontal, 10)
                .frame(height: 44)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
        .opacity(enabled ? 1 : 0.35)
        .accessibilityLabel(title)
    }

    // MARK: Search

    private var searchField: some View {
        HStack(spacing: 12) {
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass")
                    .font(.system(size: 17))
                    .foregroundStyle(PaperTokens.textMuted)
                    .accessibilityHidden(true)
                TextField("Search", text: $state.query)
                    .font(.system(size: 17))
                    .textFieldStyle(.plain)
                    .focused($searchFocused)
                    .submitLabel(.search)
                    .autocorrectionDisabled()
                    .accessibilityLabel("Search")
                    .accessibilityIdentifier("library.search")
                    // The field keeps Escape from the library's own key.
                    .onKeyPress(.escape) {
                        state.endSearch()
                        return .handled
                    }
                if !state.query.isEmpty {
                    Button {
                        state.query = ""
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .foregroundStyle(PaperTokens.textMuted)
                            .frame(width: 32, height: 32)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Clear")
                }
            }
            .padding(.horizontal, 12)
            .frame(height: 40)
            .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(PaperTokens.libraryChip))
            Button("Cancel") { state.endSearch() }
                .font(.system(size: 17))
                .foregroundStyle(PaperTokens.newButton)
                .frame(minHeight: 44)
                .accessibilityLabel("Cancel")
        }
        .padding(.horizontal, 20)
        .padding(.top, 6)
        .padding(.bottom, 4)
    }

    // MARK: Content

    @ViewBuilder
    private var content: some View {
        if state.isSearching {
            SearchView(
                controller: controller, query: state.query,
                onOpenFolder: { folder in
                    state.endSearch()
                    state.open(folder)
                },
                onOpen: commands.openSearchResult)
        } else if state.section == .trash {
            TrashView(controller: controller, embedded: true)
        } else if layout == .grid {
            grid
        } else {
            list
        }
    }

    private var list: some View {
        LazyVStack(spacing: 0) {
            ForEach(items) { item in
                LibraryRow(
                    controller: controller, item: item, actions: actions, menu: $state.menu,
                    isSelecting: state.isSelecting, isSelected: state.selection.contains(item.id),
                    isKeyboardFocused: state.keyboardItemID == item.id, dragIDs: dragIDs(item)
                )
                .id(item.id)
            }
        }
        .padding(.top, 4)
        .padding(.bottom, 40)
    }

    private var grid: some View {
        LazyVGrid(
            columns: [
                GridItem(
                    .adaptive(minimum: LibraryTile.minimumWidth, maximum: 240),
                    spacing: Self.gridSpacing, alignment: .top)
            ],
            spacing: 18
        ) {
            ForEach(items) { item in
                LibraryTile(
                    controller: controller, item: item, actions: actions, menu: $state.menu,
                    isSelecting: state.isSelecting, isSelected: state.selection.contains(item.id),
                    isKeyboardFocused: state.keyboardItemID == item.id, dragIDs: dragIDs(item)
                )
                .id(item.id)
            }
        }
        .padding(.horizontal, Self.gridPadding)
        .padding(.top, 8)
        .padding(.bottom, 40)
    }

    /// A drag carries the selection when the dragged item is part of it.
    private func dragIDs(_ item: PaperCore.LibraryItem) -> [String] {
        if state.isSelecting, state.selection.contains(item.id) {
            return items.map(\.id).filter(state.selection.contains)
        }
        return [item.id]
    }
}

// MARK: - New menu

/// What "+ New" offers, in order.
enum NewItemKind: String, CaseIterable, Identifiable, Sendable {
    case notebook, whiteboard, quickNote, folder, importPDF, scan, image, takePhoto

    var id: String { rawValue }

    var title: String {
        switch self {
        case .notebook: return "Notebook"
        case .whiteboard: return "Whiteboard"
        case .quickNote: return "Quick Note"
        case .folder: return "Folder"
        case .importPDF: return "Import"
        case .scan: return "Scan Documents"
        case .image: return "Image"
        case .takePhoto: return "Take Photo"
        }
    }

    var symbol: String {
        switch self {
        case .notebook: return "book.closed"
        case .whiteboard: return "rectangle.dashed"
        case .quickNote: return "square.and.pencil"
        case .folder: return "folder.badge.plus"
        case .importPDF: return "square.and.arrow.down"
        case .scan: return "doc.viewfinder"
        case .image: return "photo"
        case .takePhoto: return "camera"
        }
    }

    /// The hardware-keyboard shortcut, as the menu shows it.
    var shortcut: String? {
        switch self {
        case .notebook: return "⌥⌘N"
        case .whiteboard: return "⇧⌘W"
        case .quickNote: return "⇧⌘N"
        default: return nil
        }
    }
}

/// The "+ New" popover: the three documents, a folder, then the ways in
/// from outside.
struct NewItemMenu: View {
    let canScan: Bool
    let canTakePhoto: Bool
    let onPick: (NewItemKind) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach([NewItemKind.notebook, .whiteboard, .quickNote, .folder]) { kind in
                row(kind)
            }
            Divider().padding(.vertical, 4)
            ForEach([NewItemKind.importPDF, .scan, .image, .takePhoto]) { kind in
                row(kind)
            }
        }
        .padding(.vertical, 6)
        .frame(width: 280)
    }

    private func row(_ kind: NewItemKind) -> some View {
        let enabled = (kind != .scan || canScan) && (kind != .takePhoto || canTakePhoto)
        return Button {
            onPick(kind)
        } label: {
            HStack(spacing: 12) {
                Image(systemName: kind.symbol)
                    .font(.system(size: 17))
                    .symbolRenderingMode(.monochrome)
                    .frame(width: 24)
                    .accessibilityHidden(true)
                Text(kind.title)
                    .font(.body)
                Spacer(minLength: 8)
                if let shortcut = kind.shortcut {
                    Text(shortcut)
                        .font(.callout)
                        .foregroundStyle(PaperTokens.textMuted)
                        .accessibilityHidden(true)
                }
            }
            .foregroundStyle(PaperTokens.text)
            .padding(.horizontal, 16)
            .frame(height: PaperTokens.minimumTarget)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
        .opacity(enabled ? 1 : 0.4)
        .accessibilityLabel(kind.title)
    }
}
