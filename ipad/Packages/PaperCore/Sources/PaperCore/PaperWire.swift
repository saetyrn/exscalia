import Foundation

/// The shapes `/api/paper` speaks, decoded exactly as the server writes them.
///
/// Deliberately separate from the on-device models in `Models.swift`: the two
/// disagree on purpose. A page's `size` is a name on the device (`a4`) and a
/// pair of points on the wire (`{width, height}`, the name riding beside it as
/// `size_name`), and the wire carries fields
/// -- `path`, `notebook`, the three blob URLs -- that are facts about the
/// server rather than about the page. Decoding into the local model would mean
/// inventing values for both directions.
///
/// Every optional here is optional because the server may legitimately omit
/// it, never to paper over a decode failure.

// MARK: - Library

public struct PaperLibrary: Codable, Equatable, Sendable {
    public var folders: [String]
    public var notebooks: [NotebookSummary]

    public init(folders: [String] = [], notebooks: [NotebookSummary] = []) {
        self.folders = folders
        self.notebooks = notebooks
    }
}

public struct NotebookSummary: Codable, Equatable, Sendable, Identifiable {
    public var path: String
    public var folder: String?
    public var notebookID: String?
    public var title: String?
    public var created: String?
    public var modified: String?
    public var template: String?
    public var paperSize: String?
    public var tags: [String]?
    public var favorite: Bool?
    public var cover: String?
    public var coverURL: String?
    public var pageCount: Int?
    public var seq: Int?

    /// The vault-relative package path is the key everywhere, so it is the id.
    public var id: String { path }

    enum CodingKeys: String, CodingKey {
        case path, folder, title, created, modified, template, paperSize, tags, favorite, cover
        case notebookID = "id"
        case coverURL = "cover_url"
        case pageCount = "page_count"
        case seq
    }

    public init(path: String, notebookID: String? = nil, title: String? = nil, seq: Int? = nil) {
        self.path = path
        self.notebookID = notebookID
        self.title = title
        self.seq = seq
    }
}

// MARK: - Notebook manifest

public struct NotebookManifest: Codable, Equatable, Sendable {
    public var path: String
    public var id: String?
    public var title: String?
    public var created: String?
    public var modified: String?
    public var template: String?
    public var paperSize: String?
    public var cover: String?
    public var tags: [String]?
    public var favorite: Bool?
    public var pages: [String]?
    public var seq: Int?
    /// Every page's public record in `pages[]` order; the manifest GET carries
    /// it so one request paints a whole notebook.
    public var pagesDetail: [RemotePage]?
    /// The rename answer only: every page the move re-stamped. Absent from an
    /// older server.
    public var movedPages: [MovedPage]?

    enum CodingKeys: String, CodingKey {
        case path, id, title, created, modified, template, paperSize, cover, tags, favorite
        case pages, seq
        case pagesDetail = "pages_detail"
        case movedPages = "moved_pages"
    }

    public init(path: String, id: String? = nil, pages: [String]? = nil) {
        self.path = path
        self.id = id
        self.pages = pages
    }
}

/// A page a notebook rename carried along: the server gave it a new `seq`
/// so other devices hear of it at its new path, and nothing else changed.
public struct MovedPage: Codable, Equatable, Sendable {
    public var id: String
    public var seq: Int
    /// The seq it had before the move; a device holding the page at exactly
    /// this one is up to date with it.
    public var previousSeq: Int?

    enum CodingKeys: String, CodingKey {
        case id, seq
        case previousSeq = "previous_seq"
    }

    public init(id: String, seq: Int, previousSeq: Int?) {
        self.id = id
        self.seq = seq
        self.previousSeq = previousSeq
    }
}

// MARK: - Pages

/// Page dimensions in points, as the server stores them.
public struct RemoteSize: Codable, Equatable, Sendable {
    public var width: Double
    public var height: Double

    public init(width: Double, height: Double) {
        self.width = width
        self.height = height
    }

