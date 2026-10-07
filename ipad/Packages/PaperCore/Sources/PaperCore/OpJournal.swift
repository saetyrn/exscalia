import Foundation
import GRDB

/// One thing that happened locally and has to reach the server eventually.
///
/// `Codable` because the device trash keeps the ops a delete dropped, so the
/// undo toast can queue the same work again.
public struct PaperOp: Equatable, Sendable, Codable {
    public enum Kind: String, Codable, CaseIterable, Sendable {
        case notebookCreate
        case notebookRename
        case notebookMove
        case notebookPatch
        case notebookDelete
        case pageCreate
        case pagePut
        /// Derived data only: the render for a page whose ink is already
        /// upstream. It carries no source part, so the server accepts it
        /// without bumping the revision.
        case pageRender
        /// Derived data only: the recognised transcript, for a page whose ink
        /// is already upstream. Same contract as `pageRender` -- no source
        /// part, no revision bump -- because a transcript is a reading of the
        /// ink and never an edit to it.
        case pageText
        case pageDelete
        case assetPut
        /// A folder, at the op's path, with `name`, `color`, `icon` (`""` for
        /// none) and `favorite` (`"true"`/`"false"`). Folder ops keep the
        /// folder's path in `notebookPath` and its id in `notebookID`.
        case folderCreate
        /// The folder at the op's path goes to `new_path`, retitled to `name`
        /// when one is given.
        case folderRename
        /// Any of `color`, `icon` (`""` clears it) and `favorite`.
        case folderPatch
        case folderDelete
        /// A row whose kind this build cannot read -- a newer build queued
        /// it. Never sent and never journalled: `pending()` passes it over and
        /// `parked()` names it, so it waits, visibly, for a build that knows
        /// what it is. Sending it as anything else would be guessing.
        case unknown

        /// A name this build does not know reads as `.unknown`, so a device
        /// trash manifest holding one still opens.
        public init(from decoder: Decoder) throws {
            let name = try decoder.singleValueContainer().decode(String.self)
            self = Kind(rawValue: name) ?? .unknown
        }

        /// The kinds this build can send, as stored.
        static let sendable: [String] = allCases.filter { $0 != .unknown }.map(\.rawValue)

        var isFolderOp: Bool {
            switch self {
            case .folderCreate, .folderRename, .folderPatch, .folderDelete: return true
            default: return false
            }
        }
    }

    /// Why an op of a kind this build cannot read is held.
    public static let unknownKindReason = "unknown operation"

    public var id: Int64?
    public var kind: Kind
    /// The path the *server* knows the notebook by when this op reaches it.
    /// A rename or move still queued behind the op has not moved the server's
    /// copy yet, so this is the URL; where the files are now goes by id.
    /// A folder op holds the folder's path here, by the same rule.
    public var notebookPath: String
    /// The notebook's id: how the pusher finds its files on this device after
    /// a rename or move has taken them somewhere else. Empty on rows queued
    /// before ops carried it, and on a producer that did not say, in which
    /// case the journal fills it from the index as the op is written. A
    /// folder op holds the folder's id.
    public var notebookID: String
    public var pageID: String
    /// Free-form JSON the M3 pusher turns into a request body.
    public var payload: [String: String]
    public var createdAt: Date
    public var pushed: Bool
    /// Why this op is not being retried. A 4xx that is not a conflict means the
    /// request itself is wrong, so re-sending it every minute would only bury
    /// the queue behind it; the op is kept, parked, and named in the pill.
    public var parkedReason: String?

    public init(
        id: Int64? = nil,
        kind: Kind,
        notebookPath: String,
        notebookID: String = "",
        pageID: String = "",
        payload: [String: String] = [:],
        createdAt: Date = Date(),
        pushed: Bool = false,
        parkedReason: String? = nil
    ) {
        self.id = id
        self.kind = kind
        self.notebookPath = notebookPath
        self.notebookID = notebookID
        self.pageID = pageID
        self.payload = payload
        self.createdAt = createdAt
        self.pushed = pushed
        self.parkedReason = parkedReason
    }

    /// Which page this op is about, when it is about one.
    public var pageKey: String? { pageID.isEmpty ? nil : "\(notebookPath)/pages/\(pageID)" }

