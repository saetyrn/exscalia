import PaperCore
import SwiftUI
import UniformTypeIdentifiers

// MARK: - Thumbnails

/// A document's first page at its paper's shape, fitted into `box`: the
/// render when there is one, the paper and its ruling until then.
struct DocumentThumbnail: View {
    let controller: PaperController
    let notebook: Notebook
    let box: CGSize
    var cornerRadius: CGFloat = 4

    @State private var cover: LibraryCover?

    var body: some View {
        let aspect = cover?.aspect ?? PaperController.defaultAspect(for: notebook)
        let size = Self.fitted(aspect: aspect, in: box)
        page
            .frame(width: size.width, height: size.height)
            .clipShape(RoundedRectangle(cornerRadius: cornerRadius, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
                    .strokeBorder(Color.black.opacity(0.14), lineWidth: 0.5))
            .shadow(color: .black.opacity(0.28), radius: 1.5, y: 1)
            .frame(width: box.width, height: box.height)
            .task(id: controller.coverKey(for: notebook)) {
                cover = await controller.cover(for: notebook)
            }
            .accessibilityHidden(true)
    }

    @ViewBuilder
    private var page: some View {
        if let image = cover?.image {
            Image(uiImage: image)
                .resizable()
                .interpolation(.medium)
                .aspectRatio(contentMode: .fill)
        } else if cover?.isBoard ?? (notebook.kind == .whiteboard) {
            BoardPreview(
                template: cover?.template ?? notebook.template,
                paperColor: cover?.paperColor ?? notebook.paperColor, zoom: 0.2)
        } else {
            TemplatePreview(
                template: cover?.template ?? notebook.template,
                paperColor: cover?.paperColor ?? notebook.paperColor,
                pageSize: pageSize)
        }
    }

    private var pageSize: CGSize {
        let aspect = cover?.aspect ?? PaperController.defaultAspect(for: notebook)
        let points = notebook.paperSize == .infinite ? PaperSize.iPad.points : notebook.paperSize.points
        let longEdge = max(points.width, points.height, 1)
        return aspect >= 1
            ? CGSize(width: longEdge, height: longEdge / aspect)
            : CGSize(width: longEdge * aspect, height: longEdge)
    }

    static func fitted(aspect: CGFloat, in box: CGSize) -> CGSize {
        guard aspect > 0, box.width > 0, box.height > 0 else { return box }
        if box.width / box.height > aspect {
            return CGSize(width: (box.height * aspect).rounded(), height: box.height)
        }
        return CGSize(width: box.width, height: (box.width / aspect).rounded())
    }
}

/// A folder or a document, as the rows and tiles draw it.
struct LibraryThumbnail: View {
    let controller: PaperController
    let item: PaperCore.LibraryItem
    let box: CGSize

    var body: some View {
        switch item {
        case .folder(let folder):
            FolderThumbnail(
                color: folder.color, icon: folder.icon,
                size: min(box.width, box.height / FolderShape.aspect))
                .frame(width: box.width, height: box.height)
        case .document(let notebook):
            DocumentThumbnail(controller: controller, notebook: notebook, box: box)
        }
    }
}

// MARK: - Shared pieces

enum LibraryItemText {
    /// "Folder", "Notebook" or "Whiteboard".
    static func kind(_ item: PaperCore.LibraryItem) -> String {
        switch item {
        case .folder: return "Folder"
        case .document(let notebook): return notebook.kind.displayName
        }
    }

    /// What VoiceOver reads for a row: name, date, kind, and whether it is a
    /// favourite.
    static func accessibilityLabel(_ item: PaperCore.LibraryItem) -> String {
        var parts = [
            item.name.isEmpty ? "Untitled" : item.name,
            LibraryArrangement.dateLine(item.modified), kind(item),
        ]
        if item.isFavorite { parts.append("Favorite") }
        return parts.joined(separator: ", ")
    }
}

/// Dragging a row or a tile carries the ids of what is being dragged, so a
/// folder can take it. Plain text with a prefix nothing else uses: text from
/// anywhere else is refused.
enum LibraryDrag {
    static let prefix = "exscalia-paper-items:"

    static func payload(_ ids: [String]) -> String {
        prefix + ids.joined(separator: "\n")
    }

    static func ids(from payloads: [String]) -> [String] {
        payloads.flatMap { payload -> [String] in
            guard payload.hasPrefix(prefix) else { return [] }
            return payload.dropFirst(prefix.count).split(separator: "\n").map(String.init)
        }
    }
}

/// A folder takes what is dropped on it; anything else takes nothing.
private struct FolderDropTarget: ViewModifier {
    let folder: PaperFolder?
    let actions: LibraryItemActions
    @Binding var isTargeted: Bool

    func body(content: Content) -> some View {
        if let folder {
            content.dropDestination(for: String.self) { payloads, _ in
                actions.drop(LibraryDrag.ids(from: payloads), folder)
            } isTargeted: { isTargeted = $0 }
        } else {
            content
        }
    }
}

/// The star: outline, or filled for a favourite.
private struct FavoriteButton: View {
    let item: PaperCore.LibraryItem
    let actions: LibraryItemActions
    var size: CGFloat = 20
    var frame: CGFloat = 44

