import CryptoKit
import Foundation
import UniformTypeIdentifiers

public enum AssetStoreError: Error, Equatable, Sendable {
    case tooLarge(Int)
    case unsafePath(String)
}

/// Images and imported PDFs, stored beside the pages that use them.
///
/// The name is `<safe-stem>-<sha256[:16]>.<ext>`, exactly the vault's
/// attachment convention (`safeStem` is the server's `safe_stem`, pinned by a
/// test table the backend shares), which makes storing an asset idempotent:
/// the same bytes always land on the same name, so a re-import, a duplicate
/// paste and a sync round trip all reference one file. The bytes themselves
/// are never touched -- an imported PDF is stored verbatim so annotating it
/// can never alter the original.
public struct AssetStore: Sendable {
    /// Matches the server-side cap in the frozen contract.
    public static let maxBytes = 50 * 1024 * 1024

    public let paths: PaperPaths

    public init(paths: PaperPaths) {
        self.paths = paths
    }

    // MARK: - Locations

    public func directory(forNotebook notebookPath: String) -> URL {
        paths.url(forVaultPath: notebookPath).appending(path: "assets", directoryHint: .isDirectory)
    }

    public func url(forNotebook notebookPath: String, filename: String) -> URL {
        directory(forNotebook: notebookPath).appending(path: filename, directoryHint: .notDirectory)
    }

    /// Absolute URL of a vault-relative asset path such as
    /// `paper/Work/Ideas.paper/assets/scan-0a1b2c3d4e5f6071.pdf`.
    ///
    /// The path a page stores names the notebook it was written in, so a
    /// rename, a move or a duplicate leaves it pointing at a package that is no
    /// longer there. Given the notebook the page belongs to now, a missing file
    /// is looked up by name in that notebook's `assets/`: names are content
    /// hashes, so the same name there is the same bytes.
    ///
    /// Failing both, a file whose name ends in the same `-<hash>.<ext>` is the
    /// same bytes under another stem: one named before the stem followed the
    /// server's spelling, or fetched under the server's.
    public func url(forAssetPath assetPath: String, inNotebook notebookPath: String? = nil) -> URL {
        // The last component is a file, and a URL that claims otherwise carries
        // a trailing slash that `fileExists` refuses to match.
        let components = assetPath.split(separator: "/").map(String.init)
        guard let filename = components.last else { return paths.vault }
        var url = paths.vault
        for component in components.dropLast() where component != ".." {
            url = url.appending(path: component, directoryHint: .isDirectory)
        }
        let literal = url.appending(path: filename, directoryHint: .notDirectory)
        guard filename != "..", !Self.fileExists(literal) else { return literal }
        let relative = notebookPath.map { self.url(forNotebook: $0, filename: filename) }
        if let relative, Self.fileExists(relative) { return relative }
        let directory = notebookPath.map(directory(forNotebook:)) ?? url
        return Self.sameContent(as: filename, in: directory) ?? relative ?? literal
    }

    /// A file in `directory` whose name carries the same hash and extension
    /// as `filename`, ignoring case. `nil` for a name that carries no hash.
    private static func sameContent(as filename: String, in directory: URL) -> URL? {
        guard let parts = nameParts(filename) else { return nil }
        let tail = "-\(parts.hash).\(parts.ext)".lowercased()
        let names =
            (try? FileManager.default.contentsOfDirectory(
                atPath: directory.path(percentEncoded: false))) ?? []
        // By name: a listed URL spells the container's symlinked `/var` out.
        return names.first { !$0.hasPrefix(".") && $0.lowercased().hasSuffix(tail) }
            .map { directory.appending(path: $0, directoryHint: .notDirectory) }
    }

    private static func fileExists(_ url: URL) -> Bool {
        FileManager.default.fileExists(atPath: url.path(percentEncoded: false))
    }

    public func data(forAssetPath assetPath: String, inNotebook notebookPath: String? = nil)
        -> Data?
    {
        try? Data(contentsOf: url(forAssetPath: assetPath, inNotebook: notebookPath))
    }

    public func exists(assetPath: String, inNotebook notebookPath: String? = nil) -> Bool {
        FileManager.default.fileExists(
            atPath: url(forAssetPath: assetPath, inNotebook: notebookPath)
                .path(percentEncoded: false))
    }

    // MARK: - Naming

    /// First sixteen hex characters of the SHA-256, as the vault spells it.
    public static func hash(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined().prefix(16).description
    }

    /// Longest stem the server keeps.
    static let stemLimit = 60

    /// The server's `safe_stem` (backend/app/vault/notes.py), character for
    /// character: the stem of the last path component as `pathlib` finds it,
    /// every run of characters outside `A-Za-z0-9._-` made one `-`, `-._`
    /// trimmed from both ends, cut at 60, `file` when nothing is left, lower
    /// case. The server names an upload with it, so a stem spelled any other
    /// way is a page that names a file the server calls something else. If
    /// that function changes, this one has to follow it.
    public static func safeStem(_ name: String) -> String {
        var cleaned = String.UnicodeScalarView()
        var inRun = false
        for scalar in pathlibStem(name) {
            if ("a"..."z").contains(scalar) || ("A"..."Z").contains(scalar)
                || ("0"..."9").contains(scalar) || scalar == "." || scalar == "_" || scalar == "-"
            {
                cleaned.append(scalar)
                inRun = false
            } else if !inRun {
                cleaned.append("-")
                inRun = true
            }
        }
        let trim: Set<Unicode.Scalar> = ["-", ".", "_"]
        let scalars = Array(cleaned)
        let start = scalars.firstIndex { !trim.contains($0) } ?? scalars.endIndex
        let end = (scalars.lastIndex { !trim.contains($0) }).map { $0 + 1 } ?? start
        let kept = scalars[start..<max(start, end)].prefix(stemLimit)
        let stem = String(String.UnicodeScalarView(kept))
        return (stem.isEmpty ? "file" : stem).lowercased()
    }

