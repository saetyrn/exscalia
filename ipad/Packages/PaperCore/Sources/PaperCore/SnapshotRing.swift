import Foundation
import GRDB

/// Ten recoverable versions of every page, kept on the device.
///
/// The git history on the server has a ten-second autocommit debounce, which
/// folds a whole writing session into one version. This ring is the finer grain
/// underneath it: "Page history on this iPad".
public struct SnapshotRing: Sendable {
    /// Versions kept per page.
    public static let perPage = 10
    /// Whole-store ceiling. Ink is small, but a 200-page PDF annotation session
    /// is not, and a recovery aid must never be the thing that fills the disk.
    public static let byteCap = 200 * 1024 * 1024

    private let paths: PaperPaths
    private let database: PaperDatabase

    public init(paths: PaperPaths, database: PaperDatabase) {
        self.paths = paths
        self.database = database
    }

    /// Records `data` as the newest snapshot of `pageID` and trims the ring.
    /// Identical consecutive bytes are dropped: a flush with no change is not a
    /// version.
    @discardableResult
    public func push(_ data: Data, pageID: String, at date: Date = Date()) throws -> Bool {
        if let newest = try newestPath(pageID: pageID),
            let existing = try? Data(contentsOf: newest),
            existing == data
        {
            return false
        }

        let directory = paths.snapshotDirectory(pageID)
        let url = directory.appending(
            path: "\(Int(date.timeIntervalSince1970 * 1000)).pkdrawing",
            directoryHint: .notDirectory)
        try AtomicFile.write(data, to: url)
        try database.queue.write { db in
            try db.execute(
                sql: """
                    INSERT INTO snapshots (page_id, path, bytes, created_at) VALUES (?, ?, ?, ?)
                    """,
                arguments: [
                    pageID, url.lastPathComponent, data.count, date.timeIntervalSince1970,
                ])
        }
        try trimRing(pageID: pageID)
        try enforceByteCap()
        return true
    }

    public struct Snapshot: Equatable, Sendable, Identifiable {
        public let id: Int64
        public let pageID: String
        public let url: URL
        public let bytes: Int
        public let createdAt: Date
    }

    /// Newest first, which is the order the history sheet shows.
    public func snapshots(pageID: String) throws -> [Snapshot] {
        try database.queue.read { db in
            try Row.fetchAll(
                db,
                sql: "SELECT * FROM snapshots WHERE page_id = ? ORDER BY created_at DESC",
                arguments: [pageID]
            ).map { row in
                Snapshot(
                    id: row["id"] ?? 0,
                    pageID: pageID,
                    url: paths.snapshotDirectory(pageID)
                        .appending(path: row["path"] ?? "", directoryHint: .notDirectory),
                    bytes: row["bytes"] ?? 0,
                    createdAt: Date(timeIntervalSince1970: row["created_at"] ?? 0))
            }
        }
    }

    public func totalBytes() throws -> Int {
        try database.queue.read { db in
            try Int.fetchOne(db, sql: "SELECT COALESCE(SUM(bytes), 0) FROM snapshots") ?? 0
        }
    }

    private func newestPath(pageID: String) throws -> URL? {
        try database.queue.read { db in
            try String.fetchOne(
                db,
                sql: "SELECT path FROM snapshots WHERE page_id = ? ORDER BY created_at DESC LIMIT 1",
                arguments: [pageID])
        }
        .map { paths.snapshotDirectory(pageID).appending(path: $0, directoryHint: .notDirectory) }
    }

    private func trimRing(pageID: String) throws {
        let doomed = try database.queue.read { db in
            try Row.fetchAll(
                db,
                sql: """
                    SELECT id, path FROM snapshots WHERE page_id = ?
                    ORDER BY created_at DESC LIMIT -1 OFFSET ?
                    """,
                arguments: [pageID, Self.perPage])
        }
        try remove(doomed, pageID: pageID)
    }

    /// Oldest snapshots across every page go first once the store is over the
    /// cap; a page always keeps its newest version.
    private func enforceByteCap() throws {
        var total = try totalBytes()
        guard total > Self.byteCap else { return }
        let candidates = try database.queue.read { db in
            try Row.fetchAll(
                db, sql: "SELECT id, page_id, path, bytes FROM snapshots ORDER BY created_at ASC")
        }
        var newestPerPage: [String: Int64] = [:]
        for page in Set(candidates.compactMap { $0["page_id"] as String? }) {
            if let id = try database.queue.read({ db in
                try Int64.fetchOne(
                    db,
                    sql:
                        "SELECT id FROM snapshots WHERE page_id = ? ORDER BY created_at DESC LIMIT 1",
                    arguments: [page])
            }) {
                newestPerPage[page] = id
            }
        }
        for row in candidates {
            guard total > Self.byteCap else { break }
            let id: Int64 = row["id"] ?? 0
            let pageID: String = row["page_id"] ?? ""
            if newestPerPage[pageID] == id { continue }
            try remove([row], pageID: pageID)
            total -= (row["bytes"] as Int? ?? 0)
        }
    }

    private func remove(_ rows: [Row], pageID: String) throws {
        guard !rows.isEmpty else { return }
        for row in rows {
            let name: String = row["path"] ?? ""
            let page: String = row["page_id"] as String? ?? pageID
            try? FileManager.default.removeItem(
                at: paths.snapshotDirectory(page)
                    .appending(path: name, directoryHint: .notDirectory))
        }
        let ids = rows.compactMap { $0["id"] as Int64? }
        try database.queue.write { db in
            for id in ids {
                try db.execute(sql: "DELETE FROM snapshots WHERE id = ?", arguments: [id])
            }
        }
    }
}