    var body: some View {
        Button {
            actions.toggleFavorite(item)
        } label: {
            Image(systemName: item.isFavorite ? "star.fill" : "star")
                .font(.system(size: size, weight: .light))
                .symbolRenderingMode(.monochrome)
                .foregroundStyle(PaperTokens.text)
                .frame(width: frame, height: frame)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help(item.isFavorite ? "Remove from Favorites" : "Add to Favorites")
        .accessibilityLabel("Favorite")
        .accessibilityValue(item.isFavorite ? "on" : "off")
        .accessibilityIdentifier("favorite:\(item.name)")
    }
}

/// The `chevron.down` that opens the item's menu.
private struct ItemMenuButton: View {
    let item: PaperCore.LibraryItem
    let actions: LibraryItemActions
    @Binding var menu: LibraryMenuRequest?
    var size: CGFloat = 22
    var frame: CGFloat = 44

    var body: some View {
        Button {
            menu = LibraryMenuRequest(itemID: item.id, page: .main)
        } label: {
            Image(systemName: "chevron.down")
                .font(.system(size: size, weight: .light))
                .symbolRenderingMode(.monochrome)
                .foregroundStyle(PaperTokens.text)
                .frame(width: frame, height: frame)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help("Menu")
        .accessibilityLabel("Item Menu")
        .accessibilityIdentifier("itemMenu:\(item.name)")
        .popover(
            isPresented: Binding(
                get: { menu?.itemID == item.id },
                set: { if !$0, menu?.itemID == item.id { menu = nil } })
        ) {
            LibraryItemMenu(
                item: item, page: menu?.page ?? .main, actions: actions,
                onDone: { menu = nil }
            )
            .presentationCompactAdaptation(.popover)
            .presentationBackground(PaperTokens.background)
        }
    }
}

/// Which item's menu is open, and at which page.
struct LibraryMenuRequest: Equatable {
    let itemID: String
    var page: LibraryItemMenu.Page
}

/// The selection mark in select mode.
private struct SelectionMark: View {
    let isSelected: Bool

    var body: some View {
        Image(systemName: isSelected ? "checkmark.circle.fill" : "circle")
            .font(.system(size: 22, weight: .regular))
            .symbolRenderingMode(.monochrome)
            .foregroundStyle(isSelected ? PaperTokens.newButton : PaperTokens.textMuted)
            .accessibilityHidden(true)
    }
}

// MARK: - Row

/// One row of the list: the thumbnail, the name over its date line, then the
/// menu chevron and the star, and a small chevron on a folder. Laid out on
/// the owner's screenshot: rows 84 points apart, the thumbnail in a 70 x 54
/// box, hairlines between.
struct LibraryRow: View {
    let controller: PaperController
    let item: PaperCore.LibraryItem
    let actions: LibraryItemActions
    @Binding var menu: LibraryMenuRequest?
    var isSelecting = false
    var isSelected = false
    var isKeyboardFocused = false
    /// What a drag of this row carries: the selection when it is part of it.
    var dragIDs: [String]

    @Environment(\.displayScale) private var displayScale
    @State private var isDropTargeted = false

    static let height: CGFloat = 84
    static let thumbnailBox = CGSize(width: 70, height: 54)

    var body: some View {
        HStack(spacing: 0) {
            if isSelecting {
                SelectionMark(isSelected: isSelected)
                    .frame(width: 36)
            }
            Button(action: primary) {
                HStack(spacing: 10) {
                    LibraryThumbnail(controller: controller, item: item, box: Self.thumbnailBox)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(item.name.isEmpty ? "Untitled" : item.name)
                            .font(.system(size: 16))
                            .foregroundStyle(PaperTokens.text)
                            .lineLimit(1)
                        Text(LibraryArrangement.dateLine(item.modified))
                            .font(.system(size: 12))
                            .foregroundStyle(PaperTokens.textMuted)
                            .lineLimit(1)
                    }
                    Spacer(minLength: 8)
                }
                .frame(maxHeight: .infinity)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(LibraryItemText.accessibilityLabel(item))
            .accessibilityAddTraits(isSelected ? [.isSelected] : [])
            .accessibilityIdentifier("libraryItem")

            if isSelecting {
                Color.clear.frame(width: 88, height: 1)
            } else {
                ItemMenuButton(item: item, actions: actions, menu: $menu)
                FavoriteButton(item: item, actions: actions)
            }
            Group {
                if case .folder = item {
                    Image(systemName: "chevron.right")
                        .font(.system(size: 15, weight: .regular))
                        .foregroundStyle(PaperTokens.textMuted)
                        .accessibilityHidden(true)
                } else {
                    Color.clear
                }
            }
            .frame(width: 20)
            .padding(.leading, 8)
        }
        .padding(.leading, 16)
        .padding(.trailing, 10)
        // The reference sits each row's content nearer the rule above it
        // than the one below: 20 points over the thumbnail, 10 under.
        .frame(height: Self.height - 10)
        .padding(.top, 10)
        .background(highlight)
        .overlay(alignment: .bottom) {
            Rectangle()
                .fill(PaperTokens.libraryHairline)
                .frame(height: 1 / max(displayScale, 1))
                .padding(.horizontal, 16)
                .accessibilityHidden(true)
        }
        .contentShape(Rectangle())
        .contextMenu { LibraryItemContextMenu(item: item, actions: actions) }
        .draggable(LibraryDrag.payload(dragIDs)) {
            HStack(spacing: 10) {
                LibraryThumbnail(controller: controller, item: item, box: Self.thumbnailBox)
                Text(item.name).font(.system(size: 17))
            }
            .padding(8)
        }
        .modifier(FolderDropTarget(folder: folder, actions: actions, isTargeted: $isDropTargeted))
        .accessibilityElement(children: .contain)
    }

