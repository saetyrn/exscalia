import Foundation
import GRDB

// MARK: - Paths

/// Folder paths, root kept (`paper`, `paper/Work`), compared and rewritten by
/// whole components and by Unicode scalars, as SQLite's `substr` counts them.
enum FolderPath {
    static let root = "paper"
    /// The longest vault path a folder may give anything inside it, so
    /// `/pages/<id>.conflict-<stamp>/drawing.pkdrawing` still fits PATH_MAX.
    static let maximumBytes = 900

    /// `path` is `prefix` or inside it.
    static func isUnder(_ path: String, _ prefix: String) -> Bool {
        let whole = Array(path.unicodeScalars)
        let head = Array(prefix.unicodeScalars)
        guard whole.count >= head.count, whole.starts(with: head) else { return false }
        return whole.count == head.count || whole[head.count] == "/"
    }

    /// `path` with its leading `from` replaced by `to`; callers check
    /// `isUnder` first.
    static func replacingPrefix(of path: String, from: String, to: String) -> String {
        to + String(path.unicodeScalars.dropFirst(from.unicodeScalars.count))
    }

    /// `path` moved along with `from`, or as it was when it is not inside it.
    static func movingPrefix(_ path: String, from: String, to: String) -> String {
        isUnder(path, from) ? replacingPrefix(of: path, from: from, to: to) : path
    }

    /// `paper` for a top-level entry, `paper/Work` for `paper/Work/Ideas`.
    static func parent(of path: String) -> String {
        guard let slash = path.lastIndex(of: "/") else { return root }
        return String(path[..<slash])
    }

    static func leaf(of path: String) -> String {
        path.split(separator: "/").last.map(String.init) ?? path
    }

    static func join(_ parent: String, _ leaf: String) -> String { "\(parent)/\(leaf)" }

    /// `Work/Plans` for `paper/Work/Plans`, `""` for the root: what
    /// `Notebook.folder` and the `notebooks.folder` column hold.
    static func unprefixed(_ folderPath: String) -> String {
        folderPath == root ? "" : String(folderPath.dropFirst(root.count + 1))
    }

    /// The form two entry names are compared in, as the server's `fold`:
    /// NFC, then case folded.
    static func fold(_ name: String) -> String {
        name.precomposedStringWithCanonicalMapping.folding(
            options: [.caseInsensitive], locale: nil)
    }

    /// `paper`, or `paper/...` whose components are real names, none of them
    /// a package: a folder can never sit inside a notebook.
    static func isValidFolder(_ path: String) -> Bool {
        guard path == root || path.hasPrefix(root + "/") else { return false }
        let parts = path.split(separator: "/", omittingEmptySubsequences: false)
        return parts.allSatisfy { part in
            !part.isEmpty && part != "." && part != ".." && !part.hasPrefix(".")
                && !part.hasSuffix(".paper") && !part.contains("\\")
        }
    }
}

/// The folder renames and deletes still in the queue, in queue order: what
/// the server will do to paths as the queue drains. A feed path is where the
/// server has something now; `local` is where this device keeps it.
struct QueuedFolderChanges {
    private(set) var moves: [(source: String, target: String)] = []
    private(set) var deletes: [String] = []

    init(_ queue: some Sequence<PaperOp>) {
        for op in queue {
            switch op.kind {
            case .folderRename:
                if let target = op.payload["new_path"] { moves.append((op.notebookPath, target)) }
            case .folderDelete:
                deletes.append(op.notebookPath)
            default:
                break
            }
        }
    }

    /// Where this device has what the server has at `serverPath`.
    func local(_ serverPath: String) -> String {
        moves.reduce(serverPath) { FolderPath.movingPrefix($0, from: $1.source, to: $1.target) }
    }

    /// Where the server has what this device has at `localPath`.
    func server(_ localPath: String) -> String {
        moves.reversed().reduce(localPath) {
            FolderPath.movingPrefix($0, from: $1.target, to: $1.source)
        }
    }

    /// A delete still queued here covers it: applying the server's news
    /// about it would only bring back what the user threw away.
    func isDeleted(_ serverPath: String) -> Bool {
        let local = local(serverPath)
        return deletes.contains {
            FolderPath.isUnder(serverPath, $0) || FolderPath.isUnder(local, $0)
        }
    }
}

// MARK: - Folders

/// Folders on this device: the directories under `paper/` with their
/// `folder.json`, indexed in the `folders` table. Every write here goes file
/// first and row second, as notebooks do, and journals what the server needs
/// to hear. A folder's id never changes except through the merge rule: the
/// server answered a create with another folder's id, or a record arrived for
/// a directory this device knew by another id.
extension PaperStore {

    // MARK: Reading

    /// Every folder, sorted by path.
    public func folderRecords() throws -> [PaperFolder] {
        try database.queue.read { db in
            try Row.fetchAll(db, sql: "SELECT * FROM folders ORDER BY path").map(Self.folder(from:))
        }
    }

    public func folder(id: String) throws -> PaperFolder {
        guard let folder = try folderIfPresent(id: id) else { throw PaperStoreError.notFound(id) }
        return folder
    }

    public func folder(path: String) throws -> PaperFolder? {
        try database.queue.read { db in
            try Row.fetchOne(db, sql: "SELECT * FROM folders WHERE path = ?", arguments: [path])
                .map(Self.folder(from:))
        }
    }