    /// `PurePosixPath(name).stem`, in Unicode scalars as Python counts them:
    /// the last component, less everything from its last `.` -- unless that
    /// dot is the first character or the last.
    private static func pathlibStem(_ name: String) -> [Unicode.Scalar] {
        let component =
            name.split(separator: "/").last { $0 != "." }.map { Array($0.unicodeScalars) } ?? []
        guard let dot = component.lastIndex(of: "."), dot > 0, dot < component.count - 1 else {
            return component
        }
        return Array(component[..<dot])
    }

    /// A stored name taken apart: the stem, the sixteen hex of the hash and
    /// the extension. `nil` for a name that is not `<stem>-<hash>.<ext>`.
    static func nameParts(_ filename: String) -> (stem: String, hash: String, ext: String)? {
        let name = filename.split(separator: "/").last.map(String.init) ?? filename
        guard let dot = name.lastIndex(of: "."), dot > name.startIndex else { return nil }
        let ext = String(name[name.index(after: dot)...])
        let base = name[..<dot]
        guard !ext.isEmpty, base.count > 17 else { return nil }
        let hash = base.suffix(16)
        guard base.dropLast(16).last == "-", hash.allSatisfy({ $0.isASCII && $0.isHexDigit })
        else { return nil }
        return (String(base.dropLast(17)), String(hash), ext)
    }

    /// The filename an upload of this asset is sent under: the stored name
    /// without its hash, since the server hashes the bytes itself and a
    /// hashed name would come back hashed twice.
    ///
    /// The server cuts at 60 *after* trimming, so a stem cut there can end in
    /// `-`, `.` or `_`, which its trim would take off the name it is sent;
    /// one more character keeps the cut, and the trimmed end, where ours was.
    public static func uploadFilename(forAssetPath assetPath: String) -> String {
        let filename = assetPath.split(separator: "/").last.map(String.init) ?? assetPath
        guard let parts = nameParts(filename) else { return filename }
        let stem = parts.stem.isEmpty ? "file" : parts.stem
        let padded = stem.unicodeScalars.count >= stemLimit ? stem + "x" : stem
        return "\(padded).\(parts.ext)"
    }

    /// The name the server gives this asset's bytes when they are sent under
    /// `uploadFilename(forAssetPath:)`. The stored name itself for everything
    /// named since the stem followed the server's; the lower-case spelling of
    /// an older name, which kept case and cut at 48.
    public static func serverFilename(forAssetPath assetPath: String) -> String {
        let filename = assetPath.split(separator: "/").last.map(String.init) ?? assetPath
        guard let parts = nameParts(filename) else { return filename }
        let stem = safeStem(uploadFilename(forAssetPath: filename))
        return "\(stem)-\(parts.hash.lowercased()).\(parts.ext.lowercased())"
    }

    /// What an upload says it is. The server goes by the extension alone.
    public static func contentType(forFilename filename: String) -> String {
        UTType(filenameExtension: (filename as NSString).pathExtension)?.preferredMIMEType
            ?? "application/octet-stream"
    }

    /// `<safe-stem>-<sha256[:16]>.<ext>`.
    public static func filename(for data: Data, preferredName: String) -> String {
        let stem = safeStem(preferredName)
        let rawExtension = (preferredName as NSString).pathExtension.lowercased()
        let ext = rawExtension.isEmpty
            ? "bin"
            : String(rawExtension.prefix(8).filter { $0.isASCII && ($0.isLetter || $0.isNumber) })
        return "\(stem)-\(hash(data)).\(ext.isEmpty ? "bin" : ext)"
    }

    // MARK: - Writing

    /// Stores `data` and returns its vault-relative path. Writing the same
    /// bytes twice writes once.
    @discardableResult
    public func store(_ data: Data, preferredName: String, notebookPath: String) throws -> String {
        guard PaperPaths.isSafeNotebookPath(notebookPath) else {
            throw AssetStoreError.unsafePath(notebookPath)
        }
        guard data.count <= Self.maxBytes else { throw AssetStoreError.tooLarge(data.count) }

        let name = Self.filename(for: data, preferredName: preferredName)
        let assetPath = "\(notebookPath)/assets/\(name)"
        let destination = url(forNotebook: notebookPath, filename: name)
        if FileManager.default.fileExists(atPath: destination.path(percentEncoded: false)) {
            return assetPath
        }
        try AtomicFile.write(data, to: destination)
        return assetPath
    }

    /// Every asset a notebook holds, vault-relative, sorted.
    public func assetPaths(forNotebook notebookPath: String) -> [String] {
        let contents =
            (try? FileManager.default.contentsOfDirectory(
                at: directory(forNotebook: notebookPath), includingPropertiesForKeys: nil,
                options: [.skipsHiddenFiles])) ?? []
        return contents.map { "\(notebookPath)/assets/\($0.lastPathComponent)" }.sorted()
    }
}
