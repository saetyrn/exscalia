import CryptoKit
import Foundation

// MARK: - Folder record

/// A folder: a plain directory under `paper/` with its record, `folder.json`,
/// inside it. A rename, a git restore or a copy carries the record along.
///
/// ```json
/// {"id": "3dfc9f48645d394cc5a07b793b247654", "name": "Accounting", "color": "red",
///  "icon": "emoji:📒", "favorite": false, "created": "…", "modified": "…", "seq": 812}
/// ```
///
/// Every field but `id` is optional on read, and keys this build does not know
/// are kept in `additionalFields` and written back.
public struct PaperFolder: Codable, Equatable, Sendable, Identifiable {
    /// `[0-9a-f]{32}`: random for a new folder, `PaperFolderID.legacy(path:)`
    /// for a directory that predates records.
    public var id: String
    /// Verbatim; the directory is named `PaperFolderID.folderSlug(name)`.
    public var name: String
    public var color: FolderColor
    public var icon: FolderIcon?
    public var favorite: Bool
    public var created: Date
    /// Restamped by every write of `folder.json` (rename, move, colour, icon,
    /// favourite); a change to the folder's contents does not touch it.
    public var modified: Date
    /// The server's feed counter; 0 until the server writes it.
    public var seq: Int
    public var additionalFields: [String: JSONValue]
    /// Vault-relative directory, root kept: `paper/Work`. Not written into
    /// `folder.json`: the file's own location is the truth.
    public var path: String = ""

    public init(
        id: String = PaperFolderID.make(),
        name: String,
        color: FolderColor = .blue,
        icon: FolderIcon? = nil,
        favorite: Bool = false,
        created: Date = Date(),
        modified: Date = Date(),
        seq: Int = 0,
        additionalFields: [String: JSONValue] = [:],
        path: String = ""
    ) {
        self.id = id
        self.name = name
        self.color = color
        self.icon = icon
        self.favorite = favorite
        self.created = created
        self.modified = modified
        self.seq = seq
        self.additionalFields = additionalFields
        self.path = path
    }

    /// The record a directory without a `folder.json` gets on first contact,
    /// identical on the server and on every device: the legacy id of its path,
    /// the directory's own name, blue, no icon, not a favourite.
    public static func legacy(path: String, now: Date = Date()) -> PaperFolder {
        let name = path.split(separator: "/").last.map(String.init) ?? path
        return PaperFolder(
            id: PaperFolderID.legacy(path: path), name: name, created: now, modified: now,
            path: path)
    }

    /// The folder this one sits in, root kept: `paper` at the top level,
    /// `paper/Work` for `paper/Work/Accounting`, `""` for the root itself.
    public var parentPath: String {
        var parts = path.split(separator: "/").map(String.init)
        guard parts.count > 1 else { return "" }
        parts.removeLast()
        return parts.joined(separator: "/")
    }

    /// `path` and `parent` are what the wire adds to a record. They are never
    /// written into `folder.json`, and never kept as unknown keys.
    private static let knownKeys: Set<String> = [
        "id", "name", "color", "icon", "favorite", "created", "modified", "seq", "path", "parent",
    ]

    private struct Key: CodingKey {
        let stringValue: String
        let intValue: Int? = nil
        init(_ name: String) { stringValue = name }
        init?(stringValue: String) { self.stringValue = stringValue }
        init?(intValue: Int) { return nil }
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: Key.self)
        id = try container.decode(String.self, forKey: Key("id"))
        name = (try? container.decode(String.self, forKey: Key("name"))) ?? ""
        color =
            (try? container.decode(String.self, forKey: Key("color")))
            .flatMap(FolderColor.matching(rawValue:)) ?? .blue
        icon = (try? container.decode(String.self, forKey: Key("icon"))).flatMap(FolderIcon.init(wire:))
        favorite = (try? container.decode(Bool.self, forKey: Key("favorite"))) ?? false
        // The server writes microseconds, which `PaperJSON` alone refuses.
        func date(_ name: String) -> Date? {
            if let date = try? container.decode(Date.self, forKey: Key(name)) { return date }
            return (try? container.decode(String.self, forKey: Key(name))).flatMap(PaperDate.parse)
        }
        created = date("created") ?? Date(timeIntervalSince1970: 0)
        modified = date("modified") ?? created
        seq = (try? container.decode(Int.self, forKey: Key("seq"))) ?? 0
        path = (try? container.decode(String.self, forKey: Key("path"))) ?? ""
        var extras: [String: JSONValue] = [:]
        for key in container.allKeys where !Self.knownKeys.contains(key.stringValue) {
            if let value = try? container.decode(JSONValue.self, forKey: key) {
                extras[key.stringValue] = value
            }
        }
        additionalFields = extras
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: Key.self)
        for (name, value) in additionalFields where !Self.knownKeys.contains(name) {
            try container.encode(value, forKey: Key(name))
        }
        try container.encode(id, forKey: Key("id"))
        try container.encode(name, forKey: Key("name"))
        try container.encode(color, forKey: Key("color"))
        if let icon {
            try container.encode(icon.wire, forKey: Key("icon"))
        } else {
            try container.encodeNil(forKey: Key("icon"))
        }
        try container.encode(favorite, forKey: Key("favorite"))
        try container.encode(created, forKey: Key("created"))
        try container.encode(modified, forKey: Key("modified"))
        try container.encode(seq, forKey: Key("seq"))
    }
}