    /// The closest named size, which is what the device stores. Falls back to
    /// A4 rather than failing: an unknown size is a layout question, never a
    /// reason to drop a page on the floor.
    public var named: PaperSize {
        for candidate in PaperSize.allCases {
            let points = candidate.points
            if abs(points.width - width) < 1, abs(points.height - height) < 1 {
                return candidate
            }
        }
        return .a4
    }
}

public struct RemotePage: Codable, Equatable, Sendable, Identifiable {
    public var id: String
    public var path: String?
    public var notebook: String?
    public var title: String?
    /// Missing on responses from pre-bookmark servers; defaults to false.
    public var bookmarked: Bool
    public var template: String?
    public var size: RemoteSize?
    /// The preset the page is, which its dimensions cannot say for an
    /// infinite page. Missing on pages no client has named yet.
    public var sizeName: String?
    public var sizePoints: RemoteSize?
    public var objects: [PageObject]?
    public var pdf: PDFBinding?
    public var created: String?
    public var modified: String?
    public var revision: Int
    public var renderRevision: Int
    public var textRevision: Int
    public var conflictOf: String?
    public var seq: Int?
    public var drawingURL: String?
    public var renderURL: String?
    public var textURL: String?

    enum CodingKeys: String, CodingKey {
        case id, path, notebook, title, bookmarked, isBookmarked = "is_bookmarked"
        case camelBookmarked = "isBookmarked"
        case template, size, sizePoints = "size_points", objects, pdf, created, modified, revision, seq
        case sizeName = "size_name"
        case renderRevision = "render_revision"
        case textRevision = "text_revision"
        case conflictOf = "conflict_of"
        case drawingURL = "drawing_url"
        case renderURL = "render_url"
        case textURL = "text_url"
    }

    public init(
        id: String, path: String? = nil, notebook: String? = nil, title: String? = nil,
        bookmarked: Bool = false, revision: Int = 1, renderRevision: Int = 0,
        textRevision: Int = 0, conflictOf: String? = nil, size: RemoteSize? = nil,
        sizeName: String? = nil, sizePoints: RemoteSize? = nil, objects: [PageObject]? = nil,
        pdf: PDFBinding? = nil
    ) {
        self.id = id
        self.path = path
        self.notebook = notebook
        self.title = title
        self.bookmarked = bookmarked
        self.revision = revision
        self.renderRevision = renderRevision
        self.textRevision = textRevision
        self.conflictOf = conflictOf
        self.size = size
        self.sizeName = sizeName
        self.sizePoints = sizePoints
        self.objects = objects
        self.pdf = pdf
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        path = try container.decodeIfPresent(String.self, forKey: .path)
        notebook = try container.decodeIfPresent(String.self, forKey: .notebook)
        title = try container.decodeIfPresent(String.self, forKey: .title)
        let bookmarked = try container.decodeIfPresent(Bool.self, forKey: .bookmarked)
        let legacyBookmarked = try container.decodeIfPresent(Bool.self, forKey: .isBookmarked)
        let camelBookmarked = try container.decodeIfPresent(Bool.self, forKey: .camelBookmarked)
        self.bookmarked = bookmarked ?? legacyBookmarked ?? camelBookmarked ?? false
        template = try container.decodeIfPresent(String.self, forKey: .template)
        size = try container.decodeIfPresent(RemoteSize.self, forKey: .size)
        sizeName = try container.decodeIfPresent(String.self, forKey: .sizeName)
        sizePoints = try container.decodeIfPresent(RemoteSize.self, forKey: .sizePoints)
        objects = try container.decodeIfPresent([PageObject].self, forKey: .objects)
        pdf = try container.decodeIfPresent(PDFBinding.self, forKey: .pdf)
        created = try container.decodeIfPresent(String.self, forKey: .created)
        modified = try container.decodeIfPresent(String.self, forKey: .modified)
        revision = try container.decodeIfPresent(Int.self, forKey: .revision) ?? 0
        renderRevision = try container.decodeIfPresent(Int.self, forKey: .renderRevision) ?? 0
        textRevision = try container.decodeIfPresent(Int.self, forKey: .textRevision) ?? 0
        conflictOf = try container.decodeIfPresent(String.self, forKey: .conflictOf)
        seq = try container.decodeIfPresent(Int.self, forKey: .seq)
        drawingURL = try container.decodeIfPresent(String.self, forKey: .drawingURL)
        renderURL = try container.decodeIfPresent(String.self, forKey: .renderURL)
        textURL = try container.decodeIfPresent(String.self, forKey: .textURL)
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(id, forKey: .id)
        try container.encodeIfPresent(path, forKey: .path)
        try container.encodeIfPresent(notebook, forKey: .notebook)
        try container.encodeIfPresent(title, forKey: .title)
        try container.encode(bookmarked, forKey: .bookmarked)
        try container.encodeIfPresent(template, forKey: .template)
        try container.encodeIfPresent(size, forKey: .size)
        try container.encodeIfPresent(sizeName, forKey: .sizeName)
        try container.encodeIfPresent(sizePoints, forKey: .sizePoints)
        try container.encodeIfPresent(objects, forKey: .objects)
        try container.encodeIfPresent(pdf, forKey: .pdf)
        try container.encodeIfPresent(created, forKey: .created)
        try container.encodeIfPresent(modified, forKey: .modified)
        try container.encode(revision, forKey: .revision)
        try container.encode(renderRevision, forKey: .renderRevision)
        try container.encode(textRevision, forKey: .textRevision)
        try container.encodeIfPresent(conflictOf, forKey: .conflictOf)
        try container.encodeIfPresent(seq, forKey: .seq)
        try container.encodeIfPresent(drawingURL, forKey: .drawingURL)
        try container.encodeIfPresent(renderURL, forKey: .renderURL)
        try container.encodeIfPresent(textURL, forKey: .textURL)
    }
}

