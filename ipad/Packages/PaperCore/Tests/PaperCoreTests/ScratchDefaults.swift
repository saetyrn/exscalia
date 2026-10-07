import Foundation
import XCTest

/// A `UserDefaults` suite for one test that leaves no file behind.
///
/// `removePersistentDomain(forName:)` empties a suite, but the preferences
/// daemon still writes the emptied domain to `Library/Preferences/<name>.plist`
/// once the test process ends, whether or not the file was deleted first:
/// hundreds of them piled up in `~/Library/Preferences`. A suite named by an
/// absolute path keeps its file at that path instead, so each one lives in a
/// temporary directory that goes, with the domain, when the test ends.
struct ScratchDefaults {
    let name: String
    let defaults: UserDefaults
    let directory: URL

    init(_ label: String) throws {
        directory = FileManager.default.temporaryDirectory
            .appending(path: "\(label)-\(UUID().uuidString)", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        name = directory.appending(path: "defaults").path(percentEncoded: false)
        defaults = try XCTUnwrap(UserDefaults(suiteName: name), "could not make a defaults suite")
    }

    /// Empties the suite and removes its directory.
    func remove() {
        Self.remove(name: name, directory: directory)
    }

    static func remove(name: String, directory: URL) {
        UserDefaults().removePersistentDomain(forName: name)
        try? FileManager.default.removeItem(at: directory)
    }
}

extension XCTestCase {
    /// A scratch suite that is emptied and removed when the test ends.
    func scratchDefaults(_ label: String) throws -> UserDefaults {
        let scratch = try ScratchDefaults(label)
        let name = scratch.name
        let directory = scratch.directory
        addTeardownBlock { ScratchDefaults.remove(name: name, directory: directory) }
        return scratch.defaults
    }
}
