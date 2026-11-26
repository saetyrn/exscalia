import Foundation

/// The one way Paper writes a file.
///
/// Ink is the source of truth and there is no server copy until M3, so a
/// half-written `drawing.pkdrawing` is lost work. Every write therefore goes to
/// a sibling temporary file, is flushed to the platter with `fsync`, and only
/// then replaces the destination.
public enum AtomicFile {
    public static func write(
        _ data: Data,
        to url: URL,
        protection: Bool = true,
        fileManager: FileManager = .default
    ) throws {
        let directory = url.deletingLastPathComponent()
        try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)

        let temporary = directory.appending(
            path: ".\(url.lastPathComponent).\(UUID().uuidString).tmp",
            directoryHint: .notDirectory)

        let handle = try openForWriting(at: temporary, fileManager: fileManager)
        var writeError: Error?
        do {
            try handle.write(contentsOf: data)
            // fsync, not just close: close alone leaves the bytes in the page
            // cache, which a kernel panic or a battery pull discards.
            try handle.synchronize()
        } catch {
            writeError = error
        }
        try? handle.close()
        if let writeError {
            try? fileManager.removeItem(at: temporary)
            throw writeError
        }

        do {
            if fileManager.fileExists(atPath: url.path(percentEncoded: false)) {
                _ = try fileManager.replaceItemAt(url, withItemAt: temporary)
            } else {
                try fileManager.moveItem(at: temporary, to: url)
            }
        } catch {
            try? fileManager.removeItem(at: temporary)
            throw error
        }

        if protection {
            applyProtection(to: url)
        }
    }

    public static func writeJSON<T: Encodable>(_ value: T, to url: URL) throws {
        try write(PaperJSON.encode(value), to: url)
    }

    public static func readJSON<T: Decodable>(_ type: T.Type, at url: URL) throws -> T {
        try PaperJSON.decode(type, from: Data(contentsOf: url))
    }

    private static func openForWriting(at url: URL, fileManager: FileManager) throws -> FileHandle {
        var attributes: [FileAttributeKey: Any] = [:]
        #if os(iOS)
            attributes[.protectionKey] = FileProtectionType.completeUntilFirstUserAuthentication
        #endif
        guard
            fileManager.createFile(
                atPath: url.path(percentEncoded: false), contents: nil, attributes: attributes)
        else {
            throw CocoaError(.fileWriteUnknown)
        }
        return try FileHandle(forWritingTo: url)
    }

    /// `replaceItemAt` can hand back the *old* file's attributes, so protection
    /// is reapplied to the final name rather than assumed.
    private static func applyProtection(to url: URL) {
        #if os(iOS)
            try? FileManager.default.setAttributes(
                [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication],
                ofItemAtPath: url.path(percentEncoded: false))
        #endif
    }
}