    /// What the library shows inside `folderPath` (`paper` for the top
    /// level): its folders by path, its documents newest first.
    public func contents(ofFolder folderPath: String) throws -> (
        folders: [PaperFolder], documents: [Notebook]
    ) {
        let parent = folderPath.isEmpty ? FolderPath.root : folderPath
        let folders = try database.queue.read { db in
            try Row.fetchAll(
                db, sql: "SELECT * FROM folders WHERE parent = ? ORDER BY path", arguments: [parent]
            ).map(Self.folder(from:))
        }
        return (folders, try notebooks().filter { $0.folderPath == parent })
    }

    func folderIfPresent(id: String) throws -> PaperFolder? {
        try database.queue.read { db in
            try Row.fetchOne(db, sql: "SELECT * FROM folders WHERE id = ?", arguments: [id])
                .map(Self.folder(from:))
        }
    }

    // MARK: Writing

    /// A new folder called `name` in the folder at `parentPath` (`paper` for
    /// the top level). Its directory is `folderSlug(name)`; when a folder or
    /// notebook beside it already has that name the new one is called
    /// "<name> 2", "<name> 3" and so on -- named, not only placed, so its
    /// create can never be taken for another device's folder of the same
    /// name and merged into it.
    public func createFolder(
        name: String, in parentPath: String, color: FolderColor = .blue, icon: FolderIcon? = nil
    ) throws -> PaperFolder {
        writeLock.lock()
        defer { writeLock.unlock() }
        let parent = parentPath.isEmpty ? FolderPath.root : parentPath
        guard FolderPath.isValidFolder(parent) else { throw PaperStoreError.unsafePath(parentPath) }
        let base = Self.folderName(name)
        let taken = takenEntryNames(in: parent)
        var chosen = base
        var number = 2
        while taken.contains(FolderPath.fold(PaperFolderID.folderSlug(chosen))) {
            chosen = "\(base) \(number)"
            number += 1
        }
        let path = FolderPath.join(parent, PaperFolderID.folderSlug(chosen))
        guard Self.fitsPathLimit(path) else { throw PaperStoreError.unsafePath(path) }

        try ensureFolderRecords(forAncestorsOf: path)
        try FileManager.default.createDirectory(
            at: paths.url(forVaultPath: path), withIntermediateDirectories: true)
        let now = Date()
        let folder = PaperFolder(
            name: chosen, color: color, icon: icon, created: now, modified: now, path: path)
        try writeFolderRecord(folder)
        try journal.append(
            .init(
                kind: .folderCreate, notebookPath: path, notebookID: folder.id,
                payload: [
                    "name": folder.name, "color": folder.color.rawValue,
                    "icon": folder.icon?.wire ?? "", "favorite": folder.favorite ? "true" : "false",
                ]))
        return folder
    }

    /// Renames the folder in place. Its directory follows the new name; a
    /// sibling holding that name already pushes it to "<name> 2", as the
    /// server does, while the name stays as typed. A name that only changes
    /// case goes through a temporary `.rename-<id>`.
    public func renameFolder(id: String, to name: String) throws -> PaperFolder {
        writeLock.lock()
        defer { writeLock.unlock() }
        var folder = try folder(id: id)
        let name = Self.folderName(name)
        let parent = folder.parentPath
        let current = FolderPath.leaf(of: folder.path)
        let slug = PaperFolderID.folderSlug(name)
        let leaf =
            FolderPath.fold(slug) == FolderPath.fold(current)
            ? slug : freeEntryName(in: parent, base: slug, excluding: current)
        let newPath = FolderPath.join(parent, leaf)
        guard newPath != folder.path || name != folder.name else { return folder }
        let oldPath = folder.path
        if newPath != oldPath {
            guard fitsPathLimit(moving: oldPath, to: newPath) else {
                throw PaperStoreError.unsafePath(newPath)
            }
            try moveFolderTree(from: oldPath, to: newPath, id: id)
        }
        folder.path = newPath
        folder.name = name
        folder.modified = Date()
        try writeFolderRecord(folder)
        try journalFolderMove(id: id, from: oldPath, to: newPath, name: name)
        return folder
    }

    /// Moves the folder, with everything in it, into the folder at
    /// `parentPath`, keeping its name. Never into itself or a folder inside
    /// it.
    public func moveFolder(id: String, into parentPath: String) throws -> PaperFolder {
        writeLock.lock()
        defer { writeLock.unlock() }
        var folder = try folder(id: id)
        let parent = parentPath.isEmpty ? FolderPath.root : parentPath
        guard FolderPath.isValidFolder(parent), !FolderPath.isUnder(parent, folder.path) else {
            throw PaperStoreError.unsafePath(parentPath)
        }
        guard parent != folder.parentPath else { return folder }
        let oldPath = folder.path
        let newPath = FolderPath.join(
            parent, freeEntryName(in: parent, base: FolderPath.leaf(of: oldPath), excluding: nil))
        guard fitsPathLimit(moving: oldPath, to: newPath) else {
            throw PaperStoreError.unsafePath(newPath)
        }
        try moveFolderTree(from: oldPath, to: newPath, id: id)
        folder.path = newPath
        folder.modified = Date()
        try writeFolderRecord(folder)
        try journalFolderMove(id: id, from: oldPath, to: newPath, name: nil)
        return folder
    }

