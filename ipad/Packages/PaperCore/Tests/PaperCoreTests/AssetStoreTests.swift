import Foundation
import XCTest

@testable import PaperCore

final class AssetStoreTests: XCTestCase {
    private var root: URL!
    private var store: AssetStore!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory
            .appending(path: "AssetStoreTests-\(UUID().uuidString)", directoryHint: .isDirectory)
        store = AssetStore(paths: PaperPaths(root: root))
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: root)
    }

    func testNameIsTheStemPlusTheFirstSixteenHexOfTheHash() throws {
        let data = Data("hello paper".utf8)
        let name = AssetStore.filename(for: data, preferredName: "Scan 01.PNG")
        XCTAssertTrue(name.hasPrefix("scan-01-"), name)
        XCTAssertTrue(name.hasSuffix(".png"), name)
        let hash = name.dropFirst("scan-01-".count).prefix(16)
        XCTAssertEqual(hash.count, 16)
        XCTAssertTrue(hash.allSatisfy { $0.isHexDigit && !$0.isUppercase })
        XCTAssertEqual(String(hash), AssetStore.hash(data))
    }

    func testStemHoldsNoPathStructureAndNeverEndsUpEmpty() {
        XCTAssertEqual(AssetStore.safeStem("../../etc/passwd"), "passwd")
        XCTAssertEqual(AssetStore.safeStem("....."), "file")
        XCTAssertEqual(AssetStore.safeStem("a/b/c.pdf"), "c")
    }

    /// The same table as `SAFE_STEM_VECTORS` in
    /// `backend/tests/test_attachments_api.py`; change both together.
    func testStemMatchesTheVaultsSafeStem() {
        let vectors: [(String, String)] = [
            ("Scan 01.PNG", "scan-01"),
            ("../../etc/passwd", "passwd"),
            (".....", "file"),
            ("a/b/c.pdf", "c"),
            ("a/b/", "b"),
            ("x/..", "file"),
            ("", "file"),
            ("a\\b.png", "a-b"),
            (".hidden", "hidden"),
            ("__init__.py", "init"),
            ("my.photo.v2.jpg", "my.photo.v2"),
            ("photo.tar.gz", "photo.tar"),
            ("IMG_2024 (1).HEIC", "img_2024-1"),
            ("Caf\u{e9} menu.jpg", "caf-menu"),
            ("Cafe\u{301} menu.jpg", "cafe-menu"),
            ("\u{dc}bersicht Q3.pdf", "bersicht-q3"),
            ("\u{65e5}\u{672c}\u{8a9e}.pdf", "file"),
            (String(repeating: "A", count: 70) + ".png", String(repeating: "a", count: 60)),
            (String(repeating: "a", count: 59) + " b.png", String(repeating: "a", count: 59) + "-"),
        ]
        for (name, stem) in vectors {
            XCTAssertEqual(AssetStore.safeStem(name), stem, name)
        }
    }

    /// The server names an upload `safe_stem(filename)-<hash><suffix>`; every
    /// expected value here is what backend/app/vault/notes.py answers.
    func testStemIsSpelledExactlyAsTheServerSpellsIt() {
        let table: [(String, String)] = [
            ("Scan 01.PNG", "scan-01"),
            ("Report 2026 (final).pdf", "report-2026-final"),
            ("../x.jpg", "x"),
            ("....", "file"),
            ("\(String(repeating: "a", count: 70)).pdf", String(repeating: "a", count: 60)),
            ("\u{DC}n\u{EF}code.png", "n-code"),
            ("e\u{301}clair.png", "e-clair"),
            ("My  Photo!!.HEIC", "my-photo"),
            ("photo_.png", "photo"),
            ("--hi--.png", "hi"),
            ("Report.v2.pdf", "report.v2"),
            ("report..pdf", "report"),
            (".pdf", "pdf"),
            ("a.", "a"),
            ("", "file"),
            ("\u{65E5}\u{672C}\u{8A9E}.pdf", "file"),
            ("x.tar.gz", "x.tar"),
            // Cut at 60 after the trim, so the cut can end on a separator.
            ("\(String(repeating: "a", count: 59)) x.pdf", "\(String(repeating: "a", count: 59))-"),
        ]
        for (name, expected) in table {
            XCTAssertEqual(AssetStore.safeStem(name), expected, name)
            XCTAssertEqual(MockServerState.serverSafeStem(name), expected, name)
        }
    }

    /// What is uploaded under a stored name comes back under that same name.
    func testTheServerNameOfANewAssetIsItsOwnName() {
        let data = Data("bytes".utf8)
        for preferred in [
            "Scan 01.PNG", "Report 2026 (final).pdf", "photo.jpg", "Ünïcode.png",
            "\(String(repeating: "a", count: 59)) x.pdf",
            "\(String(repeating: "b", count: 59)).x.pdf", String(repeating: "c", count: 90) + ".pdf",
        ] {
            let name = AssetStore.filename(for: data, preferredName: preferred)
            let path = "paper/A.paper/assets/\(name)"
            XCTAssertEqual(AssetStore.serverFilename(forAssetPath: path), name, preferred)
            // Sent without the hash, so the server does not hash it twice.
            let upload = AssetStore.uploadFilename(forAssetPath: path)
            XCTAssertFalse(upload.contains(AssetStore.hash(data)), upload)
            let suffix = "." + (upload as NSString).pathExtension
            XCTAssertEqual(
                "\(MockServerState.serverSafeStem(upload))-\(AssetStore.hash(data))\(suffix)", name,
                preferred)
        }
    }

    /// Names stored before the stems matched kept case, and the server's copy
    /// of their bytes is the lower-case spelling.
    func testTheServerNameOfALegacyAssetIsTheLowerCaseSpelling() {
        let hash = "0a1b2c3d4e5f6071"
        XCTAssertEqual(
            AssetStore.serverFilename(forAssetPath: "paper/A.paper/assets/Scan-01-\(hash).png"),
            "scan-01-\(hash).png")
        XCTAssertEqual(
            AssetStore.uploadFilename(forAssetPath: "paper/A.paper/assets/Scan-01-\(hash).png"),
            "Scan-01.png")
        XCTAssertEqual(
            AssetStore.serverFilename(forAssetPath: "_Draft_-\(hash).jpg"), "draft-\(hash).jpg")
        // Not a stored name at all: nothing to respell.
        XCTAssertEqual(AssetStore.serverFilename(forAssetPath: "a/notes.pdf"), "notes.pdf")
    }

    /// The same hash and extension under another stem is the same bytes.
    func testAFileWithTheSameHashUnderAnotherStemResolves() throws {
        let data = Data("scan".utf8)
        let stored = try store.store(
            data, preferredName: "photo.png", notebookPath: "paper/B.paper")
        let hash = AssetStore.hash(data)
        let legacy = "paper/A.paper/assets/IMG-0001-\(hash).png"

        XCTAssertTrue(store.exists(assetPath: legacy, inNotebook: "paper/B.paper"))
        XCTAssertEqual(store.data(forAssetPath: legacy, inNotebook: "paper/B.paper"), data)
        XCTAssertEqual(
            store.url(forAssetPath: legacy, inNotebook: "paper/B.paper"),
            store.url(forAssetPath: stored))
        // Without a notebook, the directory the path names is the one searched.
        XCTAssertTrue(store.exists(assetPath: "paper/B.paper/assets/IMG-0001-\(hash).png"))
        // A different hash is a different file.
        XCTAssertFalse(
            store.exists(
                assetPath: "paper/A.paper/assets/photo-0000000000000000.png",
                inNotebook: "paper/B.paper"))
        XCTAssertFalse(store.exists(assetPath: legacy, inNotebook: "paper/C.paper"))
    }

    func testStoringTheSameBytesTwiceStoresOneFile() throws {
        let data = Data(repeating: 7, count: 4096)
        let first = try store.store(data, preferredName: "photo.jpg", notebookPath: "paper/A.paper")
        let second = try store.store(
            data, preferredName: "photo.jpg", notebookPath: "paper/A.paper")
        XCTAssertEqual(first, second)
        XCTAssertEqual(store.assetPaths(forNotebook: "paper/A.paper"), [first])
        XCTAssertEqual(store.data(forAssetPath: first), data)
    }

    func testDifferentBytesUnderOneNameGetDifferentFiles() throws {
        let first = try store.store(
            Data("one".utf8), preferredName: "note.txt", notebookPath: "paper/A.paper")
        let second = try store.store(
            Data("two".utf8), preferredName: "note.txt", notebookPath: "paper/A.paper")
        XCTAssertNotEqual(first, second)
        XCTAssertEqual(store.assetPaths(forNotebook: "paper/A.paper").count, 2)
    }

    func testAssetPathIsVaultRelativeAndResolvesBackToTheFile() throws {
        let path = try store.store(
            Data("x".utf8), preferredName: "a.png", notebookPath: "paper/Work/Ideas.paper")
        XCTAssertTrue(path.hasPrefix("paper/Work/Ideas.paper/assets/"), path)
        XCTAssertTrue(store.exists(assetPath: path))
        XCTAssertTrue(
            FileManager.default.fileExists(
                atPath: store.url(forAssetPath: path).path(percentEncoded: false)))
    }

    /// A page keeps the path it was given; the package moves under it on a
    /// rename. The notebook the page lives in now finds the file by name.
    func testAReferenceFromBeforeARenameResolvesInTheNotebookItIsInNow() throws {
        let data = Data("scan".utf8)
        let path = try store.store(data, preferredName: "scan.pdf", notebookPath: "paper/A.paper")
        // While the file is where the path says, the path wins.
        XCTAssertEqual(
            store.url(forAssetPath: path, inNotebook: "paper/B.paper"), store.url(forAssetPath: path))

        let paths = PaperPaths(root: root)
        try FileManager.default.moveItem(
            at: paths.url(forVaultPath: "paper/A.paper"), to: paths.url(forVaultPath: "paper/B.paper"))

        XCTAssertFalse(store.exists(assetPath: path))
        XCTAssertNil(store.data(forAssetPath: path))
        XCTAssertTrue(store.exists(assetPath: path, inNotebook: "paper/B.paper"))
        XCTAssertEqual(store.data(forAssetPath: path, inNotebook: "paper/B.paper"), data)
        XCTAssertEqual(
            store.url(forAssetPath: path, inNotebook: "paper/B.paper"),
            store.url(forNotebook: "paper/B.paper", filename: (path as NSString).lastPathComponent))

        // Another notebook does not have it, and does not pretend to.
        XCTAssertFalse(store.exists(assetPath: path, inNotebook: "paper/C.paper"))
        XCTAssertNil(store.data(forAssetPath: path, inNotebook: "paper/C.paper"))
    }

    func testAPathThatCouldClimbOutIsRefused() {
        XCTAssertThrowsError(
            try store.store(Data("x".utf8), preferredName: "a.png", notebookPath: "paper/../A.paper")
        )
    }

    func testAnAssetOverTheCapIsRefused() {
        let oversized = Data(count: AssetStore.maxBytes + 1)
        XCTAssertThrowsError(
            try store.store(oversized, preferredName: "big.bin", notebookPath: "paper/A.paper")
        ) { error in
            XCTAssertEqual(error as? AssetStoreError, .tooLarge(oversized.count))
        }
    }
}