    /// A `pagePut` that brings a page into existence on the server. A PUT at
    /// base revision 0 is a create, so the page arrives with its ink, objects
    /// and PDF binding in one request, placed after `after` (or at the end).
    public static func creatingPage(notebookPath: String, pageID: String, after: String?)
        -> PaperOp
    {
        var payload = ["create": "true"]
        if let after { payload["after"] = after }
        return PaperOp(kind: .pagePut, notebookPath: notebookPath, pageID: pageID, payload: payload)
    }

    public var createsPage: Bool { kind == .pagePut && payload["create"] == "true" }
}

/// FIFO queue of local changes, with the coalescing rules that keep a long
/// writing session from turning into ten thousand pushes.
///
/// M2 only fills it. Nothing drains it until the M3 sync engine, and that is
/// the seam: no code in this milestone touches the network.
public struct OpJournal: Sendable {
    private let database: PaperDatabase

    public init(database: PaperDatabase) {
        self.database = database
    }

    /// Appends `op`, applying the coalescing rules:
    ///
    /// - a `pagePut` replaces an unpushed `pagePut` for the same page (the
    ///   newer drawing supersedes the older one; the file on disk is the body),
    ///   except one that creates the page, which keeps its place: pages created
    ///   after it name it as `after`, and moving it behind them would land the
    ///   whole run out of order on the server;
    /// - a `notebookPatch` replaces the unpushed `notebookPatch`es for the same
    ///   notebook, taking their fields along: patches are partial (a favorite,
    ///   the tags, the page order), and one that replaced another dropped its
    ///   fields, which then never reached the server and the next pull put
    ///   back. Only that notebook's: another notebook can have one queued at
    ///   the same path from before its rename;
    /// - a `notebookDelete` drops every unpushed op for that notebook, because
    ///   pushing edits to something being deleted is noise -- that notebook's
    ///   own when the op names its id, not another's queued at the same path
    ///   before a rename took it elsewhere;
    /// - a `pageDelete` drops unpushed `pageCreate`/`pagePut` for that page,
    ///   and if the create was still unpushed the delete is dropped too: the
    ///   server never heard of the page.
    ///
    /// Folder ops, keyed by the folder's id:
    ///
    /// - a `folderPatch` replaces the unpushed `folderPatch`es for the same
    ///   folder, taking their fields along, as `notebookPatch` does;
    /// - a `folderDelete` drops the unpushed work it makes moot
    ///   (`unpushed(underFolder:)`), and is itself dropped when the folder's
    ///   own create was among it: the server never heard of the folder.
    ///
    /// An op of a kind this build cannot read is never written.
    @discardableResult
    public func append(_ op: PaperOp) throws -> Int64? {
        guard op.kind != .unknown else { return nil }
        return try database.queue.write { db in
            switch op.kind {
            case .notebookDelete:
                try db.execute(
                    sql: """
                        DELETE FROM ops
                        WHERE pushed = 0 AND notebook_path = ? AND \(Self.sameNotebook)
                        """,
                    arguments: [op.notebookPath, op.notebookID, op.notebookID])
            case .pagePut:
                // A newer drawing supersedes an older one, and it carries the
                // render and the transcript with it, so a queued derived-only
                // push is redundant. A queued create stays put and this op
                // queues behind it: the create may already be in flight with
                // the older files.
                try db.execute(
                    sql: """
                        DELETE FROM ops
                        WHERE pushed = 0 AND kind IN (?, ?, ?) AND notebook_path = ? AND page_id = ?
                          AND json_extract(payload, '$.create') IS NOT 'true'
                        """,
                    arguments: [
                        PaperOp.Kind.pagePut.rawValue, PaperOp.Kind.pageRender.rawValue,
                        PaperOp.Kind.pageText.rawValue, op.notebookPath, op.pageID,
                    ])
            case .pageRender, .pageText:
                // Nothing to do if the ink itself is still queued: that push
                // reads the render off the disk as it goes.
                let hasPendingPut =
                    try Int.fetchOne(
                        db,
                        sql: """
                            SELECT COUNT(*) FROM ops
                            WHERE pushed = 0 AND kind = ? AND notebook_path = ? AND page_id = ?
                            """,
                        arguments: [PaperOp.Kind.pagePut.rawValue, op.notebookPath, op.pageID])
                    ?? 0
                if hasPendingPut > 0 { return nil }
                try db.execute(
                    sql: """
                        DELETE FROM ops
                        WHERE pushed = 0 AND kind = ? AND notebook_path = ? AND page_id = ?
                        """,
                    arguments: [op.kind.rawValue, op.notebookPath, op.pageID])
            case .notebookPatch:
                let notebookID = try Self.notebookID(of: op, in: db)
                let queued = """
                    FROM ops
                    WHERE pushed = 0 AND kind = ? AND notebook_path = ? AND \(Self.sameNotebook)
                    """
                let arguments: StatementArguments = [
                    PaperOp.Kind.notebookPatch.rawValue, op.notebookPath, notebookID, notebookID,
                ]
                let older = try Row.fetchAll(
                    db, sql: "SELECT * \(queued) ORDER BY id", arguments: arguments
                ).map(Self.decode)
                guard !older.isEmpty else { break }
                // A parked patch is dropped as before, not folded in: the
                // server refused something in it, and it would park this one
                // along with it.
                var merged = op
                merged.payload = [:]
                for patch in older where patch.parkedReason == nil {
                    merged.payload.merge(patch.payload) { _, newer in newer }
                }
                merged.payload.merge(op.payload) { _, newer in newer }
                merged.notebookID = notebookID
                try db.execute(sql: "DELETE \(queued)", arguments: arguments)
                return try Self.insert(merged, in: db)
            case .pageDelete:
                let hadUnpushedCreate =
                    try Int.fetchOne(
                        db,
                        sql: """
                            SELECT COUNT(*) FROM ops
                            WHERE pushed = 0 AND notebook_path = ? AND page_id = ?
                              AND (kind = ? OR (kind = ? AND json_extract(payload, '$.create') = 'true'))
                            """,
                        arguments: [
                            op.notebookPath, op.pageID, PaperOp.Kind.pageCreate.rawValue,
                            PaperOp.Kind.pagePut.rawValue,
                        ])
                    ?? 0
                try db.execute(
                    sql: """
                        DELETE FROM ops
                        WHERE pushed = 0 AND notebook_path = ? AND page_id = ?
                          AND kind IN (?, ?, ?, ?)
                        """,
                    arguments: [
                        op.notebookPath, op.pageID,
                        PaperOp.Kind.pageCreate.rawValue, PaperOp.Kind.pagePut.rawValue,
                        PaperOp.Kind.pageRender.rawValue, PaperOp.Kind.pageText.rawValue,
                    ])
                if hadUnpushedCreate > 0 { return nil }
            case .folderPatch:
                let queued = "FROM ops WHERE pushed = 0 AND kind = ? AND notebook_id = ?"
                let arguments: StatementArguments = [
                    PaperOp.Kind.folderPatch.rawValue, op.notebookID,
                ]
                let older = try Row.fetchAll(
                    db, sql: "SELECT * \(queued) ORDER BY id", arguments: arguments
                ).map(Self.decode)
                guard !older.isEmpty else { break }
                var merged = op
                merged.payload = [:]
                for patch in older where patch.parkedReason == nil {
                    merged.payload.merge(patch.payload) { _, newer in newer }
                }
                merged.payload.merge(op.payload) { _, newer in newer }
                try db.execute(sql: "DELETE \(queued)", arguments: arguments)
                return try Self.insert(merged, in: db)
            case .folderDelete:
                let dropped = try Self.unpushed(underFolder: op.notebookPath, in: db)
                let hadUnpushedCreate = dropped.contains {
                    $0.kind == .folderCreate && $0.notebookID == op.notebookID
                }
                for id in dropped.compactMap(\.id) {
                    try db.execute(sql: "DELETE FROM ops WHERE id = ?", arguments: [id])
                }
                if hadUnpushedCreate { return nil }
            case .notebookCreate, .notebookRename, .notebookMove, .pageCreate, .assetPut,
                .folderCreate, .folderRename, .unknown:
                break
            }
            return try Self.insert(op, in: db)
        }
    }

