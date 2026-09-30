import CoreGraphics
import Foundation

// MARK: - Paper size

/// The fixed page sizes Paper offers. Points, because everything downstream
/// (PencilKit, PDFKit, CoreGraphics) is in points.
public enum PaperSize: String, Codable, CaseIterable, Sendable {
    case a4
    case letter
    case iPad
    /// A page with no fixed bottom or right edge. `points` is only where it
    /// starts: the real extent lives in `Page.customSize` (`size_points`) and
    /// grows as the writing approaches an edge. Declared last so that
    /// `RemoteSize.named` and `PaperSize.nearest(to:)` still answer `.iPad`
    /// for a page that happens to measure 1024x1366.
    case infinite

    public var points: CGSize {
        switch self {
        case .a4: return CGSize(width: 595, height: 842)
        case .letter: return CGSize(width: 612, height: 792)
        case .iPad: return CGSize(width: 1024, height: 1366)
        case .infinite: return InfiniteCanvas.baseExtent
        }
    }

    /// True when the page may be extended rather than being a fixed sheet.
    public var isInfinite: Bool { self == .infinite }

    /// What a new notebook gets unless somebody chooses otherwise. A page that
    /// runs out of room is the thing people notice first, so the default is the
    /// one that does not.
    public static let defaultForNewNotebooks: PaperSize = .infinite

    /// Shown in the New notebook sheet and in VoiceOver.
    public var displayName: String {
        switch self {
        case .a4: return "A4"
        case .letter: return "Letter"
        case .iPad: return "iPad"
        case .infinite: return "Infinite"
        }
    }
}

// MARK: - Template

/// Paper ruling. Metrics are the research defaults: ruled 8mm, grid 5mm,
/// dotted 5mm, expressed here in points so no caller has to convert.
public enum Template: String, Codable, CaseIterable, Sendable {
    case blank
    case ruled
    case grid
    case dotted

    /// One millimetre in typographic points.
    public static let millimetre: CGFloat = 72.0 / 25.4

    /// Distance between rules, grid lines or dots. Zero for `.blank`.
    public var spacing: CGFloat {
        switch self {
        case .blank: return 0
        case .ruled: return 8 * Self.millimetre
        case .grid, .dotted: return 5 * Self.millimetre
        }
    }

    /// Left margin kept clear of rules, so writing does not start at the bezel.
    public var inset: CGFloat {
        switch self {
        case .blank: return 0
        case .ruled: return 48
        case .grid, .dotted: return 24
        }
    }

    public var displayName: String {
        switch self {
        case .blank: return "Blank"
        case .ruled: return "Ruled"
        case .grid: return "Grid"
        case .dotted: return "Dotted"
        }
    }
}

extension CaseIterable where Self: RawRepresentable, RawValue == String {
    /// The case a stored or wire name spells, matched case-insensitively the
    /// way the server matches it; `nil` for a name this build does not know.
    static func matching(rawValue name: String) -> Self? {
        allCases.first { $0.rawValue.caseInsensitiveCompare(name) == .orderedSame }
    }
}

// MARK: - Page objects

/// A non-ink element sitting under the ink layer. M2 models it and stores it;
/// M4 builds the views. Round-tripping it now means an M4 page written on one
/// device is not destroyed by an M2 device reading and rewriting it.
public struct PageObject: Codable, Equatable, Sendable, Identifiable {
    /// What the object is. A kind a newer build wrote decodes as `.unknown`
    /// and encodes back under its own name, so an older build can still read
    /// the page and never renames the object when it saves; nothing draws it.
    public enum Kind: Codable, Hashable, Sendable {
        case text
        case image
        case unknown(String)

        public init(rawValue: String) {
            switch rawValue {
            case "text": self = .text
            case "image": self = .image
            default: self = .unknown(rawValue)
            }
        }

        public var rawValue: String {
            switch self {
            case .text: return "text"
            case .image: return "image"
            case .unknown(let name): return name
            }
        }

        public init(from decoder: Decoder) throws {
            self.init(rawValue: try decoder.singleValueContainer().decode(String.self))
        }

        public func encode(to encoder: Encoder) throws {
            var container = encoder.singleValueContainer()
            try container.encode(rawValue)
        }
    }

