import Foundation

/// A folder op met a 404 because another device had moved the folder: the
/// op is held for one pull, which says where the folder went.
struct FolderMovedUpstream: Error {}

/// Folders on the wire: the four folder ops going up, and the server's
/// `folder_moves`, `folders` and folder tombstones coming down.
///
/// The rule everything here keeps: a folder renamed or moved anywhere is one
/// directory rename on every other device. Nothing inside it is re-stamped
/// upstream, so nothing inside it is fetched again here; the ops queued for
/// what is inside are re-pointed by path prefix instead, at the place in the
/// queue where each of them will reach the server.
extension SyncEngine {

    // MARK: - Push

    /// The op's path as the queue holds it now: a rename while the request
    /// was out may have re-pointed it.
    func currentPath(of op: PaperOp) -> String {
        guard let id = op.id, let current = try? store.journal.op(id: id) else {
            return op.notebookPath
        }
        return current.notebookPath
    }

    /// Creates the folder where the queue says, with the name, colour, icon
    /// and favourite it has on this device now -- a rename or recolour made
    /// before it went up rewrote nothing but this folder.
    ///
    /// Answered with another folder's id, this one becomes that one: two
    /// devices that each made "Work" offline end with one folder. Answered
    /// at another path, this device follows.
    func pushFolderCreate(_ op: PaperOp) async throws {
        let path = currentPath(of: op)
        let local = try? store.folderIfPresent(id: op.notebookID)
        let icon: String?
        if let local {
            icon = local.icon?.wire
        } else {
            icon = op.payload["icon"].flatMap { $0.isEmpty ? nil : $0 }
        }
        let result = try await api.createFolder(
            parentPath: FolderPath.parent(of: path),
            name: local?.name ?? op.payload["name"] ?? FolderPath.leaf(of: path),
            id: op.notebookID,
            color: local?.color.rawValue ?? op.payload["color"] ?? FolderColor.blue.rawValue,
            icon: icon,
            favorite: local?.favorite ?? (op.payload["favorite"] == "true"),
            clientID: clientID)
        let record = result.folder
        var id = op.notebookID
        if record.id != id, (try? store.folderIfPresent(id: record.id)) == nil {
            try store.adoptFolderID(id, as: record.id)
            id = record.id
        }
        // Where the server has it, when that is not where this device asked
        // -- unless this device already knows that folder elsewhere and kept
        // its own id: a later record of it sorts the two out.
        if record.path != path, record.id == id {
            try followServerPath(of: op, from: path, to: record.path, folderID: id)
        }
        // Another folder's record is that folder's, all of it; this folder's
        // own brings back only what the server stamps.
        let queue = (try? store.journal.queue()) ?? []
        try store.updateFolder(
            id: id, from: record,
            keeping: result.existing
                ? pendingFields(of: id, in: queue.filter { $0.id != op.id })
                : ["name", "color", "icon", "favorite"])
    }

    /// A rename or a move: one request, answered with where the folder went.
    /// A sibling upstream with the name makes it "<name> 2" there, and this
    /// device follows. A 404 is done if the folder already sits at
    /// `new_path`, held for a pull if another device moved it, and parked if
    /// it is nowhere.
    func pushFolderRename(_ op: PaperOp) async throws {
        guard let newPath = op.payload["new_path"], !newPath.isEmpty else { return }
        let result: FolderRenameResult
        do {
            result = try await api.renameFolder(
                path: op.notebookPath, to: newPath, name: op.payload["name"], clientID: clientID)
        } catch let error as PaperAPIError where error.status == 404 {
            let upstream = try await api.folders().first { $0.id == op.notebookID }
            if upstream?.path == newPath {
                retargetDeferred(from: op.notebookPath, to: newPath)
                return
            }
            if upstream != nil { throw FolderMovedUpstream() }
            throw error
        }
        retargetDeferred(from: op.notebookPath, to: result.path)
        if result.path != newPath {
            try followServerPath(of: op, from: newPath, to: result.path, folderID: op.notebookID)
        }
    }

    /// Colour, icon and favourite. A folder that is gone upstream has
    /// nothing left to patch; one another device moved is held for a pull.
    func pushFolderPatch(_ op: PaperOp) async throws {
        var patch: [String: JSONValue] = [:]
        for (key, value) in op.payload {
            switch key {
            case "color", "icon": patch[key] = .string(value)
            case "favorite": patch[key] = .bool(value == "true")
            default: break
            }
        }
        guard !patch.isEmpty else { return }
        do {
            _ = try await api.patchFolder(path: op.notebookPath, patch: patch, clientID: clientID)
        } catch let error as PaperAPIError where error.status == 404 {
            try await throwIfMovedUpstream(op)
        }
    }

    /// A folder the server no longer has is a delete that already happened
    /// -- unless another device moved it, when it is still there, elsewhere,
    /// and the delete is held for a pull that says where.
    func pushFolderDelete(_ op: PaperOp) async throws {
        guard !(try await api.deleteFolderIfPresent(path: op.notebookPath)) else { return }
        try await throwIfMovedUpstream(op)
    }