    // MARK: - Folders

    /// The unpushed ops, parked ones included, that deleting the folder at
    /// `folderPath` makes moot, oldest first: what `append` drops for the
    /// `folderDelete`, and what the device trash keeps so the undo toast can
    /// queue the same work again. Read it before the folder leaves the index.
    ///
    /// - Ops at the folder's path or under it, except those of a notebook or
    ///   folder that lives somewhere else now: a rename or move queued before
    ///   the delete took it out, and its work still names where it was.
    /// - The edits of a notebook or folder inside it whose ops still name
    ///   where it was before it was moved in. The move itself stays: the
    ///   server has to take it in for its delete to take it away.
    public func unpushed(underFolder folderPath: String) throws -> [PaperOp] {
        try database.queue.read { db in try Self.unpushed(underFolder: folderPath, in: db) }
    }

    private static func unpushed(underFolder folderPath: String, in db: Database) throws
        -> [PaperOp]
    {
        let under = { (column: String) in
            "(\(column) = :p OR substr(\(column), 1, length(:p) + 1) = :p || '/')"
        }
        let inside = """
            (notebook_id <> '' AND (
                notebook_id IN (SELECT id FROM notebooks WHERE \(under("path")))
                OR notebook_id IN (SELECT id FROM folders WHERE \(under("path")))))
            """
        let elsewhere = """
            (notebook_id <> '' AND (
                notebook_id IN (SELECT id FROM notebooks WHERE NOT \(under("path")))
                OR notebook_id IN (SELECT id FROM folders WHERE NOT \(under("path")))))
            """
        let moves = [
            PaperOp.Kind.notebookRename, .notebookMove, .folderRename,
        ].map { "'\($0.rawValue)'" }.joined(separator: ", ")
        return try Row.fetchAll(
            db,
            sql: """
                SELECT * FROM ops
                WHERE pushed = 0 AND (
                    (\(under("notebook_path")) AND NOT \(elsewhere))
                    OR (\(inside) AND kind NOT IN (\(moves))))
                ORDER BY id
                """,
            arguments: ["p": folderPath]
        ).map(Self.decode)
    }