/// A decoded page and the bytes it came in, kept together so a pull can write
/// the server's `page.json` verbatim -- fields this milestone does not model
/// (M4's `objects` and `pdf`) survive the round trip untouched.
public struct RemotePageResponse: Sendable {
    public let page: RemotePage
    public let raw: Data

    public init(page: RemotePage, raw: Data) {
        self.page = page
        self.raw = raw
    }
}

/// 200 from `PUT /api/paper/pages/…`.
public struct PageSaveResult: Codable, Equatable, Sendable {
    public var page: RemotePage
    public var revision: Int
    /// The render or text part described an older revision than the one the
    /// save landed on, so it needs redoing.
    public var stale: Bool?
}

/// 409 from the same route: both halves of the race, on disk, named.
public struct PageConflict: Codable, Equatable, Sendable {
    public struct Pair: Codable, Equatable, Sendable {
        public var original: RemotePage
        public var sibling: RemotePage
    }

    public var detail: String?
    public var conflict: Pair
}

/// What a page PUT can come back as. A conflict is an answer, not an error:
/// nothing was lost, and the client has work to do.
public enum PagePutOutcome: Sendable {
    case saved(PageSaveResult)
    case conflict(PageConflict)
}

// MARK: - Change feed

public struct ChangesFeed: Codable, Equatable, Sendable {
    public struct ChangedPage: Codable, Equatable, Sendable {
        public var path: String
        public var notebook: String
        public var id: String
        public var revision: Int
        public var renderRevision: Int
        public var textRevision: Int
        public var modified: String?
        public var seq: Int

        enum CodingKeys: String, CodingKey {
            case path, notebook, id, revision, modified, seq
            case renderRevision = "render_revision"
            case textRevision = "text_revision"
        }