    public struct Frame: Codable, Equatable, Hashable, Sendable {
        public var x: Double
        public var y: Double
        public var width: Double
        public var height: Double

        public init(x: Double, y: Double, width: Double, height: Double) {
            self.x = x
            self.y = y
            self.width = width
            self.height = height
        }

        public init(_ rect: CGRect) {
            self.init(
                x: Double(rect.origin.x), y: Double(rect.origin.y),
                width: Double(rect.size.width), height: Double(rect.size.height))
        }

        public var rect: CGRect {
            CGRect(x: x, y: y, width: width, height: height)
        }
    }

    public var id: String
    public var type: Kind
    public var frame: Frame
    /// `.text` payload.
    public var text: String?
    /// `.text` payload: body size in page points.
    public var fontSize: Double?
    /// `.image` payload: vault-relative asset path.
    public var asset: String?
    /// `.image` payload: the part of the source image that is shown, in unit
    /// coordinates of the image itself. `nil` means the whole image.
    public var crop: Frame?
    /// Fields this build does not model -- an unknown kind's payload, or one a
    /// newer build added to a known kind -- kept so the object survives a
    /// read/write cycle here intact.
    public var additionalFields: [String: JSONValue]

    public init(
        id: String = PaperID.make(), type: Kind, frame: Frame, text: String? = nil,
        fontSize: Double? = nil, asset: String? = nil, crop: Frame? = nil,
        additionalFields: [String: JSONValue] = [:]
    ) {
        self.id = id
        self.type = type
        self.frame = frame
        self.text = text
        self.fontSize = fontSize
        self.asset = asset
        self.crop = crop
        self.additionalFields = additionalFields
    }

    private static let knownKeys: Set<String> = [
        "id", "type", "frame", "text", "fontSize", "asset", "crop",
    ]

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: PaperCodingKey.self)
        func key(_ name: String) -> PaperCodingKey { PaperCodingKey(stringValue: name)! }
        id = try container.decode(String.self, forKey: key("id"))
        type = try container.decode(Kind.self, forKey: key("type"))
        frame = try container.decode(Frame.self, forKey: key("frame"))
        text = try container.decodeIfPresent(String.self, forKey: key("text"))
        fontSize = try container.decodeIfPresent(Double.self, forKey: key("fontSize"))
        asset = try container.decodeIfPresent(String.self, forKey: key("asset"))
        crop = try container.decodeIfPresent(Frame.self, forKey: key("crop"))
        var extras: [String: JSONValue] = [:]
        for name in container.allKeys where !Self.knownKeys.contains(name.stringValue) {
            if let value = try? container.decode(JSONValue.self, forKey: name) {
                extras[name.stringValue] = value
            }
        }
        additionalFields = extras
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: PaperCodingKey.self)
        func key(_ name: String) -> PaperCodingKey { PaperCodingKey(stringValue: name)! }
        for (name, value) in additionalFields where !Self.knownKeys.contains(name) {
            try container.encode(value, forKey: key(name))
        }
        try container.encode(id, forKey: key("id"))
        try container.encode(type, forKey: key("type"))
        try container.encode(frame, forKey: key("frame"))
        try container.encodeIfPresent(text, forKey: key("text"))
        try container.encodeIfPresent(fontSize, forKey: key("fontSize"))
        try container.encodeIfPresent(asset, forKey: key("asset"))
        try container.encodeIfPresent(crop, forKey: key("crop"))
    }

    /// Body size for a text object, with the default the `+` menu inserts.
    public static let defaultFontSize: Double = 17

    public var resolvedFontSize: CGFloat {
        CGFloat(fontSize ?? Self.defaultFontSize)
    }

    /// The source rectangle a crop selects out of an image of `imageSize`.
    public func cropRect(in imageSize: CGSize) -> CGRect {
        guard let crop else { return CGRect(origin: .zero, size: imageSize) }
        return CGRect(
            x: crop.x * Double(imageSize.width), y: crop.y * Double(imageSize.height),
            width: crop.width * Double(imageSize.width),
            height: crop.height * Double(imageSize.height))
    }
}