    /// Colour and icon. `nil` leaves one as it is; `icon: .some(nil)` clears
    /// the icon. A change that changes nothing writes nothing.
    public func setFolderAppearance(id: String, color: FolderColor?, icon: FolderIcon??) throws
        -> PaperFolder
    {
        writeLock.lock()
        defer { writeLock.unlock() }
        var folder = try folder(id: id)
        var payload: [String: String] = [:]
        if let color, color != folder.color {
            folder.color = color
            payload["color"] = color.rawValue
        }
        if let icon, icon != folder.icon {
            folder.icon = icon
            payload["icon"] = icon?.wire ?? ""
        }
        return try patchFolder(folder, payload)
    }

    public func setFolderFavorite(id: String, _ favorite: Bool) throws -> PaperFolder {
        writeLock.lock()
        defer { writeLock.unlock() }
        var folder = try folder(id: id)
        guard favorite != folder.favorite else { return folder }
        folder.favorite = favorite
        return try patchFolder(folder, ["favorite": favorite ? "true" : "false"])
    }

    /// Moves the folder and everything in it into the device trash and
    /// returns the token the undo toast holds. Nothing is unlinked.
    public func deleteFolder(id: String) throws -> TrashToken {
        writeLock.lock()
        defer { writeLock.unlock() }
        let folder = try folder(id: id)
        let token = TrashToken(id: PaperID.make(), kind: .folder, title: folder.name)
        let destination = paths.trash.appending(path: token.id, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: destination, withIntermediateDirectories: true)
        let dropped = try journal.unpushed(underFolder: folder.path)
        try AtomicFile.writeJSON(
            TrashManifest(
                kind: .folder, notebookPath: folder.path, notebookID: folder.id, pageID: nil,
                position: nil, deletedAt: Date(), droppedOps: dropped),
            to: destination.appending(path: "manifest.json", directoryHint: .notDirectory))
        try FileManager.default.moveItem(
            at: paths.url(forVaultPath: folder.path),
            to: destination.appending(path: "folder", directoryHint: .isDirectory))
        // Journalled while the index still knows what was inside: the append
        // drops the same ops `unpushed(underFolder:)` just listed.
        try journal.append(
            .init(kind: .folderDelete, notebookPath: folder.path, notebookID: folder.id))
        try removeRows(under: folder.path)
        return token
    }

    /// Undo for `deleteFolder`: back where it was, or beside it as
    /// "<name> 2" if something took the name meanwhile. A delete that never
    /// left the device is cancelled and the work it dropped is queued again;
    /// one the server already has in its Trash is answered by sending the
    /// folder up as new, every folder and notebook in it.
    func restoreFolder(_ manifest: TrashManifest, from directory: URL) throws {
        writeLock.lock()
        defer { writeLock.unlock() }
        let original = manifest.notebookPath
        let parent = FolderPath.parent(of: original)
        try ensureFolderRecords(forAncestorsOf: original)
        let target = FolderPath.join(
            parent, freeEntryName(in: parent, base: FolderPath.leaf(of: original), excluding: nil))
        try FileManager.default.moveItem(
            at: directory.appending(path: "folder", directoryHint: .isDirectory),
            to: paths.url(forVaultPath: target))
        try? FileManager.default.removeItem(at: directory)
        let deleteNeverLeft = try journal.cancelUnpushed(
            kind: .folderDelete, notebookPath: original)
        try indexFolderTree(at: target)

        let dropped = manifest.droppedOps ?? []
        let createDropped = dropped.contains {
            $0.kind == .folderCreate && $0.notebookID == manifest.notebookID
        }
        if deleteNeverLeft || createDropped {
            if createDropped, target != original {
                // Its create sends its name; the name it had belongs to the
                // folder that took its place, and would be merged into it.
                try retitleLocalFolder(id: manifest.notebookID, to: FolderPath.leaf(of: target))
            }
            for op in dropped {
                var op = op
                op.notebookPath = FolderPath.movingPrefix(op.notebookPath, from: original, to: target)
                for key in ["new_path", "path"] {
                    if let value = op.payload[key] {
                        op.payload[key] = FolderPath.movingPrefix(value, from: original, to: target)
                    }
                }
                try journal.append(op)
            }
            return
        }
        // Folders first, from the top: a notebook created into a folder the
        // server does not have yet would make it with a legacy id.
        for folder in try folderRecords() where FolderPath.isUnder(folder.path, target) {
            try journalCreate(of: folder)
        }
        for notebook in try notebooks().sorted(by: { $0.path < $1.path })
        where FolderPath.isUnder(notebook.path, target) {
            try requeueAsNew(notebook)
        }
    }

    // MARK: Sync

    /// Moves a folder's directory -- or a notebook package -- from `oldPath`
    /// to `newPath` on this device, with every row under it, journalling
    /// nothing: the server has already done it, or the caller re-points the
    /// queue itself. Returns the paths that changed, before and after, for
    /// the library and any open notebook to follow.
    func relocateTree(from oldPath: String, to newPath: String) throws -> Set<String> {
        writeLock.lock()
        defer { writeLock.unlock() }
        guard oldPath != newPath,
            FileManager.default.fileExists(
                atPath: paths.url(forVaultPath: oldPath).path(percentEncoded: false))
        else { return [] }
        let notebooks = try database.queue.read { db in
            try String.fetchAll(
                db, sql: "SELECT path FROM notebooks WHERE \(Self.under("path"))",
                arguments: ["p": oldPath])
        }
        let id = try folder(path: oldPath)?.id
        try moveFolderTree(from: oldPath, to: newPath, id: id)
        var changed: Set<String> = [oldPath, newPath]
        for path in notebooks {
            changed.insert(path)
            changed.insert(FolderPath.replacingPrefix(of: path, from: oldPath, to: newPath))
        }
        return changed
    }