        public init(
            path: String, notebook: String, id: String, revision: Int, renderRevision: Int = 0,
            textRevision: Int = 0, seq: Int = 0
        ) {
            self.path = path
            self.notebook = notebook
            self.id = id
            self.revision = revision
            self.renderRevision = renderRevision
            self.textRevision = textRevision
            self.seq = seq
        }
    }

    public struct Deletion: Codable, Equatable, Sendable {
        public var path: String
        public var kind: String
        public var seq: Int

        public init(path: String, kind: String, seq: Int) {
            self.path = path
            self.kind = kind
            self.seq = seq
        }
    }

    public var epoch: String
    public var seq: Int
    /// The cursor cannot be brought up to date incrementally: reconcile from
    /// the library listing instead.
    public var full: Bool
    public var notebooks: [NotebookSummary]
    public var pages: [ChangedPage]
    public var deleted: [Deletion]

    public init(
        epoch: String, seq: Int, full: Bool, notebooks: [NotebookSummary] = [],
        pages: [ChangedPage] = [], deleted: [Deletion] = []
    ) {
        self.epoch = epoch
        self.seq = seq
        self.full = full
        self.notebooks = notebooks
        self.pages = pages
        self.deleted = deleted
    }
}

// MARK: - Trash and history

public struct PaperTrashItem: Codable, Equatable, Sendable, Identifiable {
    public var id: String
    public var kind: String
    public var originalPath: String
    public var deletedAt: String?
    public var expiresAt: String?
    public var notebook: String?
    public var pageID: String?
    public var position: Int?

    enum CodingKeys: String, CodingKey {
        case id, kind, notebook, position
        case originalPath = "original_path"
        case deletedAt = "deleted_at"
        case expiresAt = "expires_at"
        case pageID = "page_id"
    }

    public init(id: String, kind: String, originalPath: String, deletedAt: String? = nil) {
        self.id = id
        self.kind = kind
        self.originalPath = originalPath
        self.deletedAt = deletedAt
    }

    /// "Ideas" for a notebook, "Ideas · page" for a page: what the trash list
    /// shows instead of a vault path.
    public var displayTitle: String {
        let parts = originalPath.split(separator: "/").map(String.init)
        guard let package = parts.first(where: { $0.hasSuffix(".paper") }) else {
            return originalPath
        }
        let title = String(package.dropLast(".paper".count))
        if kind == "page" { return "\(title) · page" }
        return title
    }
}

public struct PaperTrashList: Codable, Equatable, Sendable {
    public var items: [PaperTrashItem]

    public init(items: [PaperTrashItem] = []) {
        self.items = items
    }
}

public struct PageVersion: Codable, Equatable, Sendable, Identifiable {
    public var sha: String
    public var shortSHA: String?
    public var message: String?
    public var author: String?
    public var committedAt: String?

    public var id: String { sha }

    enum CodingKeys: String, CodingKey {
        case sha, message, author
        case shortSHA = "short_sha"
        case committedAt = "committed_at"
    }

    public init(sha: String, shortSHA: String? = nil, message: String? = nil) {
        self.sha = sha
        self.shortSHA = shortSHA
        self.message = message
    }
}

public struct PageHistory: Codable, Equatable, Sendable {
    public var path: String
    public var versions: [PageVersion]

    public init(path: String, versions: [PageVersion] = []) {
        self.path = path
        self.versions = versions
    }
}

public struct AssetReference: Codable, Equatable, Sendable {
    public var path: String
    public var url: String
}

// MARK: - Blobs

/// The bytes of `drawing.pkdrawing`, `render.png` or `text.json`, or the fact
/// that the copy already held is current.
public struct BlobResponse: Sendable, Equatable {
    public var data: Data?
    public var etag: String?
    public var notModified: Bool
    /// The server has no such file. Not an error: a page with no render yet is
    /// ordinary.
    public var missing: Bool

    public init(data: Data? = nil, etag: String? = nil, notModified: Bool = false, missing: Bool = false) {
        self.data = data
        self.etag = etag
        self.notModified = notModified
        self.missing = missing
    }
}