    /// The id of the unpushed `folderCreate` of this folder, or `nil` once
    /// the server has it. While there is one, the folder is this device's
    /// alone: a rename re-points its queue rather than journalling.
    public func unpushedFolderCreate(folderID: String) throws -> Int64? {
        try database.queue.read { db in
            try Int64.fetchOne(
                db,
                sql: """
                    SELECT id FROM ops WHERE pushed = 0 AND kind = ? AND notebook_id = ?
                    ORDER BY id LIMIT 1
                    """,
                arguments: [PaperOp.Kind.folderCreate.rawValue, folderID])
        }
    }

    /// The id of an unpushed `notebookCreate` of this notebook, parked or
    /// not; nil once the server has it.
    public func unpushedNotebookCreate(notebookID: String) throws -> Int64? {
        try database.queue.read { db in
            try Int64.fetchOne(
                db,
                sql: """
                    SELECT id FROM ops WHERE pushed = 0 AND kind = ? AND notebook_id = ?
                    ORDER BY id LIMIT 1
                    """,
                arguments: [PaperOp.Kind.notebookCreate.rawValue, notebookID])
        }
    }

    /// The op as it is in the queue now, which a rename while it was out
    /// may have re-pointed; nil once it is gone.
    func op(id: Int64) throws -> PaperOp? {
        try database.queue.read { db in
            try Row.fetchOne(db, sql: "SELECT * FROM ops WHERE id = ?", arguments: [id])
                .map(Self.decode)
        }
    }

    /// Every unpushed op, parked ones and unknown kinds included, oldest
    /// first: the queue as the server will meet it.
    func queue() throws -> [PaperOp] {
        try database.queue.read { db in
            try Row.fetchAll(db, sql: "SELECT * FROM ops WHERE pushed = 0 ORDER BY id")
                .map(Self.decode)
        }
    }

    /// Re-points every unpushed op at a folder that moved before the server
    /// heard of it: `notebook_path`, `payload.new_path` and `payload.path`,
    /// wherever they equal `oldPath` or start with `oldPath + "/"`.
    ///
    /// With `sinceOpID`, only ops from that one on: a folder this device
    /// created owns the ops queued from its create onward, and anything
    /// queued at its path before then names whatever held the path first.
    /// Returns how many ops changed.
    @discardableResult
    public func rewritePrefix(from oldPath: String, to newPath: String, sinceOpID: Int64? = nil)
        throws -> Int
    {
        guard oldPath != newPath else { return 0 }
        return try database.queue.write { db in
            var changed = 0
            for op in try Self.queue(in: db) {
                if let sinceOpID, let id = op.id, id < sinceOpID { continue }
                let moved = Self.repointed(op, from: oldPath, to: newPath)
                guard moved != op else { continue }
                try Self.update(moved, unparking: false, in: db)
                changed += 1
            }
            return changed
        }
    }