    private func throwIfMovedUpstream(_ op: PaperOp) async throws {
        if let upstream = try await api.folders().first(where: { $0.id == op.notebookID }),
            upstream.path != op.notebookPath
        {
            throw FolderMovedUpstream()
        }
    }

    /// The server put what `op` made or moved at `serverPath` rather than at
    /// `path`. Every op behind it that names `path` is re-pointed, at its
    /// place in the queue, and this device's copy moves to match.
    func followServerPath(of op: PaperOp, from path: String, to serverPath: String, folderID: String? = nil)
        throws
    {
        try store.journal.followFolderMove(from: path, to: serverPath)
        let current: String?
        switch op.kind {
        case .notebookCreate, .notebookRename, .notebookMove:
            current = localNotebook(for: op)?.path
        default:
            current = try store.folderIfPresent(id: folderID ?? op.notebookID)?.path
        }
        guard let current else { return }
        let target = QueuedFolderChanges(try store.journal.queue()).local(serverPath)
        guard target != current else { return }
        // The queue already names the server's path, and the pusher finds
        // files by id. Something in the way here is left for the next pull's
        // records to sort out rather than failing the push that landed.
        guard let changed = try? store.relocateTree(from: current, to: target) else { return }
        if !changed.isEmpty { onNotebooksChanged?(changed) }
    }

    // MARK: - Pull

    /// Step 1: `folder_moves`, by seq. Returns what to announce, and false
    /// when a move could not be applied and the cursor has to stay.
    func applyFolderMoves(_ moves: [FolderMove]) -> (Set<String>, Bool) {
        var announced: Set<String> = []
        var complete = true
        for move in moves.sorted(by: { $0.seq < $1.seq }) {
            do {
                announced.formUnion(try followRemoteFolder(id: move.id, from: move.from, to: move.to))
            } catch {
                recordFailure("apply folder move failed", notebook: move.to, error: error)
                complete = false
            }
        }
        return (announced, complete)
    }

    /// Step 2: `folders` -- and `folder_records` on a full reconcile, which
    /// stays additive: a folder here the server does not list is left alone.
    func applyFolderRecords(_ records: [FolderRecord]) -> (Set<String>, Bool) {
        var announced: Set<String> = []
        var complete = true
        // Parents first: a new folder's directory goes inside its parent's.
        for record in records.sorted(by: { $0.path < $1.path }) {
            do {
                announced.formUnion(try applyFolderRecord(record))
            } catch {
                recordFailure("apply folder failed", notebook: record.path, error: error)
                complete = false
            }
        }
        return (announced, complete)
    }

    /// One record:
    ///
    /// - the same id here: moved to where the record says when a move was
    ///   missed (not over a rename of its own still queued: the later push
    ///   wins), and its fields taken except those changed here and not sent;
    /// - another id at that path, with no create or rename of its own queued:
    ///   it is this folder, and takes the server's id;
    /// - nothing here: the folder is made, empty or not.
    func applyFolderRecord(_ record: FolderRecord) throws -> Set<String> {
        guard PaperFolderID.isValid(record.id), record.path != FolderPath.root,
            FolderPath.isValidFolder(record.path)
        else { return [] }
        let queue = try store.journal.queue()
        let folders = QueuedFolderChanges(queue)
        guard !folders.isDeleted(record.path) else { return [] }

        if try store.folderIfPresent(id: record.id) != nil {
            var announced = try followRemoteFolder(id: record.id, from: nil, to: record.path)
            let changed = try store.updateFolder(
                id: record.id, from: record,
                keeping: pendingFields(of: record.id, in: try store.journal.queue()))
            if changed, let path = try store.folderIfPresent(id: record.id)?.path {
                announced.insert(path)
            }
            return announced
        }
        let wanted = folders.local(record.path)
        if let other = try store.folder(path: wanted) {
            let busy = queue.contains {
                $0.notebookID == other.id && ($0.kind == .folderCreate || $0.kind == .folderRename)
            }
            guard !busy else { return [] }
            try store.adoptFolderID(other.id, as: record.id)
            try store.updateFolder(
                id: record.id, from: record,
                keeping: pendingFields(of: record.id, in: try store.journal.queue()))
            return [wanted]
        }
        try store.createRemoteFolder(record, at: wanted)
        return [wanted]
    }

    /// Step 4, for a `folder` row: by id, since a path can name a folder made
    /// since at the same place. A folder this device made and has not sent
    /// is its own and stays.
    func applyFolderDeletion(_ deletion: ChangesFeed.Deletion) -> Set<String> {
        do {
            let folder: PaperFolder?
            if let id = deletion.id {
                folder = try store.folderIfPresent(id: id)
            } else {
                let folders = QueuedFolderChanges(try store.journal.queue())
                folder = try store.folder(path: folders.local(deletion.path))
            }
            guard let folder, try store.journal.unpushedFolderCreate(folderID: folder.id) == nil
            else { return [] }
            return try store.applyRemoteFolderDelete(folder)
        } catch {
            recordFailure("apply folder delete failed", notebook: deletion.path, error: error)
            return []
        }
    }

