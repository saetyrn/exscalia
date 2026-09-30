import Foundation
import GRDB

/// One thing that happened locally and has to reach the server eventually.
///
/// `Codable` because the device trash keeps the ops a delete dropped, so the
/// undo toast can queue the same work again.
public struct PaperOp: Equatable, Sendable, Codable {
    public enum Kind: String, Codable, Sendable {
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
    }

    public var id: Int64?
    public var kind: Kind
    /// The path the *server* knows the notebook by when this op reaches it.
    /// A rename or move still queued behind the op has not moved the server's
    /// copy yet, so this is the URL; where the files are now goes by id.
    public var notebookPath: String
    /// The notebook's id: how the pusher finds its files on this device after
    /// a rename or move has taken them somewhere else. Empty on rows queued
    /// before ops carried it, and on a producer that did not say, in which
    /// case the journal fills it from the index as the op is written.
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
    @discardableResult
    public func append(_ op: PaperOp) throws -> Int64? {
        try database.queue.write { db in
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
            case .notebookCreate, .notebookRename, .notebookMove, .pageCreate, .assetPut:
                break
            }
            return try Self.insert(op, in: db)
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

    /// The queue the pusher drains, oldest first. Parked ops are excluded: they
    /// are not going anywhere until something changes, and leaving them at the
    /// head would stop every op behind them.
    public func pending(limit: Int? = nil) throws -> [PaperOp] {
        try database.queue.read { db in
            var sql = "SELECT * FROM ops WHERE pushed = 0 AND parked_reason IS NULL ORDER BY id"
            if let limit { sql += " LIMIT \(max(1, limit))" }
            return try Row.fetchAll(db, sql: sql).map(Self.decode)
        }
    }

    public func pendingCount() throws -> Int {
        try database.queue.read { db in
            try Int.fetchOne(
                db, sql: "SELECT COUNT(*) FROM ops WHERE pushed = 0 AND parked_reason IS NULL") ?? 0
        }
    }

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

    public func parked() throws -> [PaperOp] {
        try database.queue.read { db in
            try Row.fetchAll(
                db, sql: "SELECT * FROM ops WHERE pushed = 0 AND parked_reason IS NOT NULL ORDER BY id"
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
        return PaperOp(
            id: row["id"],
            kind: PaperOp.Kind(rawValue: row["kind"] ?? "") ?? .pagePut,
            notebookPath: row["notebook_path"] ?? "",
            notebookID: row["notebook_id"] ?? "",
            pageID: row["page_id"] ?? "",
            payload: payload,
            createdAt: Date(timeIntervalSince1970: row["created_at"] ?? 0),
            pushed: row["pushed"] ?? false,
            parkedReason: row["parked_reason"])
    }
}