    /// The server moved the folder at `from` to `to` while ops were queued
    /// here. Every op that names the folder where it was -- at its place in
    /// the queue, after the renames queued ahead of it -- is re-pointed at
    /// where it is now; ops that name something else that took the path
    /// later are left alone.
    ///
    /// Walks the queue the way the server will meet it, keeping where the
    /// folder was assumed to be and where it actually is, and stops once
    /// they agree again: this device renamed the folder itself (the later
    /// push wins), or deleted it. A parked op that is re-pointed is a new
    /// request and is put back in the queue. Returns how many ops changed.
    @discardableResult
    public func followFolderMove(from: String, to: String) throws -> Int {
        guard from != to else { return 0 }
        return try database.queue.write { db in
            var assumed = from
            var actual = to
            var changed = 0
            for op in try Self.queue(in: db) {
                let moved = Self.repointed(op, from: assumed, to: actual)
                if moved != op {
                    try Self.update(moved, unparking: true, in: db)
                    changed += 1
                }
                switch op.kind {
                case .folderRename:
                    guard let target = op.payload["new_path"],
                        let movedTarget = moved.payload["new_path"]
                    else { continue }
                    if FolderPath.isUnder(assumed, op.notebookPath) {
                        assumed = FolderPath.replacingPrefix(
                            of: assumed, from: op.notebookPath, to: target)
                    }
                    if FolderPath.isUnder(actual, moved.notebookPath) {
                        actual = FolderPath.replacingPrefix(
                            of: actual, from: moved.notebookPath, to: movedTarget)
                    }
                case .folderDelete:
                    if FolderPath.isUnder(assumed, op.notebookPath) { return changed }
                default:
                    break
                }
                if assumed == actual { break }
            }
            return changed
        }
    }

    /// Gives a folder's queued ops the id the server knows it by.
    public func adoptFolderID(_ oldID: String, as newID: String) throws {
        guard oldID != newID, !oldID.isEmpty else { return }
        try database.queue.write { db in
            try db.execute(
                sql: "UPDATE ops SET notebook_id = ? WHERE pushed = 0 AND notebook_id = ? AND kind IN (?, ?, ?, ?)",
                arguments: [
                    newID, oldID, PaperOp.Kind.folderCreate.rawValue,
                    PaperOp.Kind.folderRename.rawValue, PaperOp.Kind.folderPatch.rawValue,
                    PaperOp.Kind.folderDelete.rawValue,
                ])
        }
    }

    /// Drops a folder's own unpushed ops -- its rename, patch or delete --
    /// once another device has deleted it and it is gone from here too.
    func dropFolderOps(folderID: String) throws {
        try database.queue.write { db in
            try db.execute(
                sql: "DELETE FROM ops WHERE pushed = 0 AND notebook_id = ? AND kind IN (?, ?, ?, ?)",
                arguments: [
                    folderID, PaperOp.Kind.folderCreate.rawValue,
                    PaperOp.Kind.folderRename.rawValue, PaperOp.Kind.folderPatch.rawValue,
                    PaperOp.Kind.folderDelete.rawValue,
                ])
        }
    }

    private static func queue(in db: Database) throws -> [PaperOp] {
        try Row.fetchAll(db, sql: "SELECT * FROM ops WHERE pushed = 0 ORDER BY id").map(decode)
    }

    /// `op` with its three path fields moved from `oldPath` to `newPath`
    /// wherever they are at or under it.
    private static func repointed(_ op: PaperOp, from oldPath: String, to newPath: String)
        -> PaperOp
    {
        var moved = op
        moved.notebookPath = FolderPath.movingPrefix(op.notebookPath, from: oldPath, to: newPath)
        for key in ["new_path", "path"] {
            guard let value = op.payload[key] else { continue }
            moved.payload[key] = FolderPath.movingPrefix(value, from: oldPath, to: newPath)
        }
        return moved
    }