    /// Takes the server's word for a folder this device has: name, colour,
    /// icon, favourite, dates and seq, except the fields named in `keeping`,
    /// which this device has changed and not yet sent -- its push comes after
    /// and wins. True when something the library shows changed.
    @discardableResult
    func updateFolder(id: String, from record: FolderRecord, keeping: Set<String> = []) throws
        -> Bool
    {
        writeLock.lock()
        defer { writeLock.unlock() }
        guard var folder = try folderIfPresent(id: id) else { return false }
        let before = folder
        if !keeping.contains("name"), !record.name.isEmpty { folder.name = record.name }
        if !keeping.contains("color") {
            folder.color = FolderColor.matching(rawValue: record.color) ?? .blue
        }
        if !keeping.contains("icon") { folder.icon = record.icon.flatMap(FolderIcon.init(wire:)) }
        if !keeping.contains("favorite") { folder.favorite = record.favorite }
        if let created = record.created.flatMap(PaperDate.parse) { folder.created = created }
        if keeping.isEmpty, let modified = record.modified.flatMap(PaperDate.parse) {
            folder.modified = modified
        }
        folder.seq = max(folder.seq, record.seq)
        guard folder != before else { return false }
        try writeFolderRecord(folder)
        return folder.name != before.name || folder.color != before.color
            || folder.icon != before.icon || folder.favorite != before.favorite
    }

    /// A folder the server has and this device does not: its directory at
    /// `localPath`, with the server's record, journalling nothing.
    func createRemoteFolder(_ record: FolderRecord, at localPath: String) throws {
        writeLock.lock()
        defer { writeLock.unlock() }
        guard FolderPath.isValidFolder(localPath), localPath != FolderPath.root else { return }
        try ensureFolderRecords(forAncestorsOf: localPath)
        try FileManager.default.createDirectory(
            at: paths.url(forVaultPath: localPath), withIntermediateDirectories: true)
        let now = Date()
        let created = record.created.flatMap(PaperDate.parse) ?? now
        try writeFolderRecord(
            PaperFolder(
                id: record.id, name: record.name.isEmpty ? FolderPath.leaf(of: localPath) : record.name,
                color: FolderColor.matching(rawValue: record.color) ?? .blue,
                icon: record.icon.flatMap(FolderIcon.init(wire:)), favorite: record.favorite,
                created: created, modified: record.modified.flatMap(PaperDate.parse) ?? created,
                seq: record.seq, path: localPath))
    }

    /// Gives a folder the id the server knows it by: in `folder.json`, in its
    /// row, and on its queued ops.
    func adoptFolderID(_ oldID: String, as newID: String) throws {
        writeLock.lock()
        defer { writeLock.unlock() }
        guard oldID != newID, var folder = try folderIfPresent(id: oldID) else { return }
        try database.queue.write { db in
            try db.execute(sql: "DELETE FROM folders WHERE id = ?", arguments: [newID])
            try db.execute(sql: "UPDATE folders SET id = ? WHERE id = ?", arguments: [newID, oldID])
        }
        folder.id = newID
        try writeFolderRecord(folder)
        try journal.adoptFolderID(oldID, as: newID)
    }

    /// Renames a folder this device made, whose create has not gone up, so
    /// the create cannot be taken for another folder of the same name.
    func retitleLocalFolder(id: String, to name: String) throws {
        writeLock.lock()
        defer { writeLock.unlock() }
        guard var folder = try folderIfPresent(id: id), folder.name != name else { return }
        folder.name = name
        folder.modified = Date()
        try writeFolderRecord(folder)
    }

    /// Another device deleted the folder. It goes to the device trash, rows
    /// and own queued ops with it, unless a notebook package survives
    /// anywhere inside it -- one with work this device has not sent, which a
    /// remote delete never takes. Then the directory stays, under the record
    /// the server will make again for it once that work arrives: the legacy
    /// one of its path. Returns the paths to announce.
    func applyRemoteFolderDelete(_ folder: PaperFolder) throws -> Set<String> {
        writeLock.lock()
        defer { writeLock.unlock() }
        let manager = FileManager.default
        let source = paths.url(forVaultPath: folder.path)
        guard manager.fileExists(atPath: source.path(percentEncoded: false)) else {
            try removeRows(under: folder.path)
            return [folder.path]
        }
        var packages: [String] = []
        collectPackages(in: source, prefix: folder.path.split(separator: "/").map(String.init),
            into: &packages)
        let inside = try database.queue.read { db in
            try String.fetchAll(
                db, sql: "SELECT id FROM folders WHERE \(Self.under("path"))",
                arguments: ["p": folder.path])
        }
        guard packages.isEmpty else {
            let legacy = PaperFolder.legacy(path: folder.path)
            if legacy.id != folder.id {
                try database.queue.write { db in
                    try db.execute(sql: "DELETE FROM folders WHERE id = ?", arguments: [folder.id])
                }
            }
            try writeFolderRecord(legacy)
            try journal.dropFolderOps(folderID: folder.id)
            return [folder.path]
        }
        let token = TrashToken(id: PaperID.make(), kind: .folder, title: folder.name)
        let entry = paths.trash.appending(path: token.id, directoryHint: .isDirectory)
        try manager.createDirectory(at: entry, withIntermediateDirectories: true)
        try AtomicFile.writeJSON(
            TrashManifest(
                kind: .folder, notebookPath: folder.path, notebookID: folder.id, pageID: nil,
                position: nil, deletedAt: Date()),
            to: entry.appending(path: "manifest.json", directoryHint: .notDirectory))
        try manager.moveItem(at: source, to: entry.appending(path: "folder", directoryHint: .isDirectory))
        try removeRows(under: folder.path)
        for id in Set(inside + [folder.id]) { try journal.dropFolderOps(folderID: id) }
        return [folder.path]
    }