    private var folder: PaperFolder? {
        if case .folder(let folder) = item { return folder }
        return nil
    }

    private func primary() {
        if isSelecting {
            actions.toggleSelection(item)
        } else {
            actions.open(item)
        }
    }

    @ViewBuilder
    private var highlight: some View {
        if isDropTargeted {
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(PaperTokens.newButton.opacity(0.22))
                .padding(.horizontal, 8)
        } else if isSelected {
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(PaperTokens.libraryChip)
                .padding(.horizontal, 8)
        } else if isKeyboardFocused {
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .strokeBorder(PaperTokens.newButton, lineWidth: 2)
                .padding(.horizontal, 8)
                .padding(.vertical, 2)
        }
    }
}

// MARK: - Tile

/// One tile of the grid: the thumbnail with its star, then the name with the
/// menu chevron beside it, then the date line.
struct LibraryTile: View {
    let controller: PaperController
    let item: PaperCore.LibraryItem
    let actions: LibraryItemActions
    @Binding var menu: LibraryMenuRequest?
    var isSelecting = false
    var isSelected = false
    var isKeyboardFocused = false
    var dragIDs: [String]

    @State private var isDropTargeted = false

    static let thumbnailHeight: CGFloat = 136
    static let minimumWidth: CGFloat = 168

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Button(action: primary) {
                GeometryReader { proxy in
                    LibraryThumbnail(
                        controller: controller, item: item,
                        box: CGSize(width: proxy.size.width, height: Self.thumbnailHeight - 12))
                        .frame(width: proxy.size.width, height: Self.thumbnailHeight, alignment: .bottom)
                }
                .frame(height: Self.thumbnailHeight)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(LibraryItemText.accessibilityLabel(item))
            .accessibilityAddTraits(isSelected ? [.isSelected] : [])
            .accessibilityIdentifier("libraryItem")
            .overlay(alignment: .topTrailing) {
                if !isSelecting {
                    FavoriteButton(item: item, actions: actions, size: 18, frame: 36)
                        .background(
                            Circle().fill(PaperTokens.libraryBackground.opacity(0.55))
                                .frame(width: 30, height: 30))
                        .padding(4)
                }
            }
            .overlay(alignment: .topLeading) {
                if isSelecting {
                    SelectionMark(isSelected: isSelected)
                        .background(Circle().fill(PaperTokens.libraryBackground).padding(2))
                        .padding(6)
                }
            }

            HStack(spacing: 0) {
                Text(item.name.isEmpty ? "Untitled" : item.name)
                    .font(.system(size: 15, weight: .medium))
                    .foregroundStyle(PaperTokens.text)
                    .lineLimit(1)
                    .accessibilityHidden(true)
                if !isSelecting {
                    ItemMenuButton(item: item, actions: actions, menu: $menu, size: 14, frame: 32)
                }
                Spacer(minLength: 0)
            }
            .frame(height: 32)
            Text(LibraryArrangement.dateLine(item.modified))
                .font(.system(size: 12))
                .foregroundStyle(PaperTokens.textMuted)
                .lineLimit(1)
                .accessibilityHidden(true)
        }
        .padding(8)
        .background(highlight)
        .contentShape(Rectangle())
        .contextMenu { LibraryItemContextMenu(item: item, actions: actions) }
        .draggable(LibraryDrag.payload(dragIDs)) {
            LibraryThumbnail(
                controller: controller, item: item, box: CGSize(width: 120, height: 96))
        }
        .modifier(FolderDropTarget(folder: folder, actions: actions, isTargeted: $isDropTargeted))
        .accessibilityElement(children: .contain)
    }

    private var folder: PaperFolder? {
        if case .folder(let folder) = item { return folder }
        return nil
    }

    private func primary() {
        if isSelecting {
            actions.toggleSelection(item)
        } else {
            actions.open(item)
        }
    }

    @ViewBuilder
    private var highlight: some View {
        if isDropTargeted {
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(PaperTokens.newButton.opacity(0.22))
        } else if isSelected {
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(PaperTokens.libraryChip)
        } else if isKeyboardFocused {
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .strokeBorder(PaperTokens.newButton, lineWidth: 2)
        }
    }
}