    private static func update(_ op: PaperOp, unparking: Bool, in db: Database) throws {
        guard let id = op.id else { return }
        let payload = String(
            data: (try? JSONEncoder().encode(op.payload)) ?? Data("{}".utf8), encoding: .utf8)
            ?? "{}"
        try db.execute(
            sql: "UPDATE ops SET notebook_path = ?, payload = ? WHERE id = ?",
            arguments: [op.notebookPath, payload, id])
        if unparking {
            try db.execute(sql: "UPDATE ops SET parked_reason = NULL WHERE id = ?", arguments: [id])
        }
    }

    /// Cancels an unpushed op, which is how the 6-second undo toast works: if
    /// the delete never left the device there is nothing to undo upstream.
    /// Returns `true` when something was cancelled.
    @discardableResult
    public func cancelUnpushed(kind: PaperOp.Kind, notebookPath: String, pageID: String = "") throws
        -> Bool
    {
        try database.queue.write { db in
            let sql = """
                DELETE FROM ops
                WHERE pushed = 0 AND kind = ? AND notebook_path = ? AND page_id = ?
                """
            try db.execute(sql: sql, arguments: [kind.rawValue, notebookPath, pageID])
            return db.changesCount > 0
        }
    }

    /// Re-points every unpushed op for `oldPath` at `newPath`, in place, so
    /// the queue keeps its order -- but only while the notebook's own create
    /// is among them. The server has never heard of `oldPath` then, so the
    /// create can simply land at `newPath` and no rename is owed; the caller
    /// journals none. Returns `false`, touching nothing, once the create has
    /// gone up: the server's copy is at `oldPath` and only a rename moves it.
    ///
    /// With `notebookID`, only that notebook's ops count and move: another
    /// notebook can have ops queued at `oldPath` from before its own rename.
    @discardableResult
    public func rewritePath(from oldPath: String, to newPath: String, notebookID: String = "")
        throws -> Bool
    {
        try database.queue.write { db in
            let unpushedCreate =
                try Int.fetchOne(
                    db,
                    sql: """
                        SELECT COUNT(*) FROM ops
                        WHERE pushed = 0 AND kind = ? AND notebook_path = ? AND \(Self.sameNotebook)
                        """,
                    arguments: [
                        PaperOp.Kind.notebookCreate.rawValue, oldPath, notebookID, notebookID,
                    ])
                ?? 0
            guard unpushedCreate > 0 else { return false }
            try db.execute(
                sql: """
                    UPDATE ops SET notebook_path = ?
                    WHERE pushed = 0 AND notebook_path = ? AND \(Self.sameNotebook)
                    """,
                arguments: [newPath, oldPath, notebookID, notebookID])
            return true
        }
    }

    /// The rows of one notebook, its id bound to both placeholders. An empty
    /// id matches every row, and a row queued before ops carried an id
    /// matches every notebook, as the path alone did.
    private static let sameNotebook = "(? = '' OR notebook_id = ? OR notebook_id = '')"

    /// The unpushed ops, parked ones included, that appending a delete of
    /// this notebook -- or of `pageID` in it -- drops. The device trash keeps
    /// them so the undo toast can queue the same work again.
    public func unpushed(notebookPath: String, notebookID: String = "", pageID: String? = nil)
        throws -> [PaperOp]
    {
        try database.queue.read { db in
            guard let pageID else {
                return try Row.fetchAll(
                    db,
                    sql: """
                        SELECT * FROM ops
                        WHERE pushed = 0 AND notebook_path = ? AND \(Self.sameNotebook)
                        ORDER BY id
                        """,
                    arguments: [notebookPath, notebookID, notebookID]
                ).map(Self.decode)
            }
            return try Row.fetchAll(
                db,
                sql: """
                    SELECT * FROM ops
                    WHERE pushed = 0 AND notebook_path = ? AND page_id = ? AND kind IN (?, ?, ?, ?)
                    ORDER BY id
                    """,
                arguments: [
                    notebookPath, pageID,
                    PaperOp.Kind.pageCreate.rawValue, PaperOp.Kind.pagePut.rawValue,
                    PaperOp.Kind.pageRender.rawValue, PaperOp.Kind.pageText.rawValue,
                ]
            ).map(Self.decode)
        }
    }