    /// Every folder above `path` that has no record gets the legacy one, its
    /// directory made if it is missing. Journals nothing: the server makes
    /// the same records itself when a write creates a directory.
    func ensureFolderRecords(forAncestorsOf path: String) throws {
        writeLock.lock()
        defer { writeLock.unlock() }
        var chain: [String] = []
        var current = FolderPath.parent(of: path)
        while current != FolderPath.root, current.hasPrefix(FolderPath.root + "/") {
            chain.append(current)
            current = FolderPath.parent(of: current)
        }
        for folderPath in chain.reversed() where FolderPath.isValidFolder(folderPath) {
            try FileManager.default.createDirectory(
                at: paths.url(forVaultPath: folderPath), withIntermediateDirectories: true)
            guard try folder(path: folderPath) == nil else { continue }
            var seen = Set(try database.queue.read { db in
                try String.fetchAll(db, sql: "SELECT id FROM folders")
            })
            _ = try indexFolder(at: folderPath, seen: &seen)
        }
    }

    /// `ref` where its notebook is now. A folder moved by another device takes
    /// the directory of an open notebook along; the notebook's id still finds
    /// it.
    public func located(_ ref: PageRef) -> PageRef {
        guard let path = try? notebook(id: ref.notebookID).path, path != ref.notebookPath else {
            return ref
        }
        return PageRef(notebookID: ref.notebookID, notebookPath: path, pageID: ref.pageID)
    }

    /// Where the notebook holding `pageID` is now, when that is not
    /// `notebookPath` any more -- a folder moved by another device, or a
    /// rename -- found by the page's id; nil when the page is nowhere. A
    /// render or a reading that finishes late writes there, never at a path
    /// that has gone, where it would make the old directories again.
    func currentNotebookPath(ofPage pageID: String, near notebookPath: String) -> String? {
        let manager = FileManager.default
        func holds(_ path: String) -> Bool {
            manager.fileExists(atPath: paths.pageJSON(path, pageID).path(percentEncoded: false))
        }
        if holds(notebookPath) { return notebookPath }
        let found = try? database.queue.read { db in
            try String.fetchOne(
                db,
                sql: "SELECT n.path FROM pages p JOIN notebooks n ON p.notebook_id = n.id WHERE p.id = ?",
                arguments: [pageID])
        }
        guard let found, holds(found) else { return nil }
        return found
    }

    /// Writes a page's render, and a board's `render_rect`, where the page is
    /// now, under the lock a folder move takes, and returns that path. Nil,
    /// writing nothing, when the page is nowhere, or when it is no longer
    /// what was drawn: a sheet that became a board meanwhile (a conversion)
    /// must not get the sheet's picture.
    func storeRender(
        _ png: Data, notebookPath: String, pageID: String, isBoard: Bool,
        area: PageObject.Frame?
    ) throws -> String? {
        writeLock.lock()
        defer { writeLock.unlock() }
        guard let path = currentNotebookPath(ofPage: pageID, near: notebookPath),
            let page = try? page(notebookPath: path, id: pageID), (page.size == .board) == isBoard
        else { return nil }
        try AtomicFile.write(png, to: paths.render(path, pageID))
        if isBoard { _ = try? setRenderRect(area, notebookPath: path, pageID: pageID) }
        return path
    }

    // MARK: Reconciler

    /// The folder half of `reconcile()`. Visits every directory under
    /// `paper/` that is not a package:
    ///
    /// - a missing `folder.json` gets the legacy record of its path;
    /// - an unreadable one is set aside as `folder.json.corrupt-<stamp>` and
    ///   replaced by the legacy record;
    /// - an id an earlier path already holds becomes the legacy id of this
    ///   path;
    /// - rows are upserted, and rows whose directory has gone are deleted.
    ///
    /// Journals nothing: the server derives the same legacy records.
    func reconcileFolders() throws {
        let manager = FileManager.default
        recoverInterruptedRenames(in: paths.paper, prefix: [FolderPath.root])
        var found: [String] = []
        collectFolders(in: paths.paper, prefix: [FolderPath.root], into: &found)
        var seen: Set<String> = []
        for path in found.sorted() {
            writeLock.lock()
            defer { writeLock.unlock() }
            // Moved by a pull since the walk listed it: writing its record
            // here would make the old path again.
            guard manager.fileExists(
                atPath: paths.url(forVaultPath: path).path(percentEncoded: false))
            else { continue }
            _ = try? indexFolder(at: path, seen: &seen)
        }
        let known = Set(found)
        let stale = try database.queue.read { db in
            try String.fetchAll(db, sql: "SELECT path FROM folders")
        }.filter {
            !known.contains($0)
                && !manager.fileExists(atPath: paths.url(forVaultPath: $0).path(percentEncoded: false))
        }
        guard !stale.isEmpty else { return }
        try database.queue.write { db in
            for path in stale {
                try db.execute(sql: "DELETE FROM folders WHERE path = ?", arguments: [path])
            }
        }
    }

    // MARK: - Internals

