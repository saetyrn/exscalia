import CoreGraphics
import Foundation
import GRDB

/// One page that matched a search, with everything a result row needs.
public struct PageTextMatch: Equatable, Sendable, Identifiable {
    public let notebookPath: String
    public let pageID: String
    public let pageTitle: String?
    /// 1-based position in the notebook, which is what a person calls a page.
    public let pageNumber: Int
    /// The line the query was found in -- the one shown in the result.
    public let line: String
    /// Boxes of every matching line, in page points. The crop is their union.
    public let boxes: [PageObject.Frame]

    public var id: String { "\(notebookPath)/pages/\(pageID)" }

    public init(
        notebookPath: String, pageID: String, pageNumber: Int, line: String,
        pageTitle: String? = nil,
        boxes: [PageObject.Frame]
    ) {
        self.notebookPath = notebookPath
        self.pageID = pageID
        self.pageTitle = pageTitle
        self.pageNumber = pageNumber
        self.line = line
        self.boxes = boxes
    }

    /// The rectangle a result crop and the on-page highlight both use.
    public var unionBox: CGRect {
        boxes.dropFirst().reduce(boxes.first?.rect ?? .zero) { $0.union($1.rect) }
    }
}

/// Search results, grouped the way the plan asks: notebook, then pages.
///
/// A group with no pages is a notebook whose *title* matched. That is a real
/// answer -- "where is my Quarterly notebook" -- and dropping it because no
/// page mentions the word would make the search worse than the grid it
/// replaced.
public struct NotebookMatches: Equatable, Sendable, Identifiable {
    public let notebookPath: String
    public let notebookTitle: String
    public let pages: [PageTextMatch]

    public var id: String { notebookPath }

    public init(notebookPath: String, notebookTitle: String, pages: [PageTextMatch]) {
        self.notebookPath = notebookPath
        self.notebookTitle = notebookTitle
        self.pages = pages
    }
}

/// The device's own handwriting index: `page_text` holds the lines, `page_fts`
/// holds the transcript for matching.
///
/// FTS5 with the **trigram** tokeniser, which is what makes a search for a
/// fragment inside a handwritten word hit at all: recognition is imperfect, so
/// word-boundary matching would throw away most of the value. Trigram needs
/// three characters, so shorter queries fall back to a `LIKE` over the same
/// column rather than silently returning nothing.
public struct TextIndex: Sendable {
    /// Below this a trigram index has nothing to match on.
    public static let minimumTrigramLength = 3

    private let database: PaperDatabase

    public init(database: PaperDatabase) {
        self.database = database
    }

    // MARK: - Writing

    public func upsert(
        notebookPath: String, pageID: String, text: PageText, forRevision: Int,
        updatedAt: Date = Date()
    ) throws {
        let lines = String(
            data: (try? PaperJSON.encode(text.lines)) ?? Data("[]".utf8), encoding: .utf8) ?? "[]"
        let corrections = String(
            data: (try? PaperJSON.encode(text.corrections)) ?? Data("[]".utf8), encoding: .utf8)
            ?? "[]"
        let transcriptAI = text.transcriptAI.flatMap {
            try? String(data: PaperJSON.encode($0), encoding: .utf8)
        }
        try database.queue.write { db in
            try db.execute(
                sql: """
                    INSERT INTO page_text
                        (page_id, notebook_path, transcript, lines, corrected_transcript,
                         corrections, corrections_revision, transcript_ai, for_revision, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(page_id) DO UPDATE SET
                        notebook_path = excluded.notebook_path,
                        transcript = excluded.transcript,
                        lines = excluded.lines,
                        corrected_transcript = excluded.corrected_transcript,
                        corrections = excluded.corrections,
                        corrections_revision = excluded.corrections_revision,
                        transcript_ai = excluded.transcript_ai,
                        for_revision = excluded.for_revision,
                        updated_at = excluded.updated_at
                    """,
                arguments: [
                    pageID, notebookPath, text.transcript, lines, text.correctedTranscript,
                    corrections, text.correctionsRevision, transcriptAI, forRevision,
                    updatedAt.timeIntervalSince1970,
                ])
            try db.execute(sql: "DELETE FROM page_fts WHERE page_id = ?", arguments: [pageID])
            try db.execute(
                sql: "INSERT INTO page_fts (page_id, transcript) VALUES (?, ?)",
                arguments: [pageID, text.searchableTranscript])
        }
    }

