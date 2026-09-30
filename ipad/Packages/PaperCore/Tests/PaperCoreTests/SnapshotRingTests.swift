import Foundation
import XCTest

@testable import PaperCore

final class SnapshotRingTests: XCTestCase {
    private var root: URL!
    private var ring: SnapshotRing!
    private var database: PaperDatabase!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory
            .appending(path: "SnapshotRingTests-\(UUID().uuidString)", directoryHint: .isDirectory)
        database = try PaperDatabase()
        ring = SnapshotRing(paths: PaperPaths(root: root), database: database)
    }

    override func tearDownWithError() throws {
        if let root { try? FileManager.default.removeItem(at: root) }
    }

    func testKeepsTenVersionsPerPageAndDropsTheOldest() throws {
        let start = Date(timeIntervalSince1970: 1_000_000)
        for index in 0..<15 {
            try ring.push(
                Data("version \(index)".utf8), pageID: "p1",
                at: start.addingTimeInterval(Double(index)))
        }

        let kept = try ring.snapshots(pageID: "p1")
        XCTAssertEqual(kept.count, SnapshotRing.perPage)
        XCTAssertEqual(try Data(contentsOf: XCTUnwrap(kept.first).url), Data("version 14".utf8))
        // The files went with the rows.
        let onDisk = try FileManager.default.contentsOfDirectory(
            at: PaperPaths(root: root).snapshotDirectory("p1"), includingPropertiesForKeys: nil)
        XCTAssertEqual(onDisk.count, SnapshotRing.perPage)
    }

    func testRingsAreIndependentPerPage() throws {
        for index in 0..<12 {
            try ring.push(Data("a\(index)".utf8), pageID: "p1")
            try ring.push(Data("b\(index)".utf8), pageID: "p2")
        }
        XCTAssertEqual(try ring.snapshots(pageID: "p1").count, 10)
        XCTAssertEqual(try ring.snapshots(pageID: "p2").count, 10)
    }

    func testAnUnchangedFlushIsNotAVersion() throws {
        XCTAssertTrue(try ring.push(Data("same".utf8), pageID: "p1"))
        XCTAssertFalse(try ring.push(Data("same".utf8), pageID: "p1"))
        XCTAssertEqual(try ring.snapshots(pageID: "p1").count, 1)
    }

    func testTheStoreCapEvictsOldestAcrossPagesButNeverTheNewest() throws {
        // 40 pages x 6 MB is 240 MB of claimed bytes against a 200 MB cap. The
        // rows carry the size, so the test does not have to write 240 MB.
        let blob = Data(repeating: 0x41, count: 1024)
        var stamp = Date(timeIntervalSince1970: 1_000_000)
        for page in 0..<40 {
            for version in 0..<6 {
                stamp = stamp.addingTimeInterval(1)
                try ring.push(
                    blob + Data("p\(page)v\(version)".utf8), pageID: "page\(page)", at: stamp)
                try database.queue.write { db in
                    try db.execute(
                        sql: "UPDATE snapshots SET bytes = 1048576 WHERE bytes < 1048576")
                }
            }
        }
        // Force one more eviction pass now that every row claims 1 MB.
        stamp = stamp.addingTimeInterval(1)
        try ring.push(blob + Data("final".utf8), pageID: "page0", at: stamp)

        XCTAssertLessThanOrEqual(try ring.totalBytes(), SnapshotRing.byteCap)
        for page in 0..<40 {
            XCTAssertGreaterThanOrEqual(
                try ring.snapshots(pageID: "page\(page)").count, 1,
                "every page keeps its newest version")
        }
    }
}