/// A page size that is not one of the three presets -- what a PDF page brings
/// with it. Additive: a page without one is exactly the page M2 wrote.
public struct SizePoints: Codable, Equatable, Sendable {
    public var width: Double
    public var height: Double

    public init(width: Double, height: Double) {
        self.width = width
        self.height = height
    }

    public init(_ size: CGSize) {
        self.init(width: Double(size.width), height: Double(size.height))
    }

    public var cgSize: CGSize { CGSize(width: width, height: height) }
}

/// The PDF background a page may be bound to (M4).
public struct PDFBinding: Codable, Equatable, Sendable {
    public var asset: String
    public var pageIndex: Int

    public init(asset: String, pageIndex: Int) {
        self.asset = asset
        self.pageIndex = pageIndex
    }
}

/// A coding key that lets Paper retain additive fields introduced by a newer
/// client.  The file tree is the source of truth, so an older client must not
/// erase a field it does not understand merely because it touched a page.
private struct PaperCodingKey: CodingKey, Hashable {
    let stringValue: String
    let intValue: Int? = nil

    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { return nil }
}

// MARK: - Page

/// One page of a notebook: exactly `page.json` from the frozen vault layout.
public struct Page: Codable, Equatable, Sendable, Identifiable {
    public var id: String
    /// Optional human-readable page title.  The package name remains the
    /// stable identity; this is presentation metadata only.
    public var title: String?
    /// Whether this page is bookmarked in the notebook.  This property was
    /// added after the original Paper schema; decoding a missing key yields
    /// `false` so existing page.json files continue to load.
    public var bookmarked: Bool
    public var template: Template
    public var size: PaperSize
    public var created: Date
    public var modified: Date
    /// Server-issued, monotonic, starts at 1. Zero means "never pushed".
    public var revision: Int
    public var renderRevision: Int
    public var textRevision: Int
    public var objects: [PageObject]
    public var pdf: PDFBinding?
    /// Set only when the page is not one of the preset sizes -- an imported PDF
    /// page carries its media box here while `size` keeps the nearest preset,
    /// so an older client still renders something sensible.
    public var customSize: SizePoints?
    /// Set on a conflict sibling: the id of the page it forked from.
    public var conflictOf: String?
    public var seq: Int
    /// Unknown top-level page fields are retained across a read/write cycle.
    /// This is intentionally not part of the wire contract; it is a local
    /// compatibility envelope for future additive metadata.
    public var additionalFields: [String: JSONValue]

    enum CodingKeys: String, CodingKey {
        case id, title, bookmarked, template, size, created, modified, revision, objects, pdf, seq
        case customSize = "size_points"
        case renderRevision = "render_revision"
        case textRevision = "text_revision"
        case conflictOf = "conflict_of"
    }

    public init(
        id: String = PaperID.make(),
        title: String? = nil,
        bookmarked: Bool = false,
        template: Template = .ruled,
        size: PaperSize = .a4,
        created: Date = Date(),
        modified: Date = Date(),
        revision: Int = 0,
        renderRevision: Int = 0,
        textRevision: Int = 0,
        objects: [PageObject] = [],
        pdf: PDFBinding? = nil,
        customSize: SizePoints? = nil,
        conflictOf: String? = nil,
        seq: Int = 0,
        additionalFields: [String: JSONValue] = [:]
    ) {
        self.id = id
        self.title = title
        self.bookmarked = bookmarked
        self.template = template
        self.size = size
        self.created = created
        self.modified = modified
        self.revision = revision
        self.renderRevision = renderRevision
        self.textRevision = textRevision
        self.objects = objects
        self.pdf = pdf
        self.customSize = customSize
        self.conflictOf = conflictOf
        self.seq = seq
        self.additionalFields = additionalFields
    }

    /// The page's real size in points: the media box of an imported PDF page,
    /// otherwise the preset.
    public var pointSize: CGSize { customSize?.cgSize ?? size.points }

    /// Legacy spelling used by some clients.  `bookmarked` is the canonical
    /// Swift property and JSON key; this alias is deliberately computed so a
    /// page can never encode two competing values.
    public var isBookmarked: Bool {
        get { bookmarked }
        set { bookmarked = newValue }
    }