    public func remove(pageID: String) throws {
        try database.queue.write { db in
            try db.execute(sql: "DELETE FROM page_text WHERE page_id = ?", arguments: [pageID])
            try db.execute(sql: "DELETE FROM page_fts WHERE page_id = ?", arguments: [pageID])
        }
    }

    /// When this page's text was last indexed, so a reconcile can skip a
    /// `text.json` it has already read.
    public func indexedAt(pageID: String) -> Date? {
        let value = try? database.queue.read { db in
            try Double.fetchOne(
                db, sql: "SELECT updated_at FROM page_text WHERE page_id = ?", arguments: [pageID])
        }
        return value.map(Date.init(timeIntervalSince1970:))
    }

    public func text(pageID: String) -> PageText? {
        let row = try? database.queue.read { db in
            try Row.fetchOne(
                db,
                sql: "SELECT transcript, lines, corrected_transcript, corrections, transcript_ai "
                    + "FROM page_text WHERE page_id = ?",
                arguments: [pageID])
        }
        guard let row else { return nil }
        let transcript: String = row["transcript"] ?? ""
        let linesText: String = row["lines"] ?? "[]"
        let lines =
            (try? PaperJSON.decode([RecognizedLine].self, from: Data(linesText.utf8))) ?? []
        let corrected: String? = row["corrected_transcript"]
        let correctionsText: String = row["corrections"] ?? "[]"
        let corrections =
            (try? PaperJSON.decode([TextCorrection].self, from: Data(correctionsText.utf8))) ?? []
        let aiText: String? = row["transcript_ai"]
        let ai = aiText.flatMap {
            try? PaperJSON.decode(AITranscript.self, from: Data($0.utf8))
        }
        return PageText(
            lines: lines, transcript: transcript, correctedTranscript: corrected,
            corrections: corrections, transcriptAI: ai,
            correctionsRevision: row["corrections_revision"])
    }

    /// Pages with any indexed text at all -- what "nothing has been read yet"
    /// is told apart from "nothing matched".
    public func indexedPageCount() -> Int {
        (try? database.queue.read { db in
            try Int.fetchOne(db, sql: "SELECT COUNT(*) FROM page_text") ?? 0
        }) ?? 0
    }

    // MARK: - Searching

    /// Every page whose transcript contains `query`, grouped by notebook, plus
    /// notebooks whose title matches.
    public func search(_ query: String, limit: Int = 100) -> [NotebookMatches] {
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return [] }
        let rows = (try? pageRows(trimmed, limit: limit)) ?? []

        var order: [String] = []
        var grouped: [String: [PageTextMatch]] = [:]
        var titles: [String: String] = [:]

        for row in rows {
            let notebookPath: String = row["notebook_path"] ?? ""
            guard !notebookPath.isEmpty else { continue }
            let pageID: String = row["page_id"] ?? ""
            let linesText: String = row["lines"] ?? "[]"
            let rawLines = (try? PaperJSON.decode([RecognizedLine].self, from: Data(linesText.utf8))) ?? []
            let pageText = PageText(
                lines: rawLines, transcript: row["transcript"] ?? "",
                correctedTranscript: row["corrected_transcript"],
                corrections: (try? PaperJSON.decode(
                    [TextCorrection].self,
                    from: Data((row["corrections"] ?? "[]").utf8))) ?? [],
                transcriptAI: (row["transcript_ai"] as String?).flatMap {
                    try? PaperJSON.decode(AITranscript.self, from: Data($0.utf8))
                },
                correctionsRevision: row["corrections_revision"])
            let lines = pageText.searchableLines
            let matching = lines.filter { $0.text.localizedCaseInsensitiveContains(trimmed) }
            // The transcript matched but no single line does: the query spans a
            // line break. Show the whole page rather than pretending it missed.
            let shown = matching.isEmpty ? lines : matching
            guard let first = shown.first else { continue }
            let match = PageTextMatch(
                notebookPath: notebookPath,
                pageID: pageID,
                pageNumber: (row["position"] as Int? ?? 0) + 1,
                line: first.text,
                pageTitle: row["page_title"],
                boxes: shown.map(\.box))
            if grouped[notebookPath] == nil {
                order.append(notebookPath)
                titles[notebookPath] = row["title"] ?? PaperStore.title(ofNotebookPath: notebookPath)
            }
            grouped[notebookPath, default: []].append(match)
        }