    /// Whether a delete still in the queue covers this page: its own
    /// `pageDelete`, or the `notebookDelete` of its notebook. The coalescing
    /// rules match by path, so an op queued before a rename outlives a delete
    /// made after it; the page being gone is then the user's doing, not a
    /// loss.
    public func hasQueuedDelete(pageID: String, notebookID: String) throws -> Bool {
        try database.queue.read { db in
            let count =
                try Int.fetchOne(
                    db,
                    sql: """
                        SELECT COUNT(*) FROM ops
                        WHERE pushed = 0
                          AND ((kind = ? AND page_id = ?)
                            OR (kind = ? AND notebook_id = ? AND notebook_id <> ''))
                        """,
                    arguments: [
                        PaperOp.Kind.pageDelete.rawValue, pageID,
                        PaperOp.Kind.notebookDelete.rawValue, notebookID,
                    ])
                ?? 0
            return count > 0
        }
    }

    /// Whether any op for this page, parked ones included, has not reached
    /// the server -- `except` aside. A pull writes nothing over such a page.
    public func hasUnpushedWork(pageID: String, except: Int64? = nil) throws -> Bool {
        try database.queue.read { db in
            let count =
                try Int.fetchOne(
                    db,
                    sql: "SELECT COUNT(*) FROM ops WHERE pushed = 0 AND page_id = ? AND id IS NOT ?",
                    arguments: [pageID, except])
                ?? 0
            return count > 0
        }
    }

    /// Drops a notebook's unpushed ops -- `except` aside -- that its being
    /// sent up again as new makes moot: what each would send, the new create
    /// and its pages carry as they are now. Folder ops are kept.
    public func dropUnpushedWork(ofNotebook notebookID: String, except: Int64? = nil) throws {
        guard !notebookID.isEmpty else { return }
        let folderKinds = [
            PaperOp.Kind.folderCreate, .folderRename, .folderPatch, .folderDelete,
        ].map { "'\($0.rawValue)'" }.joined(separator: ", ")
        try database.queue.write { db in
            try db.execute(
                sql: """
                    DELETE FROM ops WHERE pushed = 0 AND notebook_id = ? AND id IS NOT ?
                      AND kind NOT IN (\(folderKinds))
                    """,
                arguments: [notebookID, except])
        }
    }

    /// Gives a page's unpushed ops -- `except` aside -- another page id: the
    /// page they describe became that page (a conflict sibling).
    public func repointPage(from pageID: String, to newID: String, except: Int64? = nil) throws {
        guard pageID != newID else { return }
        try database.queue.write { db in
            try db.execute(
                sql: "UPDATE ops SET page_id = ? WHERE pushed = 0 AND page_id = ? AND id IS NOT ?",
                arguments: [newID, pageID, except])
        }
    }

    /// The queue the pusher drains, oldest first. Parked ops are excluded: they
    /// are not going anywhere until something changes, and leaving them at the
    /// head would stop every op behind them. So are ops of a kind this build
    /// cannot read: `parked()` lists them instead.
    public func pending(limit: Int? = nil) throws -> [PaperOp] {
        try database.queue.read { db in
            var sql = """
                SELECT * FROM ops WHERE pushed = 0 AND parked_reason IS NULL
                  AND kind IN (\(Self.sendableKinds)) ORDER BY id
                """
            if let limit { sql += " LIMIT \(max(1, limit))" }
            return try Row.fetchAll(db, sql: sql).map(Self.decode)
        }
    }

    public func pendingCount() throws -> Int {
        try database.queue.read { db in
            try Int.fetchOne(
                db,
                sql: """
                    SELECT COUNT(*) FROM ops WHERE pushed = 0 AND parked_reason IS NULL
                      AND kind IN (\(Self.sendableKinds))
                    """) ?? 0
        }
    }

    /// The kinds this build sends, as an SQL list. Built from the enum, so
    /// it holds only fixed identifiers.
    private static let sendableKinds = PaperOp.Kind.sendable.map { "'\($0)'" }
        .joined(separator: ", ")

    /// Page directory paths with unpushed local work, so a pull never
    /// overwrites ink that has not reached the server yet.
    public func pendingPageKeys() throws -> Set<String> {
        try database.queue.read { db in
            let rows = try Row.fetchAll(
                db, sql: "SELECT notebook_path, page_id FROM ops WHERE pushed = 0")
            return Set(
                rows.compactMap { row -> String? in
                    let notebook: String = row["notebook_path"] ?? ""
                    let page: String = row["page_id"] ?? ""
                    guard !notebook.isEmpty, !page.isEmpty else { return nil }
                    return "\(notebook)/pages/\(page)"
                })
        }
    }

