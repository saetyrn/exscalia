import Foundation
import GRDB

/// The device's metadata store: the index over the file tree, the outbound op
/// journal, the snapshot ring and (from M5) the local text index.
///
/// The files are the source of truth. This database is a cache that can always
/// be rebuilt by `Reconciler`, which is why every schema change is an additive
/// migration and nothing here is ever the only copy of anything.
public struct PaperDatabase: Sendable {
    public let queue: DatabaseQueue

    public init(at url: URL) throws {
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        var configuration = Configuration()
        configuration.foreignKeysEnabled = true
        self.queue = try DatabaseQueue(path: url.path(percentEncoded: false), configuration: configuration)
        try Self.migrator.migrate(queue)
    }

    /// In-memory instance for tests.
    public init() throws {
        self.queue = try DatabaseQueue()
        try Self.migrator.migrate(queue)
    }

    static var migrator: DatabaseMigrator {
        var migrator = DatabaseMigrator()

        migrator.registerMigration("v1") { db in
            try db.create(table: "notebooks") { table in
                table.column("id", .text).primaryKey()
                table.column("path", .text).notNull().unique()
                table.column("title", .text).notNull()
                table.column("folder", .text).notNull().defaults(to: "")
                table.column("template", .text).notNull()
                table.column("paper_size", .text).notNull()
                table.column("cover", .text)
                table.column("tags", .text).notNull().defaults(to: "[]")
                table.column("favorite", .boolean).notNull().defaults(to: false)
                table.column("created", .double).notNull()
                table.column("modified", .double).notNull()
                table.column("opened", .double)
                table.column("seq", .integer).notNull().defaults(to: 0)
            }
            try db.create(index: "notebooks_on_folder", on: "notebooks", columns: ["folder"])
            try db.create(index: "notebooks_on_modified", on: "notebooks", columns: ["modified"])

            try db.create(table: "pages") { table in
                table.column("id", .text).primaryKey()
                table.column("notebook_id", .text).notNull()
                    .references("notebooks", onDelete: .cascade)
                table.column("position", .integer).notNull()
                table.column("template", .text).notNull()
                table.column("paper_size", .text).notNull()
                table.column("created", .double).notNull()
                table.column("modified", .double).notNull()
                table.column("revision", .integer).notNull().defaults(to: 0)
                table.column("render_revision", .integer).notNull().defaults(to: 0)
                table.column("text_revision", .integer).notNull().defaults(to: 0)
                table.column("conflict_of", .text)
                table.column("seq", .integer).notNull().defaults(to: 0)
            }
            try db.create(
                index: "pages_on_notebook_position", on: "pages",
                columns: ["notebook_id", "position"])

            // Device-local reading position. Deliberately not part of the
            // synced model: where this iPad was looking is not a fact about the
            // notebook.
            try db.create(table: "notebook_state") { table in
                table.column("notebook_id", .text).primaryKey()
                    .references("notebooks", onDelete: .cascade)
                table.column("last_page_id", .text)
                table.column("zoom", .double).notNull().defaults(to: 1)
                table.column("offset_x", .double).notNull().defaults(to: 0)
                table.column("offset_y", .double).notNull().defaults(to: 0)
            }

            // Outbound journal. M2 appends and coalesces; nothing consumes it
            // until the M3 sync engine.
            try db.create(table: "ops") { table in
                table.autoIncrementedPrimaryKey("id")
                table.column("kind", .text).notNull()
                table.column("notebook_path", .text).notNull().defaults(to: "")
                table.column("page_id", .text).notNull().defaults(to: "")
                table.column("payload", .text).notNull().defaults(to: "{}")
                table.column("created_at", .double).notNull()
                table.column("pushed", .boolean).notNull().defaults(to: false)
                table.column("parked_reason", .text)
            }
            try db.create(index: "ops_on_pushed_id", on: "ops", columns: ["pushed", "id"])

            try db.create(table: "sync_state") { table in
                table.column("key", .text).primaryKey()
                table.column("value", .text).notNull()
            }

            try db.create(table: "snapshots") { table in
                table.autoIncrementedPrimaryKey("id")
                table.column("page_id", .text).notNull()
                table.column("path", .text).notNull()
                table.column("bytes", .integer).notNull()
                table.column("created_at", .double).notNull()
            }
            try db.create(
                index: "snapshots_on_page_created", on: "snapshots",
                columns: ["page_id", "created_at"])

            // Created now so M5 is a code change and not a migration on a
            // device full of ink. Nothing writes to either table in M2.
            try db.create(table: "page_text") { table in
                table.column("page_id", .text).primaryKey()
                table.column("notebook_path", .text).notNull().defaults(to: "")
                table.column("transcript", .text).notNull().defaults(to: "")
                table.column("lines", .text).notNull().defaults(to: "[]")
                table.column("for_revision", .integer).notNull().defaults(to: 0)
                table.column("updated_at", .double).notNull().defaults(to: 0)
            }
            try db.create(virtualTable: "page_fts", using: FTS5()) { table in
                // Trigram, so a search for a fragment inside a handwritten
                // word still hits. Spelled out because GRDB has no shorthand.
                table.tokenizer = FTS5TokenizerDescriptor(components: ["trigram"])
                table.column("page_id").notIndexed()
                table.column("transcript")
            }
        }

        // Page metadata and corrected OCR are additive. Existing installs have
        // a v1 database and must be upgraded in place; all columns are cached
        // projections of the JSON tree and are therefore safe to rebuild.
        migrator.registerMigration("v2-paper-metadata") { db in
            try db.alter(table: "pages") { table in
                table.add(column: "title", .text)
                table.add(column: "bookmarked", .boolean).notNull().defaults(to: false)
                table.add(column: "custom_width", .double)
                table.add(column: "custom_height", .double)
            }
            try db.alter(table: "page_text") { table in
                table.add(column: "corrected_transcript", .text)
                table.add(column: "corrections", .text).notNull().defaults(to: "[]")
                table.add(column: "corrections_revision", .integer)
                table.add(column: "transcript_ai", .text)
            }
        }

        // Ops name their notebook by id as well as by path: the path is what
        // the server knows it by until a queued rename reaches it, the id is
        // how the pusher finds the files on this device after a rename or move
        // took them elsewhere. Rows already queued take the id of whatever
        // notebook still sits at their path; the rest fall back to the path.
        migrator.registerMigration("v3-ops-notebook-id") { db in
            try db.alter(table: "ops") { table in
                table.add(column: "notebook_id", .text).notNull().defaults(to: "")
            }
            try db.execute(
                sql: """
                    UPDATE ops SET notebook_id = COALESCE(
                        (SELECT id FROM notebooks WHERE notebooks.path = ops.notebook_path), '')
                    """)
        }

        return migrator
    }
}