        for (path, title) in matchingNotebookTitles(trimmed, limit: limit) where grouped[path] == nil
        {
            order.append(path)
            titles[path] = title
            grouped[path] = []
        }

        return order.map { path in
            NotebookMatches(
                notebookPath: path,
                notebookTitle: titles[path] ?? PaperStore.title(ofNotebookPath: path),
                pages: (grouped[path] ?? []).sorted { $0.pageNumber < $1.pageNumber })
        }
    }

    /// The notebook's path and title come from the live rows, reached through
    /// the page, never from `page_text.notebook_path`: that is a copy taken
    /// when the page was read, and a rename, a move or a delete since would
    /// otherwise surface as a hit that opens nothing. Inner joins, so text
    /// left behind by a page that is gone matches nothing.
    private func pageRows(_ query: String, limit: Int) throws -> [Row] {
        try database.queue.read { db in
            let joins = """
                FROM page_text pt
                JOIN pages p ON p.id = pt.page_id
                JOIN notebooks n ON n.id = p.notebook_id
                """
            let columns =
                "SELECT pt.page_id AS page_id, n.path AS notebook_path, "
                + "pt.lines AS lines, pt.transcript AS transcript, "
                + "pt.corrected_transcript AS corrected_transcript, "
                + "pt.corrections AS corrections, pt.corrections_revision AS corrections_revision, "
                + "pt.transcript_ai AS transcript_ai, "
                + "p.title AS page_title, n.title AS title, p.position AS position "
            if query.count >= Self.minimumTrigramLength {
                return try Row.fetchAll(
                    db,
                    sql: columns
                        + """
                        FROM page_fts f
                        JOIN page_text pt ON pt.page_id = f.page_id
                        JOIN pages p ON p.id = pt.page_id
                        JOIN notebooks n ON n.id = p.notebook_id
                        WHERE page_fts MATCH ?
                        ORDER BY n.modified DESC, p.position ASC
                        LIMIT ?
                        """,
                    arguments: [Self.phrase(query), limit])
            }
            // One or two characters: no trigram to match, so scan. The index is
            // one device's notebooks, and an empty result here would be a lie.
            return try Row.fetchAll(
                db,
                sql: columns + joins
                    + """
                     WHERE COALESCE(pt.corrected_transcript, pt.transcript) LIKE ? ESCAPE '\\'
                    ORDER BY n.modified DESC, p.position ASC
                    LIMIT ?
                    """,
                arguments: ["%" + Self.escapedLike(query) + "%", limit])
        }
    }

    private func matchingNotebookTitles(_ query: String, limit: Int) -> [(String, String)] {
        let rows = try? database.queue.read { db in
            try Row.fetchAll(
                db,
                sql: """
                    SELECT path, title FROM notebooks
                    WHERE title LIKE ? ESCAPE '\\'
                    ORDER BY modified DESC LIMIT ?
                    """,
                arguments: ["%" + Self.escapedLike(query) + "%", limit])
        }
        return (rows ?? []).compactMap { row in
            guard let path: String = row["path"], let title: String = row["title"] else {
                return nil
            }
            return (path, title)
        }
    }

    /// An FTS5 phrase: whatever the user typed, quoted, so `AND`, `*` and a
    /// stray quote are text rather than syntax.
    static func phrase(_ query: String) -> String {
        "\"" + query.replacingOccurrences(of: "\"", with: "\"\"") + "\""
    }

    static func escapedLike(_ query: String) -> String {
        query
            .replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "%", with: "\\%")
            .replacingOccurrences(of: "_", with: "\\_")
    }
}