    /// Notebook paths with unpushed local work of any kind.
    public func pendingNotebookPaths() throws -> Set<String> {
        try database.queue.read { db in
            Set(
                try String.fetchAll(
                    db, sql: "SELECT DISTINCT notebook_path FROM ops WHERE pushed = 0"))
        }
    }

    /// The op reached the server. Removed rather than flagged: a journal is a
    /// queue of work outstanding, and rows that are done are only ballast.
    public func complete(_ id: Int64) throws {
        try database.queue.write { db in
            try db.execute(sql: "DELETE FROM ops WHERE id = ?", arguments: [id])
        }
    }

    /// Stops retrying `id` and records why. Never deletes: the local files are
    /// still the user's, and a later release (or a fixed server) can unpark.
    public func park(_ id: Int64, reason: String) throws {
        try database.queue.write { db in
            try db.execute(
                sql: "UPDATE ops SET parked_reason = ? WHERE id = ?", arguments: [reason, id])
        }
    }

    /// Ops that are not being sent, and why: the server refused them, or this
    /// build cannot read their kind (`PaperOp.unknownKindReason`).
    public func parked() throws -> [PaperOp] {
        try database.queue.read { db in
            try Row.fetchAll(
                db,
                sql: """
                    SELECT * FROM ops WHERE pushed = 0
                      AND (parked_reason IS NOT NULL OR kind NOT IN (\(Self.sendableKinds)))
                    ORDER BY id
                    """
            ).map(Self.decode)
        }
    }

    /// Puts every parked op back in the queue -- what "try again" does.
    @discardableResult
    public func unparkAll() throws -> Int {
        try database.queue.write { db in
            try db.execute(sql: "UPDATE ops SET parked_reason = NULL WHERE parked_reason IS NOT NULL")
            return db.changesCount
        }
    }

    // MARK: - Row mapping

    /// The op's notebook id, or the id of the notebook the index has at its
    /// path. Every producer journals after the notebook's row is written, so
    /// the index knows the id. A rename or move is the exception -- its op
    /// carries the path the notebook has just left -- and names it itself.
    private static func notebookID(of op: PaperOp, in db: Database) throws -> String {
        if !op.notebookID.isEmpty { return op.notebookID }
        return try String.fetchOne(
            db, sql: "SELECT id FROM notebooks WHERE path = ?", arguments: [op.notebookPath]) ?? ""
    }

    private static func insert(_ op: PaperOp, in db: Database) throws -> Int64 {
        let payload = String(
            data: (try? JSONEncoder().encode(op.payload)) ?? Data("{}".utf8), encoding: .utf8)
            ?? "{}"
        let notebookID = try Self.notebookID(of: op, in: db)
        try db.execute(
            sql: """
                INSERT INTO ops
                    (kind, notebook_path, notebook_id, page_id, payload, created_at, pushed)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                """,
            arguments: [
                op.kind.rawValue, op.notebookPath, notebookID, op.pageID, payload,
                op.createdAt.timeIntervalSince1970,
            ])
        return db.lastInsertedRowID
    }

    private static func decode(_ row: Row) -> PaperOp {
        let payloadText: String = row["payload"] ?? "{}"
        let payload =
            (try? JSONDecoder().decode([String: String].self, from: Data(payloadText.utf8))) ?? [:]
        // A kind this build does not know is never read as one it does: it
        // used to come back as a page save and was sent as one.
        let kind = PaperOp.Kind(rawValue: row["kind"] ?? "") ?? .unknown
        let known = kind != .unknown
        return PaperOp(
            id: row["id"],
            kind: kind,
            notebookPath: row["notebook_path"] ?? "",
            notebookID: row["notebook_id"] ?? "",
            pageID: row["page_id"] ?? "",
            payload: payload,
            createdAt: Date(timeIntervalSince1970: row["created_at"] ?? 0),
            pushed: row["pushed"] ?? false,
            parkedReason: known ? row["parked_reason"] as String? : PaperOp.unknownKindReason)
    }
}