    /// `size_name` is the wire's spelling of `size`: it never lives on disk,
    /// so a stale copy cannot outlast a change to `size`.
    private static let knownKeys: Set<String> = [
        "id", "title", "bookmarked", "is_bookmarked", "isBookmarked", "template", "size",
        "size_name", "size_points", "custom_size", "created", "modified", "revision",
        "render_revision", "text_revision", "objects", "pdf", "conflict_of", "seq",
    ]

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: PaperCodingKey.self)
        func decode<T: Decodable>(_ type: T.Type, _ key: String) throws -> T? {
            guard let key = PaperCodingKey(stringValue: key) else { return nil }
            return try container.decodeIfPresent(type, forKey: key)
        }

        guard let id = try decode(String.self, "id") else {
            throw DecodingError.keyNotFound(
                PaperCodingKey(stringValue: "id")!,
                .init(codingPath: decoder.codingPath, debugDescription: "page id is required"))
        }
        self.id = id
        self.title = try decode(String.self, "title")
        let bookmarked = try decode(Bool.self, "bookmarked")
        let legacyBookmarked = try decode(Bool.self, "is_bookmarked")
        let camelBookmarked = try decode(Bool.self, "isBookmarked")
        self.bookmarked = bookmarked ?? legacyBookmarked ?? camelBookmarked ?? false
        // Names, not enums: a template or size a newer build wrote falls back
        // to the default instead of making the whole page unreadable here.
        self.template =
            (try? decode(String.self, "template")).flatMap(Template.matching(rawValue:)) ?? .ruled
        self.size =
            (try? decode(String.self, "size")).flatMap(PaperSize.matching(rawValue:)) ?? .a4
        self.created = (try decode(Date.self, "created")) ?? Date(timeIntervalSince1970: 0)
        self.modified = (try decode(Date.self, "modified")) ?? self.created
        self.revision = (try decode(Int.self, "revision")) ?? 0
        self.renderRevision = (try decode(Int.self, "render_revision")) ?? 0
        self.textRevision = (try decode(Int.self, "text_revision")) ?? 0
        self.objects = (try decode([PageObject].self, "objects")) ?? []
        self.pdf = try decode(PDFBinding.self, "pdf")
        let customSize = try decode(SizePoints.self, "size_points")
        let legacyCustomSize = try decode(SizePoints.self, "custom_size")
        self.customSize = customSize ?? legacyCustomSize
        self.conflictOf = try decode(String.self, "conflict_of")
        self.seq = (try decode(Int.self, "seq")) ?? 0
        var extras: [String: JSONValue] = [:]
        for key in container.allKeys where !Self.knownKeys.contains(key.stringValue) {
            if let value = try? container.decode(JSONValue.self, forKey: key) {
                extras[key.stringValue] = value
            }
        }
        self.additionalFields = extras
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: PaperCodingKey.self)
        for (name, value) in additionalFields {
            guard !Self.knownKeys.contains(name), let key = PaperCodingKey(stringValue: name) else {
                continue
            }
            try container.encode(value, forKey: key)
        }
        try container.encode(id, forKey: PaperCodingKey(stringValue: "id")!)
        try container.encodeIfPresent(title, forKey: PaperCodingKey(stringValue: "title")!)
        try container.encode(bookmarked, forKey: PaperCodingKey(stringValue: "bookmarked")!)
        try container.encode(template, forKey: PaperCodingKey(stringValue: "template")!)
        try container.encode(size, forKey: PaperCodingKey(stringValue: "size")!)
        try container.encode(created, forKey: PaperCodingKey(stringValue: "created")!)
        try container.encode(modified, forKey: PaperCodingKey(stringValue: "modified")!)
        try container.encode(revision, forKey: PaperCodingKey(stringValue: "revision")!)
        try container.encode(renderRevision, forKey: PaperCodingKey(stringValue: "render_revision")!)
        try container.encode(textRevision, forKey: PaperCodingKey(stringValue: "text_revision")!)
        try container.encode(objects, forKey: PaperCodingKey(stringValue: "objects")!)
        try container.encodeIfPresent(pdf, forKey: PaperCodingKey(stringValue: "pdf")!)
        try container.encodeIfPresent(customSize, forKey: PaperCodingKey(stringValue: "size_points")!)
        try container.encodeIfPresent(conflictOf, forKey: PaperCodingKey(stringValue: "conflict_of")!)
        try container.encode(seq, forKey: PaperCodingKey(stringValue: "seq")!)
    }
}