    /// The server has folder `id` at `to`, having moved it from `from` (a
    /// feed move) or from wherever this device last knew (a record).
    ///
    /// Nothing happens when that is where this device expects it already --
    /// the echo of its own rename -- or when a feed move starts somewhere
    /// this device has gone past since. Otherwise:
    ///
    /// 1. Unless a rename of its own is still queued here, the directory is
    ///    renamed to where the folder now is, after the renames still queued
    ///    here. Something else there, a folder this device made most often,
    ///    first steps aside to a free " N".
    /// 2. Every queued op that names the folder where it was is re-pointed,
    ///    at its place in the queue, and so is every deferred notebook.
    ///
    /// No page is fetched. Returns the paths that changed.
    func followRemoteFolder(id: String, from: String?, to: String) throws -> Set<String> {
        guard try store.journal.unpushedFolderCreate(folderID: id) == nil else { return [] }
        let queue = try store.journal.queue()
        let folders = QueuedFolderChanges(queue)
        let local = try store.folderIfPresent(id: id)
        guard let expected = expectedServerPath(of: id, local: local, queue: queue, folders: folders),
            expected != to
        else { return [] }
        if let from, expected != from { return [] }

        var announced: Set<String> = []
        let ownRename = queue.contains { $0.kind == .folderRename && $0.notebookID == id }
        if let local, !ownRename {
            let target = folders.local(to)
            if target != local.path {
                // A change of case is the folder itself, whatever a
                // case-insensitive disk says is at the target already.
                let caseOnly = FolderPath.fold(target) == FolderPath.fold(local.path)
                if !caseOnly, let occupant = try occupant(of: target), occupant.id != id {
                    announced.formUnion(try moveAside(occupant, folders: folders))
                }
                announced.formUnion(try store.relocateTree(from: local.path, to: target))
            }
        }
        if try store.journal.followFolderMove(from: expected, to: to) > 0 { queueRepointed = true }
        retargetDeferred(from: expected, to: to)
        return announced
    }

    /// Where the server has folder `id` now, as far as this device knows:
    /// the path its first queued op names, taken back through the folder
    /// renames queued ahead of that op; else its own path here, taken back
    /// through every folder rename queued.
    private func expectedServerPath(
        of id: String, local: PaperFolder?, queue: [PaperOp], folders: QueuedFolderChanges
    ) -> String? {
        if let index = queue.firstIndex(where: { $0.notebookID == id && $0.kind.isFolderOp }) {
            return QueuedFolderChanges(queue[..<index]).server(queue[index].notebookPath)
        }
        return local.map { folders.server($0.path) }
    }

    /// The folder at `path` here, indexing a directory the reconciler has not
    /// met yet.
    private func occupant(of path: String) throws -> PaperFolder? {
        if let folder = try store.folder(path: path) { return folder }
        // By name as the disk spells it: a case-insensitive disk finds
        // "Work" when asked for "WORK".
        let names =
            (try? FileManager.default.contentsOfDirectory(
                atPath: store.paths.url(forVaultPath: FolderPath.parent(of: path))
                    .path(percentEncoded: false))) ?? []
        guard names.contains(FolderPath.leaf(of: path)) else { return nil }
        try store.ensureFolderRecords(forAncestorsOf: FolderPath.join(path, "_"))
        return try store.folder(path: path)
    }

    /// Moves `occupant` out of the way to a free " N" beside it, with its
    /// queued ops. A folder this device made and has not sent takes that
    /// name too: its create sends the name it has when it goes, and the name
    /// it had would be taken for the folder that just arrived and merged
    /// into it.
    private func moveAside(_ occupant: PaperFolder, folders: QueuedFolderChanges) throws
        -> Set<String>
    {
        let parent = occupant.parentPath
        let leaf = store.freeEntryName(
            in: parent, base: FolderPath.leaf(of: occupant.path), excluding: nil)
        let aside = FolderPath.join(parent, leaf)
        let changed = try store.relocateTree(from: occupant.path, to: aside)
        try store.journal.followFolderMove(
            from: folders.server(occupant.path), to: folders.server(aside))
        if try store.journal.unpushedFolderCreate(folderID: occupant.id) != nil {
            try store.retitleLocalFolder(id: occupant.id, to: leaf)
        }
        return changed
    }

    /// The fields of folder `id` changed here and not yet sent: the server's
    /// copy of them is older than this device's.
    func pendingFields(of id: String, in queue: [PaperOp]) -> Set<String> {
        var fields: Set<String> = []
        for op in queue where op.notebookID == id {
            switch op.kind {
            case .folderCreate: fields.formUnion(["name", "color", "icon", "favorite"])
            case .folderRename: if op.payload["name"] != nil { fields.insert("name") }
            case .folderPatch: fields.formUnion(op.payload.keys)
            default: break
            }
        }
        return fields
    }

    /// Deferred notebooks are asked for by the server's path, which a folder
    /// move just changed.
    func retargetDeferred(from: String, to: String) {
        deferredNotebooks = Set(
            deferredNotebooks.map { FolderPath.movingPrefix($0, from: from, to: to) })
    }
}