// MARK: - Colour and icon

/// A folder's colour. The iPad draws the `UIColor.system*` colour of the
/// same name. A name this build does not know reads as `.blue`, the default.
public enum FolderColor: String, Codable, CaseIterable, Sendable {
    case red, orange, yellow, green, mint, teal, blue, indigo, purple, pink, brown, gray

    public var displayName: String {
        rawValue.prefix(1).uppercased() + rawValue.dropFirst()
    }

    public init(from decoder: Decoder) throws {
        let name = try decoder.singleValueContainer().decode(String.self)
        self = FolderColor.matching(rawValue: name) ?? .blue
    }
}

/// What a folder shows instead of the plain folder glyph: an SF Symbol, or
/// one emoji -- the one place the app allows emoji. Spelled on disk and on the
/// wire as `symbol:<name>` or `emoji:<text>`.
public enum FolderIcon: Equatable, Hashable, Sendable {
    case symbol(String)
    case emoji(String)

    /// `nil` for anything that is not a well-formed icon, which then draws
    /// the plain folder.
    public init?(wire: String) {
        if wire.hasPrefix("symbol:") {
            let name = String(wire.dropFirst("symbol:".count))
            guard Self.isValidSymbol(name) else { return nil }
            self = .symbol(name)
        } else if wire.hasPrefix("emoji:") {
            let text = String(wire.dropFirst("emoji:".count))
            guard Self.isValidEmoji(text) else { return nil }
            self = .emoji(text)
        } else {
            return nil
        }
    }

    public var wire: String {
        switch self {
        case .symbol(let name): return "symbol:\(name)"
        case .emoji(let text): return "emoji:\(text)"
        }
    }

    /// `^[a-z0-9]+(\.[a-z0-9]+)*$`, at most 64 characters. Whether this
    /// device has the symbol is the drawing code's question, not this one's.
    public static func isValidSymbol(_ name: String) -> Bool {
        guard !name.isEmpty, name.utf8.count <= 64 else { return false }
        let parts = name.split(separator: ".", omittingEmptySubsequences: false)
        return parts.allSatisfy { part in
            !part.isEmpty
                && part.utf8.allSatisfy { (0x61...0x7A).contains($0) || (0x30...0x39).contains($0) }
        }
    }

    /// 1-16 code points, none of them whitespace, a control or ASCII. The
    /// picker further holds it to one grapheme.
    public static func isValidEmoji(_ text: String) -> Bool {
        let scalars = text.unicodeScalars
        guard (1...16).contains(scalars.count) else { return false }
        return scalars.allSatisfy { scalar in
            !scalar.isASCII && !scalar.properties.isWhitespace
                && scalar.properties.generalCategory != .control
        }
    }

    /// The 24 symbols the Icon menu offers, in order.
    public static let symbolChoices: [String] = [
        "folder", "book.closed", "books.vertical", "graduationcap", "briefcase", "doc.text",
        "pencil", "paintbrush", "lightbulb", "star", "heart", "flag", "bookmark", "calendar",
        "clock", "chart.bar", "function", "atom", "leaf", "globe", "house", "person",
        "music.note", "camera",
    ]
}

// MARK: - Identity and names

public enum PaperFolderID {
    /// A random id for a new folder: 32 lowercase hex digits.
    public static func make() -> String { PaperID.make() }

    /// The id a directory without a record gets, which the server and every
    /// device derive alike without talking:
    /// `sha256_hex("paper-folder:" + NFC(path))[0:32]`.
    public static func legacy(path: String) -> String {
        let input = "paper-folder:" + path.precomposedStringWithCanonicalMapping
        let digest = SHA256.hash(data: Data(input.utf8))
        return String(digest.map { String(format: "%02x", $0) }.joined().prefix(32))
    }

    /// Whether `id` has the shape of a folder id, `[0-9a-f]{32}`.
    public static func isValid(_ id: String) -> Bool {
        id.utf8.count == 32
            && id.utf8.allSatisfy { (0x30...0x39).contains($0) || (0x61...0x66).contains($0) }
    }

    /// The directory name for a folder called `name`: `PaperPaths.slug`, plus
    /// a trailing `_` when that ends in `.paper` or is `folder.json` (ignoring
    /// case), so a folder can never pass for a notebook package or for its
    /// parent's record. The server's `paper_folders.folder_slug` mirrors it.
    public static func folderSlug(_ name: String) -> String {
        let slug = PaperPaths.slug(name)
        // Case folding, as Python's `casefold`, not just lowercasing.
        let folded = slug.folding(options: [.caseInsensitive], locale: nil)
        if folded.hasSuffix(".paper") || folded == "folder.json" { return slug + "_" }
        return slug
    }
}