// MARK: - Durable page links

/// A durable deep link into Paper. Paths and page positions are mutable, so
/// links carry the stable notebook/page ids instead. A result may provide a
/// normalized rectangle (portable across page sizes) and/or a page-space
/// rectangle (exact for the page revision that produced it).
public struct PaperLink: Codable, Equatable, Hashable, Sendable {
    public enum CoordinateSpace: String, Codable, Sendable {
        case normalized
        case page
    }

    public var notebookID: String
    public var pageID: String
    public var normalizedRect: PageObject.Frame?
    public var pageRect: PageObject.Frame?

    public init(
        notebookID: String,
        pageID: String,
        normalizedRect: PageObject.Frame? = nil,
        pageRect: PageObject.Frame? = nil
    ) {
        self.notebookID = notebookID
        self.pageID = pageID
        self.normalizedRect = normalizedRect
        self.pageRect = pageRect
    }

    /// Convenience for callers that have one rectangle and know its space.
    public init(
        notebookID: String,
        pageID: String,
        rect: PageObject.Frame?,
        coordinateSpace: CoordinateSpace = .page
    ) {
        self.init(
            notebookID: notebookID,
            pageID: pageID,
            normalizedRect: coordinateSpace == .normalized ? rect : nil,
            pageRect: coordinateSpace == .page ? rect : nil)
    }

    /// UUID vocabulary used by integrations that call these ids UUIDs. The
    /// underlying representation remains String because conflict sibling page
    /// ids are valid Paper ids but not UUIDs.
    public var notebookUUID: String { notebookID }
    public var pageUUID: String { pageID }

    /// The page-space target, converting a normalized target when necessary.
    public func pageRect(for pageSize: CGSize) -> PageObject.Frame? {
        if let pageRect { return pageRect }
        guard let normalizedRect, pageSize.width > 0, pageSize.height > 0 else { return nil }
        return PageObject.Frame(
            x: normalizedRect.x * Double(pageSize.width),
            y: normalizedRect.y * Double(pageSize.height),
            width: normalizedRect.width * Double(pageSize.width),
            height: normalizedRect.height * Double(pageSize.height))
    }

    private enum CodingKeys: String, CodingKey {
        case notebookID = "notebook_id"
        case pageID = "page_id"
        case normalizedRect = "normalized_rect"
        case pageRect = "page_rect"
        case rect
        case coordinateSpace = "coordinate_space"
        case notebookUUID = "notebook_uuid"
        case pageUUID = "page_uuid"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        notebookID =
            try container.decodeIfPresent(String.self, forKey: .notebookID)
            ?? container.decode(String.self, forKey: .notebookUUID)
        pageID =
            try container.decodeIfPresent(String.self, forKey: .pageID)
            ?? container.decode(String.self, forKey: .pageUUID)
        normalizedRect = try container.decodeIfPresent(PageObject.Frame.self, forKey: .normalizedRect)
        pageRect = try container.decodeIfPresent(PageObject.Frame.self, forKey: .pageRect)
        if normalizedRect == nil, pageRect == nil,
            let rect = try container.decodeIfPresent(PageObject.Frame.self, forKey: .rect)
        {
            if try container.decodeIfPresent(CoordinateSpace.self, forKey: .coordinateSpace) == .normalized {
                normalizedRect = rect
            } else {
                pageRect = rect
            }
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(notebookID, forKey: .notebookID)
        try container.encode(pageID, forKey: .pageID)
        try container.encodeIfPresent(normalizedRect, forKey: .normalizedRect)
        try container.encodeIfPresent(pageRect, forKey: .pageRect)
    }
}

/// Descriptive aliases keep the contract readable at call sites that are
/// specifically dealing with search regions.
public typealias PageRegionLink = PaperLink
public typealias PaperRegionLink = PaperLink

// MARK: - Notebook

/// Exactly `notebook.json`, plus the vault-relative package path the row is
/// keyed by everywhere else.
public struct Notebook: Codable, Equatable, Sendable, Identifiable {
    public var id: String
    public var title: String
    public var created: Date
    public var modified: Date
    public var template: Template
    public var paperSize: PaperSize
    /// Page id whose render is the cover, or `nil` for "last edited".
    public var cover: String?
    public var tags: [String]
    public var favorite: Bool
    public var pages: [String]
    public var seq: Int

