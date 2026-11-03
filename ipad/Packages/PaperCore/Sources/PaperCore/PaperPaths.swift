import Foundation

/// Where everything lives on the device, mirroring the frozen vault layout so
/// a directory copied off the iPad is a directory the server understands.
///
/// ```
/// Application Support/Paper/<sha1(origin)>/
///   vault/paper/<Folder>/…/<Title>.paper/
///     notebook.json
///     pages/<pageId>/page.json
///     pages/<pageId>/drawing.pkdrawing
///     pages/<pageId>/render.png
///     assets/…
///   paper.sqlite
///   snapshots/<pageId>/<n>.pkdrawing
///   .trash/<notebookId>/…
/// ```
public struct PaperPaths: Sendable {
    /// `Application Support/Paper/<sha1(origin)>`.
    public let root: URL

    public init(root: URL) {
        self.root = root
    }

    public var vault: URL { root.appending(path: "vault", directoryHint: .isDirectory) }
    public var paper: URL { vault.appending(path: "paper", directoryHint: .isDirectory) }
    public var database: URL { root.appending(path: "paper.sqlite", directoryHint: .notDirectory) }
    public var snapshots: URL { root.appending(path: "snapshots", directoryHint: .isDirectory) }
    public var trash: URL { root.appending(path: ".trash", directoryHint: .isDirectory) }

    /// Absolute URL of a vault-relative path such as `paper/Work/Ideas.paper`.
    public func url(forVaultPath path: String) -> URL {
        var url = vault
        for component in path.split(separator: "/") {
            url = url.appending(path: String(component), directoryHint: .isDirectory)
        }
        return url
    }

    public func notebookJSON(_ notebookPath: String) -> URL {
        url(forVaultPath: notebookPath)
            .appending(path: "notebook.json", directoryHint: .notDirectory)
    }

    public func pagesDirectory(_ notebookPath: String) -> URL {
        url(forVaultPath: notebookPath).appending(path: "pages", directoryHint: .isDirectory)
    }

    public func pageDirectory(_ notebookPath: String, _ pageID: String) -> URL {
        pagesDirectory(notebookPath).appending(path: pageID, directoryHint: .isDirectory)
    }

    public func pageJSON(_ notebookPath: String, _ pageID: String) -> URL {
        pageDirectory(notebookPath, pageID)
            .appending(path: "page.json", directoryHint: .notDirectory)
    }

    public func drawing(_ notebookPath: String, _ pageID: String) -> URL {
        pageDirectory(notebookPath, pageID)
            .appending(path: "drawing.pkdrawing", directoryHint: .notDirectory)
    }

    public func render(_ notebookPath: String, _ pageID: String) -> URL {
        pageDirectory(notebookPath, pageID)
            .appending(path: "render.png", directoryHint: .notDirectory)
    }

    /// The derived transcript, written by `HandwritingRecognizer` and sent to
    /// the server as the `text` part of a page PUT.
    public func text(_ notebookPath: String, _ pageID: String) -> URL {
        pageDirectory(notebookPath, pageID)
            .appending(path: "text.json", directoryHint: .notDirectory)
    }

    public func snapshotDirectory(_ pageID: String) -> URL {
        snapshots.appending(path: pageID, directoryHint: .isDirectory)
    }

    // MARK: - Naming

    /// Characters no filesystem in the chain (APFS, ext4, NTFS via a Windows
    /// checkout) will accept, plus the ones git dislikes.
    private static let forbidden = CharacterSet(charactersIn: "/\\:*?\"<>|\u{0}")

    /// Windows-safe directory stem for a notebook title. The verbatim title
    /// lives in `notebook.json`; this is only how the directory is spelled.
    ///
    /// The server's `media_slug` implements the same spec, and
    /// `backend/tests/fixtures/slug_vectors.json` is the contract both are
    /// tested against: where they disagree, a notebook created here is renamed
    /// on its first push. Everything counts Unicode scalars, not Characters,
    /// because that is what Python counts.
    public static func slug(_ title: String) -> String {
        // NFC; every forbidden character, control and run of whitespace
        // (White_Space, so U+2028 and U+00A0 too) becomes one space, trimmed.
        var scalars: [Unicode.Scalar] = []
        var gap = false
        for scalar in title.precomposedStringWithCanonicalMapping.unicodeScalars {
            if forbidden.contains(scalar) || scalar.value < 0x20
                || scalar.properties.isWhitespace
            {
                gap = true
                continue
            }
            if gap, !scalars.isEmpty { scalars.append(" ") }
            gap = false
            scalars.append(scalar)
        }
        // At most 80 scalars and 200 UTF-8 bytes (NAME_MAX headroom for the
        // `.paper` suffix and " N" numbering), and only then strip the
        // trailing dots and spaces Windows refuses.
        if scalars.count > maxSlugScalars {
            scalars.removeLast(scalars.count - maxSlugScalars)
        }
        var bytes = scalars.reduce(0) { $0 + UTF8.width($1) }
        while bytes > maxSlugBytes, let last = scalars.popLast() { bytes -= UTF8.width(last) }
        while let last = scalars.last, last == "." || last == " " { scalars.removeLast() }
        var text = String(String.UnicodeScalarView(scalars))
        if text.isEmpty { text = "Untitled" }
        if reservedWindowsNames.contains(text.uppercased()) { text = "_" + text }
        return text
    }

    private static let maxSlugScalars = 80
    private static let maxSlugBytes = 200

    private static let reservedWindowsNames: Set<String> = [
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    ]

    /// `paper/Work/Ideas.paper` for folder `Work` and title `Ideas`.
    public static func notebookPath(folder: String, title: String) -> String {
        var components = ["paper"]
        for part in folder.split(separator: "/") {
            components.append(slug(String(part)))
        }
        components.append(slug(title) + ".paper")
        return components.joined(separator: "/")
    }

    /// The folder part of `paper/Work/Plans/Ideas.paper` -- `Work/Plans`.
    public static func folder(ofNotebookPath path: String) -> String {
        var parts = path.split(separator: "/").map(String.init)
        guard !parts.isEmpty else { return "" }
        parts.removeLast()  // the .paper package
        if parts.first == "paper" { parts.removeFirst() }
        return parts.joined(separator: "/")
    }

    /// Rejects anything that could climb out of the store.
    public static func isSafeNotebookPath(_ path: String) -> Bool {
        guard path.hasPrefix("paper/"), path.hasSuffix(".paper") else { return false }
        let parts = path.split(separator: "/", omittingEmptySubsequences: false)
        guard parts.count >= 2 else { return false }
        for part in parts {
            if part.isEmpty || part == "." || part == ".." { return false }
            if part.unicodeScalars.contains(where: { forbidden.contains($0) }) { return false }
        }
        return true
    }
}
