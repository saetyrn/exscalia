import PaperCore
import SwiftUI

/// Where to move folders and documents: the folder tree, each folder opening
/// to show the ones inside it, a button that makes a folder where the
/// selection is, and Move. A folder can never go into itself or anything
/// inside it, so those rows are dimmed and cannot be chosen.
struct MovePickerSheet: View {
    let controller: PaperController
    let items: [PaperCore.LibraryItem]
    /// The folder they went into, by path.
    let onMoved: (String) -> Void
    let onCancel: () -> Void

    @State private var expanded: Set<String>
    @State private var target: String?
    @State private var isNaming = false
    @State private var newName = ""
    @State private var failed = false
    @State private var finished = false

    init(
        controller: PaperController, items: [PaperCore.LibraryItem],
        onMoved: @escaping (String) -> Void, onCancel: @escaping () -> Void
    ) {
        self.controller = controller
        self.items = items
        self.onMoved = onMoved
        self.onCancel = onCancel
        // Open down to where the items are now.
        var open: Set<String> = [PaperController.rootFolderPath]
        for item in items {
            var path = Self.location(of: item)
            while path != PaperController.rootFolderPath,
                path.hasPrefix(PaperController.rootFolderPath + "/")
            {
                open.insert(path)
                path = PaperController.parent(of: path)
            }
        }
        _expanded = State(initialValue: open)
    }

    var body: some View {
        NavigationStack {
            List {
                ForEach(rows) { node in
                    row(node)
                }
            }
            .listStyle(.plain)
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel", action: cancel)
                        .keyboardShortcut(.cancelAction)
                        .accessibilityLabel("Cancel")
                }
                ToolbarItemGroup(placement: .confirmationAction) {
                    Button {
                        newName = ""
                        isNaming = true
                    } label: {
                        Image(systemName: "folder.badge.plus")
                            .symbolRenderingMode(.monochrome)
                    }
                    .help("New Folder")
                    .accessibilityLabel("New Folder")
                    Button("Move", action: move)
                        .keyboardShortcut(.defaultAction)
                        .disabled(!canMove)
                        .accessibilityLabel("Move")
                }
            }
            .alert("New Folder", isPresented: $isNaming) {
                TextField("Name", text: $newName)
                    .accessibilityLabel("Name")
                Button("Cancel", role: .cancel) {}
                Button("Create", action: createFolder)
            }
            .alert("Could not move", isPresented: $failed) {
                Button("OK", role: .cancel) {}
            }
        }
        .presentationSizing(.form)
    }

    // MARK: Rows

    private var rows: [FolderTreeNode] {
        PaperController.folderTree(controller.libraryFolders, expanded: expanded)
    }

    private func row(_ node: FolderTreeNode) -> some View {
        let enabled = isAllowed(node.path)
        let selected = target == node.path
        return HStack(spacing: 8) {
            Group {
                if node.hasChildren {
                    Button {
                        toggle(node.path)
                    } label: {
                        Image(systemName: "chevron.right")
                            .font(.system(size: 13, weight: .semibold))
                            .symbolRenderingMode(.monochrome)
                            .rotationEffect(.degrees(expanded.contains(node.path) ? 90 : 0))
                            .foregroundStyle(PaperTokens.textMuted)
                            .frame(width: 32, height: 44)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(expanded.contains(node.path) ? "Collapse" : "Expand")
                    .accessibilityValue(node.name)
                } else {
                    Color.clear.frame(width: 32, height: 44)
                }
            }
            Button {
                target = node.path
            } label: {
                HStack(spacing: 10) {
                    if let folder = node.folder {
                        FolderThumbnail(color: folder.color, icon: folder.icon, size: 30)
                    } else {
                        Image(systemName: "folder")
                            .font(.system(size: 20, weight: .regular))
                            .symbolRenderingMode(.monochrome)
                            .frame(width: 30)
                            .accessibilityHidden(true)
                    }
                    Text(node.name)
                        .font(.body)
                        .lineLimit(1)
                    Spacer(minLength: 0)
                    if selected {
                        Image(systemName: "checkmark")
                            .font(.system(size: 15, weight: .semibold))
                            .foregroundStyle(PaperTokens.newButton)
                            .accessibilityHidden(true)
                    }
                }
                .foregroundStyle(PaperTokens.text)
                .frame(minHeight: 44)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(!enabled)
            .accessibilityLabel(node.name)
            .accessibilityAddTraits(selected ? [.isSelected] : [])
            .accessibilityIdentifier("moveTarget:\(node.name)")
        }
        .padding(.leading, CGFloat(node.depth) * 22)
        .opacity(enabled ? 1 : 0.35)
        .listRowBackground(selected ? PaperTokens.libraryChip : Color.clear)
        .listRowInsets(EdgeInsets(top: 0, leading: 8, bottom: 0, trailing: 16))
    }

    // MARK: Rules

    private var title: String {
        if items.count == 1, let item = items.first { return "Move \(item.name)" }
        return "Move \(items.count) Items"
    }

    private func isAllowed(_ path: String) -> Bool {
        PaperController.canMove(items, into: path, notebooks: controller.notebooks)
    }

    private var canMove: Bool {
        guard let target else { return false }
        return isAllowed(target) && !PaperController.isAlreadyIn(items, target)
    }

    private static func location(of item: PaperCore.LibraryItem) -> String {
        switch item {
        case .folder(let folder): return folder.parentPath
        case .document(let notebook): return notebook.folderPath
        }
    }

    private func toggle(_ path: String) {
        if expanded.contains(path) {
            expanded.remove(path)
        } else {
            expanded.insert(path)
        }
    }

    // MARK: Actions

    private func createFolder() {
        let parent = target.flatMap { isAllowed($0) ? $0 : nil } ?? PaperController.rootFolderPath
        guard let folder = controller.createFolder(name: newName, in: parent) else { return }
        expanded.insert(parent)
        target = folder.path
    }

    private func move() {
        guard !finished, let target, canMove else { return }
        if controller.move(items, toFolderPath: target) {
            finished = true
            onMoved(target)
        } else {
            failed = true
        }
    }

    private func cancel() {
        guard !finished else { return }
        finished = true
        onCancel()
    }
}