    /// Vault-relative package path, e.g. `paper/Work/Ideas.paper`. Not written
    /// into `notebook.json` (the file's own location is the truth); carried
    /// here because it is the key in the database, the API and the change feed.
    public var path: String = ""

    enum CodingKeys: String, CodingKey {
        case id, title, created, modified, template, paperSize, cover, tags, favorite, pages, seq
    }

    public init(
        id: String = PaperID.make(),
        title: String,
        created: Date = Date(),
        modified: Date = Date(),
        template: Template = .ruled,
        paperSize: PaperSize = .a4,
        cover: String? = nil,
        tags: [String] = [],
        favorite: Bool = false,
        pages: [String] = [],
        seq: Int = 0,
        path: String
    ) {
        self.id = id
        self.title = title
        self.created = created
        self.modified = modified
        self.template = template
        self.paperSize = paperSize
        self.cover = cover
        self.tags = tags
        self.favorite = favorite
        self.pages = pages
        self.seq = seq
        self.path = path
    }

    /// Folder the notebook sits in, vault-relative and without the `paper/`
    /// root: `""` for the top level, `Work/Plans` for a nested one.
    public var folder: String {
        PaperPaths.folder(ofNotebookPath: path)
    }

    /// Whether two copies say the same thing about the notebook. `seq` and
    /// `modified` are left out: the server re-stamps both on every write,
    /// including the one that answers this device's own page create.
    public func hasSameContent(as other: Notebook) -> Bool {
        id == other.id && title == other.title && template == other.template
            && paperSize == other.paperSize && cover == other.cover && tags == other.tags
            && favorite == other.favorite && pages == other.pages
    }
}

// MARK: - Notebook view state

/// Device-local reading position. Never synced: where *this* iPad was looking
/// is not a fact about the notebook.
public struct NotebookViewState: Equatable, Sendable {
    public var lastPageID: String?
    public var zoom: CGFloat
    public var offset: CGPoint

    public init(lastPageID: String? = nil, zoom: CGFloat = 1, offset: CGPoint = .zero) {
        self.lastPageID = lastPageID
        self.zoom = zoom
        self.offset = offset
    }
}

// MARK: - Identifiers

/// Client-generated ids: UUID hex, which satisfies the frozen constraint of
/// `[A-Za-z0-9_-]` and at most 64 characters.
public enum PaperID {
    public static func make() -> String {
        UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
    }

    /// Ids arriving from disk or the network are checked, never trusted.
    public static func isValid(_ id: String) -> Bool {
        guard !id.isEmpty, id.count <= 64 else { return false }
        return id.allSatisfy { character in
            character.isASCII
                && (character.isLetter || character.isNumber || character == "_" || character == "-"
                    || character == ".")
        }
    }
}

// MARK: - JSON coding

/// One coder pair for every Paper JSON file, so dates never disagree between
/// writers.
public enum PaperJSON {
    public static let encoder: JSONEncoder = {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        encoder.dateEncodingStrategy = .custom { date, encoder in
            var container = encoder.singleValueContainer()
            try container.encode(formatter.string(from: date))
        }
        return encoder
    }()

    public static let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { decoder in
            let text = try decoder.singleValueContainer().decode(String.self)
            guard let date = formatter.date(from: text) ?? fallbackFormatter.date(from: text) else {
                throw DecodingError.dataCorrupted(
                    .init(codingPath: decoder.codingPath, debugDescription: "bad date \(text)"))
            }
            return date
        }
        return decoder
    }()

    nonisolated(unsafe) private static let formatter: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    nonisolated(unsafe) private static let fallbackFormatter: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter
    }()

    public static func encode<T: Encodable>(_ value: T) throws -> Data {
        try encoder.encode(value)
    }

    public static func decode<T: Decodable>(_ type: T.Type, from data: Data) throws -> T {
        try decoder.decode(type, from: data)
    }
}