    /// The folder's `folder.json`, read and healed, and its row. Callers
    /// hold `writeLock`.
    private func indexFolder(at path: String, seen: inout Set<String>) throws -> PaperFolder {
        let url = folderJSONURL(path)
        var folder: PaperFolder
        var dirty = false
        if let data = try? Data(contentsOf: url) {
            if let decoded = Self.readFolderRecord(data) {
                folder = decoded
                if !PaperFolderID.isValid(folder.id) {
                    folder.id = PaperFolderID.legacy(path: path)
                    dirty = true
                }
                if folder.name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    folder.name = FolderPath.leaf(of: path)
                    dirty = true
                }
            } else {
                try FileManager.default.moveItem(
                    at: url,
                    to: url.deletingLastPathComponent().appending(
                        path: "folder.json.corrupt-\(Self.corruptStamp())",
                        directoryHint: .notDirectory))
                folder = .legacy(path: path)
                dirty = true
            }
        } else {
            folder = .legacy(path: path)
            dirty = true
        }
        folder.path = path
        if seen.contains(folder.id) {
            // A copy of a folder an earlier path holds the id of.
            folder.id = PaperFolderID.legacy(path: path)
            if seen.contains(folder.id) { folder.id = PaperFolderID.make() }
            dirty = true
        }
        seen.insert(folder.id)
        if dirty {
            try writeFolderRecord(folder)
        } else {
            let snapshot = folder
            try database.queue.write { db in try Self.upsertFolderRow(snapshot, in: db) }
        }
        return folder
    }

    /// `folder.json` as written, or as a record once a missing or malformed
    /// id is given the legacy one; nil when it is not a JSON object at all.
    private static func readFolderRecord(_ data: Data) -> PaperFolder? {
        if let folder = try? PaperJSON.decode(PaperFolder.self, from: data) { return folder }
        guard var object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            return nil
        }
        object["id"] = ""
        guard let patched = try? JSONSerialization.data(withJSONObject: object) else { return nil }
        return try? PaperJSON.decode(PaperFolder.self, from: patched)
    }

    private static func corruptStamp() -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "UTC")
        formatter.dateFormat = "yyyyMMddHHmmss"
        return formatter.string(from: Date())
    }

    /// Records and rows for every folder at or under `path`, and the index
    /// for every notebook in them: a folder that came back from the trash.
    private func indexFolderTree(at path: String) throws {
        var folders: [String] = [path]
        collectFolders(
            in: paths.url(forVaultPath: path), prefix: path.split(separator: "/").map(String.init),
            into: &folders)
        var seen = Set(try database.queue.read { db in
            try String.fetchAll(db, sql: "SELECT id FROM folders")
        })
        for folderPath in folders.sorted() { _ = try indexFolder(at: folderPath, seen: &seen) }
        var packages: [String] = []
        collectPackages(
            in: paths.url(forVaultPath: path), prefix: path.split(separator: "/").map(String.init),
            into: &packages)
        for package in packages.sorted() { try indexNotebook(at: package) }
    }

    /// Every directory under `directory` that is not a package, by name: the
    /// container lives under a symlinked `/var`.
    private func collectFolders(in directory: URL, prefix: [String], into result: inout [String]) {
        let children =
            (try? FileManager.default.contentsOfDirectory(
                at: directory, includingPropertiesForKeys: [.isDirectoryKey],
                options: [.skipsHiddenFiles])) ?? []
        for child in children where child.hasDirectoryPath {
            let name = child.lastPathComponent
            guard !name.hasSuffix(".paper") else { continue }
            let path = (prefix + [name]).joined(separator: "/")
            result.append(path)
            collectFolders(in: child, prefix: prefix + [name], into: &result)
        }
    }

    /// Every notebook package under `directory`.
    private func collectPackages(in directory: URL, prefix: [String], into result: inout [String]) {
        let children =
            (try? FileManager.default.contentsOfDirectory(
                at: directory, includingPropertiesForKeys: [.isDirectoryKey],
                options: [.skipsHiddenFiles])) ?? []
        for child in children where child.hasDirectoryPath {
            let name = child.lastPathComponent
            let path = (prefix + [name]).joined(separator: "/")
            if name.hasSuffix(".paper") {
                if PaperPaths.isSafeNotebookPath(path) { result.append(path) }
            } else {
                collectPackages(in: child, prefix: prefix + [name], into: &result)
            }
        }
    }

    /// A `.rename-<id>` left by a case-only rename that never finished goes
    /// back to the name its record carries, or beside it when that is taken.
    private func recoverInterruptedRenames(in directory: URL, prefix: [String]) {
        let manager = FileManager.default
        let children =
            (try? manager.contentsOfDirectory(
                at: directory, includingPropertiesForKeys: [.isDirectoryKey])) ?? []
        for child in children where child.hasDirectoryPath {
            let name = child.lastPathComponent
            if name.hasPrefix(".rename-") {
                let parent = prefix.joined(separator: "/")
                let record = (try? Data(contentsOf: child.appending(path: "folder.json")))
                    .flatMap(Self.readFolderRecord)
                let base = PaperFolderID.folderSlug(record?.name ?? "Untitled")
                let leaf = freeEntryName(in: parent, base: base, excluding: nil)
                try? manager.moveItem(at: child, to: directory.appending(path: leaf))
                continue
            }
            guard !name.hasPrefix("."), !name.hasSuffix(".paper") else { continue }
            recoverInterruptedRenames(in: child, prefix: prefix + [name])
        }
    }

    /// The names already used in the folder at `parent`, folded: folders by
    /// directory name, notebooks without `.paper`.
    func takenEntryNames(in parent: String, excluding: String? = nil) -> Set<String> {
        let children =
            (try? FileManager.default.contentsOfDirectory(
                at: paths.url(forVaultPath: parent), includingPropertiesForKeys: [.isDirectoryKey],
                options: [.skipsHiddenFiles])) ?? []
        var taken: Set<String> = []
        for child in children where child.hasDirectoryPath {
            var name = child.lastPathComponent
            if name == excluding { continue }
            if name.hasSuffix(".paper") { name = String(name.dropLast(".paper".count)) }
            taken.insert(FolderPath.fold(name))
        }
        return taken
    }

    /// `base`, or "base 2", "base 3" … -- the first name nothing in `parent`
    /// holds.
    func freeEntryName(in parent: String, base: String, excluding: String?) -> String {
        let taken = takenEntryNames(in: parent, excluding: excluding)
        guard taken.contains(FolderPath.fold(base)) else { return base }
        var number = 2
        while taken.contains(FolderPath.fold("\(base) \(number)")) { number += 1 }
        return "\(base) \(number)"
    }

    /// Queues a rename or move -- unless the server has never heard of the
    /// folder. Then every op its create owns is re-pointed at the new path,
    /// and the create, which sends the name the folder has when it goes,
    /// lands there.
    private func journalFolderMove(id: String, from oldPath: String, to newPath: String, name: String?)
        throws
    {
        if let create = try journal.unpushedFolderCreate(folderID: id) {
            try journal.rewritePrefix(from: oldPath, to: newPath, sinceOpID: create)
            return
        }
        var payload = ["new_path": newPath]
        if let name { payload["name"] = name }
        try journal.append(
            .init(kind: .folderRename, notebookPath: oldPath, notebookID: id, payload: payload))
    }

    private func journalCreate(of folder: PaperFolder) throws {
        try journal.append(
            .init(
                kind: .folderCreate, notebookPath: folder.path, notebookID: folder.id,
                payload: [
                    "name": folder.name, "color": folder.color.rawValue,
                    "icon": folder.icon?.wire ?? "", "favorite": folder.favorite ? "true" : "false",
                ]))
    }

    private func patchFolder(_ folder: PaperFolder, _ payload: [String: String]) throws
        -> PaperFolder
    {
        guard !payload.isEmpty else { return folder }
        var folder = folder
        folder.modified = Date()
        try writeFolderRecord(folder)
        try journal.append(
            .init(
                kind: .folderPatch, notebookPath: folder.path, notebookID: folder.id,
                payload: payload))
        return folder
    }

    /// Moves the directory and every row under it. A move that only changes
    /// case goes through a temporary sibling, which a case-insensitive disk
    /// needs. Callers hold `writeLock`.
    private func moveFolderTree(from oldPath: String, to newPath: String, id: String?) throws {
        let manager = FileManager.default
        let source = paths.url(forVaultPath: oldPath)
        let destination = paths.url(forVaultPath: newPath)
        try ensureFolderRecords(forAncestorsOf: newPath)
        let caseOnly =
            FolderPath.parent(of: oldPath) == FolderPath.parent(of: newPath)
            && FolderPath.fold(FolderPath.leaf(of: oldPath)) == FolderPath.fold(FolderPath.leaf(of: newPath))
        if caseOnly {
            let temporary = source.deletingLastPathComponent().appending(
                path: ".rename-\(id ?? PaperID.make())", directoryHint: .isDirectory)
            try manager.moveItem(at: source, to: temporary)
            try manager.moveItem(at: temporary, to: destination)
        } else {
            try manager.moveItem(at: source, to: destination)
        }
        try relocateRows(from: oldPath, to: newPath)
    }

    /// Rewrites, by prefix, every row that names a path under `oldPath`:
    /// notebooks (path and folder), folders (path and parent), transcripts,
    /// and the ETags of the pages that moved, so nothing is fetched again.
    private func relocateRows(from oldPath: String, to newPath: String) throws {
        try database.queue.write { db in
            let arguments: StatementArguments = ["p": newPath]
            // Rows a stale index still holds where the tree is about to go.
            try db.execute(sql: "DELETE FROM notebooks WHERE \(Self.under("path"))", arguments: arguments)
            try db.execute(sql: "DELETE FROM folders WHERE \(Self.under("path"))", arguments: arguments)

            let old: StatementArguments = ["p": oldPath]
            for row in try Row.fetchAll(
                db, sql: "SELECT id, path FROM notebooks WHERE \(Self.under("path"))", arguments: old)
            {
                let path = FolderPath.replacingPrefix(of: row["path"], from: oldPath, to: newPath)
                try db.execute(
                    sql: "UPDATE notebooks SET path = ?, folder = ? WHERE id = ?",
                    arguments: [path, PaperPaths.folder(ofNotebookPath: path), row["id"] as String])
            }
            for row in try Row.fetchAll(
                db, sql: "SELECT id, path FROM folders WHERE \(Self.under("path"))", arguments: old)
            {
                let path = FolderPath.replacingPrefix(of: row["path"], from: oldPath, to: newPath)
                try db.execute(
                    sql: "UPDATE folders SET path = ?, parent = ? WHERE id = ?",
                    arguments: [path, FolderPath.parent(of: path), row["id"] as String])
            }
            try db.execute(
                sql: """
                    UPDATE page_text SET notebook_path = :to || substr(notebook_path, length(:p) + 1)
                    WHERE \(Self.under("notebook_path"))
                    """,
                arguments: ["p": oldPath, "to": newPath])
            try db.execute(
                sql: """
                    UPDATE OR REPLACE sync_state SET key = 'etag.' || :to || substr(key, length(:p) + 6)
                    WHERE substr(key, 1, length(:p) + 6) = 'etag.' || :p || '/'
                    """,
                arguments: ["p": oldPath, "to": newPath])
        }
    }

    /// Drops every row at or under `path`: the folders, the notebooks and
    /// their pages, and the transcripts of those pages.
    private func removeRows(under path: String) throws {
        try database.queue.write { db in
            let arguments: StatementArguments = ["p": path]
            let pages = """
                SELECT id FROM pages WHERE notebook_id IN
                    (SELECT id FROM notebooks WHERE \(Self.under("path")))
                """
            try db.execute(sql: "DELETE FROM page_fts WHERE page_id IN (\(pages))", arguments: arguments)
            try db.execute(sql: "DELETE FROM page_text WHERE page_id IN (\(pages))", arguments: arguments)
            try db.execute(sql: "DELETE FROM notebooks WHERE \(Self.under("path"))", arguments: arguments)
            try db.execute(sql: "DELETE FROM folders WHERE \(Self.under("path"))", arguments: arguments)
        }
    }

    /// Writes `folder.json`, keeping the keys this build does not know from
    /// the file it replaces, then the row.
    private func writeFolderRecord(_ folder: PaperFolder) throws {
        var folder = folder
        let url = folderJSONURL(folder.path)
        if let onDisk = (try? Data(contentsOf: url)).flatMap(Self.readFolderRecord) {
            folder.additionalFields.merge(onDisk.additionalFields) { mine, _ in mine }
        }
        try AtomicFile.writeJSON(folder, to: url)
        let snapshot = folder
        try database.queue.write { db in try Self.upsertFolderRow(snapshot, in: db) }
    }

    private func folderJSONURL(_ path: String) -> URL {
        paths.url(forVaultPath: path).appending(path: "folder.json", directoryHint: .notDirectory)
    }

    /// Whether everything inside the folder still fits the path limit once it
    /// is at `newPath`.
    private func fitsPathLimit(moving oldPath: String, to newPath: String) -> Bool {
        let inside = (try? database.queue.read { db in
            try String.fetchAll(
                db,
                sql: """
                    SELECT path FROM notebooks WHERE \(Self.under("path"))
                    UNION SELECT path FROM folders WHERE \(Self.under("path"))
                    """,
                arguments: ["p": oldPath])
        }) ?? []
        let deepest = inside.map { $0.utf8.count - oldPath.utf8.count }.max() ?? 0
        return newPath.utf8.count + max(0, deepest) <= FolderPath.maximumBytes
    }

    private static func fitsPathLimit(_ path: String) -> Bool {
        path.utf8.count <= FolderPath.maximumBytes
    }

    /// Verbatim and trimmed, at most 200 scalars; empty becomes Untitled.
    static func folderName(_ name: String) -> String {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return "Untitled" }
        let scalars = trimmed.unicodeScalars
        guard scalars.count > 200 else { return trimmed }
        return String(String.UnicodeScalarView(scalars.prefix(200)))
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// `column` is the path bound to `:p` or inside it.
    static func under(_ column: String) -> String {
        "(\(column) = :p OR substr(\(column), 1, length(:p) + 1) = :p || '/')"
    }

    /// The dates are seconds since 2001, which is what a `Date` holds, so a
    /// row reads back exactly the record that was written. Seconds since
    /// 1970 -- what the other tables keep -- lose the last bit of about half
    /// of the dates `Date()` makes on the way through, and the record
    /// `createFolder` returned then differed from the one the store answered
    /// with for the same id.
    private static func folder(from row: Row) -> PaperFolder {
        PaperFolder(
            id: row["id"] ?? "",
            name: row["name"] ?? "",
            color: FolderColor.matching(rawValue: row["color"] ?? "") ?? .blue,
            icon: (row["icon"] as String?).flatMap(FolderIcon.init(wire:)),
            favorite: row["favorite"] ?? false,
            created: Date(timeIntervalSinceReferenceDate: row["created"] ?? 0),
            modified: Date(timeIntervalSinceReferenceDate: row["modified"] ?? 0),
            seq: row["seq"] ?? 0,
            path: row["path"] ?? "")
    }

    private static func upsertFolderRow(_ folder: PaperFolder, in db: Database) throws {
        // One folder per path: a row another id left here is stale.
        try db.execute(
            sql: "DELETE FROM folders WHERE path = ? AND id <> ?", arguments: [folder.path, folder.id])
        try db.execute(
            sql: """
                INSERT INTO folders
                    (id, path, parent, name, color, icon, favorite, created, modified, seq)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    path = excluded.path, parent = excluded.parent, name = excluded.name,
                    color = excluded.color, icon = excluded.icon, favorite = excluded.favorite,
                    created = excluded.created, modified = excluded.modified, seq = excluded.seq
                """,
            arguments: [
                folder.id, folder.path, folder.parentPath, folder.name, folder.color.rawValue,
                folder.icon?.wire, folder.favorite, folder.created.timeIntervalSinceReferenceDate,
                folder.modified.timeIntervalSinceReferenceDate, folder.seq,
            ])
    }
}
