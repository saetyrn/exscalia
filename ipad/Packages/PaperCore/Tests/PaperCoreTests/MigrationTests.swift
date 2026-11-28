import CryptoKit
import Foundation
import GRDB
import XCTest

@testable import PaperCore

/// The first launch of the rebuilt app over the container the shipped build
/// left on the owner's iPad. `V1Container` writes that container the way the
/// shipped build wrote it; `PaperMigrations.run` brings it forward; the sync
/// tests then show what reaches the server afterwards, from a server with
/// folder records, from one without them, and after days offline.
final class MigrationTests: XCTestCase {
    private var made: [V1Container] = []

    override func setUpWithError() throws {
        try super.setUpWithError()
        MockPaperServer.state.reset()
        MigrationServer.config.reset()
    }

    override func tearDownWithError() throws {
        MigrationServer.config.reset()
        MockPaperServer.state.reset()
        for container in made { container.remove() }
        made = []
        try super.tearDownWithError()
    }

    private func container() throws -> V1Container {
        let container = try V1Container.make()
        made.append(container)
        return container
    }

    private typealias C = V1Container

    // MARK: - The run

    func testAV1ContainerIsBroughtForwardInTheContractsOrder() async throws {
        let container = try container()
        let before = try container.state()
        // Three manifests, four pages with their ink, two renders, a
        // transcript, a snapshot and a trashed page.
        XCTAssertEqual(before.files.count, 17)
        XCTAssertEqual(before.ops.count, 5)
        let store = try container.openStore()
        // Opening the store applied `v4-documents` and `v5-folders`; the
        // queue is as the shipped build left it.
        XCTAssertEqual(try container.ops(store), before.ops)

        var steps: [PaperMigrations.Step] = []
        let outcome = try await PaperMigrations.run(
            defaults: container.defaults, store: store, engine: nil, afterStep: { steps.append($0) })

        XCTAssertEqual(outcome, .migrated)
        XCTAssertEqual(
            steps,
            [
                .toolSettings, .retiredSettings, .recognitionLanguages, .changeCursor, .reconcile,
                .penWidths, .otherBrushWidths,
            ])
        let defaults = container.defaults
        XCTAssertEqual(defaults.integer(forKey: PaperMigrations.versionKey), 3)

        // 1. The capsule's tools, carried over, and its keys gone.
        let tools = try XCTUnwrap(ToolSettingsStore.decode(defaults.object(forKey: ToolSettingsStore.key)))
        XCTAssertEqual(tools.active, .highlighter)
        XCTAssertEqual(tools.lastWritingTool, .highlighter)
        XCTAssertEqual(tools.penStyle, .brush)
        XCTAssertEqual(tools.slots[.pen]?.widths, [2, 4, 7])
        XCTAssertEqual(tools.slots[.pen]?.color, "#1f6feb", "the old pen colour, index 2")
        XCTAssertEqual(tools.slots[.highlighter]?.widths, [9, 18, 31.5])
        XCTAssertEqual(tools.slots[.highlighter]?.color, "#a8e6a1")
        XCTAssertEqual(tools.slots[.eraser]?.widths, [15, 30, 52.5])
        XCTAssertEqual(tools.eraserMode, .stroke)
        XCTAssertEqual(tools.dock, .top)
        for key in C.capsuleKeys { XCTAssertNil(defaults.object(forKey: key), key) }
        XCTAssertEqual(defaults.string(forKey: "paper.tool"), "highlighter")
        // 2. The two retired settings, and nothing else of the app's.
        XCTAssertNil(defaults.object(forKey: "paper.drawWithFinger"))
        XCTAssertNil(defaults.object(forKey: "paper.useSystemToolPicker"))
        XCTAssertEqual(defaults.string(forKey: ServerSettings.Key.serverURL), "https://paper.test")
        XCTAssertEqual(defaults.string(forKey: ServerSettings.Key.clientID), C.clientID)
        // 3. The languages, as recognition reads them.
        XCTAssertEqual(
            defaults.stringArray(forKey: ServerSettings.Key.recognitionLanguages), ["en-US", "de-DE"])

        // 4. The cursor, and nothing else the engine keeps.
        var syncState = before.syncState
        syncState["changes.seq"] = "0"
        XCTAssertEqual(try container.syncState(store), syncState)

        // 5. Every folder has its record, with the id the server derives.
        let folders = try store.folderRecords()
        XCTAssertEqual(folders.map(\.path), C.folders)
        XCTAssertEqual(
            folders.map(\.id), C.folders.map(PaperFolderID.legacy(path:)))
        XCTAssertEqual(try store.folder(path: "paper/Work")?.id, "9e7201dacb87ef0d43fb22798ccf4af6")
        XCTAssertEqual(
            try store.folder(path: "paper/Work/Accounting")?.id, "3dfc9f48645d394cc5a07b793b247654")
        XCTAssertEqual(folders.map(\.name), ["Empty", "Work", "Accounting"])
        XCTAssertTrue(folders.allSatisfy { $0.color == .blue && $0.icon == nil && $0.seq == 0 })
        for path in C.folders { XCTAssertTrue(container.hasFolderJSON(path), path) }
        let notebooks = try store.notebooks()
        XCTAssertEqual(Set(notebooks.map(\.path)), Set(C.notebooks))
        XCTAssertTrue(notebooks.allSatisfy { $0.kind == .notebook })
        XCTAssertEqual(try store.notebook(path: C.ledgerPath)?.tags, ["tax"])
        let (contents, documents) = try store.contents(ofFolder: "paper/Work")
        XCTAssertEqual(contents.map(\.path), ["paper/Work/Accounting"])
        XCTAssertEqual(documents.map(\.path), [C.ideasPath])

        // Not one unpushed op dropped, rewritten or moved; not one byte of
        // ink, render, transcript, manifest or snapshot changed.
        let after = try container.state(store)
        XCTAssertEqual(after.ops, before.ops)
        XCTAssertEqual(after.files, before.files)
        XCTAssertEqual(after.recordedFolders, C.folders)
    }

    func testASecondRunDoesNothing() async throws {
        let container = try container()
        let store = try container.openStore()
        try await PaperMigrations.run(defaults: container.defaults, store: store, engine: nil)

        // What a week of use would leave: a newer cursor, and a value of the
        // kind the first run converted.
        try store.setSyncValue("77", for: "changes.seq")
        container.defaults.set("fr-FR", forKey: ServerSettings.Key.recognitionLanguages)
        let before = try container.state(store)

        var steps: [PaperMigrations.Step] = []
        let outcome = try await PaperMigrations.run(
            defaults: container.defaults, store: store, engine: nil, afterStep: { steps.append($0) })

        XCTAssertEqual(outcome, .current)
        XCTAssertEqual(steps, [])
        XCTAssertEqual(try container.state(store), before)
        XCTAssertEqual(store.syncValue("changes.seq"), "77")
    }

    /// An install that finished migration 1 (the first rebuilt build) runs
    /// only migration 2: pen slots still at that build's factory widths take
    /// the calibrated ones, and slots the owner changed are left alone.
    func testAnInstallAtVersionOneGetsTheCalibratedPenWidthsUnlessItChangedThem() async throws {
        let container = try container()
        let store = try container.openStore()
        let defaults = container.defaults
        var tools = ToolSettings.factory
        tools.slots[.pen]?.widths = PaperMigrations.uncalibratedPenWidths
        tools.slots[.drawShape]?.widths = PaperMigrations.uncalibratedPenWidths
        tools.slots[.shape]?.widths = [1, 2, 6]
        tools.slots[.pen]?.colors = ["#ff0000", "#1f6feb", "#c0392b"]
        ToolSettingsStore.save(tools, defaults)
        defaults.set(1, forKey: PaperMigrations.versionKey)
        try store.setSyncValue("77", for: "changes.seq")

        var steps: [PaperMigrations.Step] = []
        let outcome = try await PaperMigrations.run(
            defaults: defaults, store: store, engine: nil, afterStep: { steps.append($0) })

        XCTAssertEqual(outcome, .migrated)
        XCTAssertEqual(steps, [.penWidths, .otherBrushWidths])
        XCTAssertEqual(defaults.integer(forKey: PaperMigrations.versionKey), 3)
        let after = try XCTUnwrap(ToolSettingsStore.decode(defaults.object(forKey: ToolSettingsStore.key)))
        XCTAssertEqual(after.slots[.pen]?.widths, [2.5, 5, 8.5])
        XCTAssertEqual(after.slots[.drawShape]?.widths, [2.5, 5, 8.5])
        XCTAssertEqual(after.slots[.shape]?.widths, [1, 2, 6], "a slot the owner changed is left alone")
        XCTAssertEqual(after.slots[.pen]?.colors, ["#ff0000", "#1f6feb", "#c0392b"], "colours are untouched")
        XCTAssertEqual(store.syncValue("changes.seq"), "77", "migration 1 does not run again")

        let again = try await PaperMigrations.run(defaults: defaults, store: store, engine: nil)
        XCTAssertEqual(again, .current)
    }

    /// An install at version 2 (the build with the calibrated pen) runs only
    /// migration 3: pencil and highlighter slots still at the old factory
    /// widths take the scaled ones.
    func testAnInstallAtVersionTwoGetsTheScaledPencilAndHighlighterWidths() async throws {
        let container = try container()
        let store = try container.openStore()
        let defaults = container.defaults
        var tools = ToolSettings.factory
        tools.slots[.pencil]?.widths = [1.5, 2.5, 4]
        tools.slots[.highlighter]?.widths = [10, 16, 24]
        tools.slots[.pen]?.widths = [1, 2, 3.5]
        ToolSettingsStore.save(tools, defaults)
        defaults.set(2, forKey: PaperMigrations.versionKey)

        var steps: [PaperMigrations.Step] = []
        let outcome = try await PaperMigrations.run(
            defaults: defaults, store: store, engine: nil, afterStep: { steps.append($0) })

        XCTAssertEqual(outcome, .migrated)
        XCTAssertEqual(steps, [.otherBrushWidths])
        XCTAssertEqual(defaults.integer(forKey: PaperMigrations.versionKey), 3)
        let after = try XCTUnwrap(ToolSettingsStore.decode(defaults.object(forKey: ToolSettingsStore.key)))
        XCTAssertEqual(after.slots[.pencil]?.widths, [4, 6, 10])
        XCTAssertEqual(after.slots[.highlighter]?.widths, [14, 22, 32])
        XCTAssertEqual(after.slots[.pen]?.widths, [1, 2, 3.5], "migration 2 does not run again")
    }

    func testAFreshInstallEndsAtTheCurrentVersionWithNothingChanged() async throws {
        let container = try V1Container.empty()
        made.append(container)
        let store = try container.openStore()

        let outcome = try await PaperMigrations.run(
            defaults: container.defaults, store: store, engine: nil)

        XCTAssertEqual(outcome, .migrated)
        XCTAssertEqual(container.defaults.integer(forKey: PaperMigrations.versionKey), 3)
        // The factory tools any first launch saves, and the version; nothing else.
        XCTAssertEqual(
            Set(container.domain.keys),
            [ToolSettingsStore.key, ToolSettingsStore.activeKey, PaperMigrations.versionKey])
        XCTAssertEqual(ToolSettingsStore.decode(container.defaults.object(forKey: ToolSettingsStore.key)), .factory)
        XCTAssertEqual(try container.syncState(store), [:], "no cursor to reset")
        XCTAssertEqual(try store.folderRecords(), [])
        XCTAssertEqual(try store.notebooks(), [])
        XCTAssertEqual(try store.journal.pendingCount(), 0)
        XCTAssertEqual(
            try FileManager.default.contentsOfDirectory(atPath: store.paths.paper.path(percentEncoded: false)),
            [])
    }

    /// A launch killed after any step: the version is not recorded, nothing
    /// that matters has moved, and the next launch ends exactly where an
    /// uninterrupted run does.
    func testALaunchKilledAfterAnyStepIsFinishedByTheNext() async throws {
        let reference = try container()
        let referenceStore = try reference.openStore()
        try await PaperMigrations.run(defaults: reference.defaults, store: referenceStore, engine: nil)
        let finished = try reference.state(referenceStore)

        struct Killed: Error {}
        for killed in PaperMigrations.Step.allCases {
            let container = try container()
            let before = try container.state()
            var store: PaperStore? = try container.openStore()
            do {
                try await PaperMigrations.run(
                    defaults: container.defaults, store: try XCTUnwrap(store), engine: nil,
                    afterStep: { if $0 == killed { throw Killed() } })
                XCTFail("ran past \(killed)")
            } catch is Killed {}

            let defaults = container.defaults
            let context = "killed after \(killed)"
            XCTAssertNil(defaults.object(forKey: PaperMigrations.versionKey), context)
            XCTAssertNotNil(defaults.object(forKey: ToolSettingsStore.key), context)
            XCTAssertEqual(
                defaults.object(forKey: "paper.drawWithFinger") == nil,
                killed.rawValue >= PaperMigrations.Step.retiredSettings.rawValue, context)
            XCTAssertEqual(
                defaults.object(forKey: ServerSettings.Key.recognitionLanguages) is [String],
                killed.rawValue >= PaperMigrations.Step.recognitionLanguages.rawValue, context)
            XCTAssertEqual(
                try XCTUnwrap(store).syncValue("changes.seq"),
                killed.rawValue >= PaperMigrations.Step.changeCursor.rawValue ? "0" : "\(C.cursor)",
                context)
            XCTAssertEqual(
                container.hasFolderJSON("paper/Work"),
                killed.rawValue >= PaperMigrations.Step.reconcile.rawValue, context)
            let interrupted = try container.state(try XCTUnwrap(store))
            XCTAssertEqual(interrupted.ops, before.ops, context)
            XCTAssertEqual(interrupted.files, before.files, context)

            // The next launch: a new process, a new store over the same files.
            store = nil
            let relaunched = try container.openStore()
            let outcome = try await PaperMigrations.run(
                defaults: defaults, store: relaunched, engine: nil)
            XCTAssertEqual(outcome, .migrated, context)
            XCTAssertEqual(try container.state(relaunched), finished, context)
        }
    }

    func testOnlyAStringOfLanguagesIsConverted() async throws {
        XCTAssertEqual(PaperMigrations.languages(joined: " en-US , de-DE,,fr-FR "), ["en-US", "de-DE", "fr-FR"])
        XCTAssertEqual(PaperMigrations.languages(joined: ""), [])

        let container = try V1Container.empty()
        made.append(container)
        let store = try container.openStore()
        let key = ServerSettings.Key.recognitionLanguages
        container.defaults.set(["it-IT"], forKey: key)
        try await PaperMigrations.run(defaults: container.defaults, store: store, engine: nil)
        XCTAssertEqual(container.defaults.stringArray(forKey: key), ["it-IT"])
    }

    /// The reconcile step counts only once every directory it visits has a
    /// record; hidden directories and packages are not folders.
    func testTheReconcileStepChecksEveryFolderHasItsRecord() async throws {
        let container = try V1Container.empty()
        made.append(container)
        let store = try container.openStore()
        for path in ["paper/A/B", "paper/.cache", "paper/Notes.paper/pages"] {
            try FileManager.default.createDirectory(
                at: store.paths.url(forVaultPath: path), withIntermediateDirectories: true)
        }
        XCTAssertEqual(try PaperMigrations.foldersWithoutRecords(in: store), ["paper/A", "paper/A/B"])
        try await PaperMigrations.perform(
            .reconcile, defaults: container.defaults, store: store, engine: nil)
        XCTAssertEqual(try PaperMigrations.foldersWithoutRecords(in: store), [])
    }

    // MARK: - What reaches the server afterwards

    /// A server with folder records: the queue goes first, as it was, then
    /// one full pull that downloads no page and writes no folder.
    func testAfterwardsTheQueueGoesFirstAndTheFullPullFetchesNoPage() async throws {
        seedServer(bootPass: true)
        let container = try container()
        let store = try container.openStore()
        let engine = try engine(for: store)

        try await PaperMigrations.run(defaults: container.defaults, store: store, engine: engine)
        await engine.syncNow()

        let requests = MigrationServer.config.requests
        let pulls = requests.indices.filter { requests[$0].path == "/api/paper/changes" }
        XCTAssertEqual(
            pulls.map { requests[$0].since }, [C.cursor, 0],
            "what moved since the last pull, then a full pull")
        let pull = try XCTUnwrap(pulls.first)
        let full = try XCTUnwrap(pulls.last)
        XCTAssertEqual(
            requests[..<pull].filter { $0.method != "GET" }.map(\.label), C.queuedRequests,
            "the shipped build's queue, in its order")
        XCTAssertEqual(
            requests[(full + 1)...].map(\.label),
            ["GET /api/paper"] + C.serverNotebooks.map { "GET /api/paper/notebooks/\($0)" })
        XCTAssertEqual(requests.filter(\.isDownload).map(\.label), [], "nothing downloaded again")
        XCTAssertEqual(requests.filter { $0.path.hasPrefix("/api/paper/folders") }.map(\.label), [])
        try assertDrained(store)
        XCTAssertGreaterThan(Int(store.syncValue("changes.seq") ?? "") ?? 0, C.cursor)

        // The server's records name the same folders by the same ids.
        let folders = try store.folderRecords()
        XCTAssertEqual(folders.map(\.id), C.folders.map(PaperFolderID.legacy(path:)))
        XCTAssertTrue(folders.allSatisfy { $0.seq > C.cursor })
        XCTAssertEqual(MockPaperServer.state.withState { $0[C.ledgerPath]?.favorite }, true)
        XCTAssertNotNil(MockPaperServer.state.withState { $0["paper/Inbox.paper"] })
    }

    /// A server from before folder records: nothing is lost and nothing
    /// parks. Once it is updated, its boot records arrive on an ordinary
    /// pull with the ids this device already gave its folders.
    func testAnOldServerLosesNothingAndItsLaterRecordsMatch() async throws {
        seedServer(bootPass: false)
        MigrationServer.config.isOld = true
        let container = try container()
        let store = try container.openStore()
        let engine = try engine(for: store)

        try await PaperMigrations.run(defaults: container.defaults, store: store, engine: engine)
        await engine.syncNow()

        var requests = MigrationServer.config.requests
        let pulls = requests.indices.filter { requests[$0].path == "/api/paper/changes" }
        XCTAssertEqual(pulls.map { requests[$0].since }, [C.cursor, 0])
        let pull = try XCTUnwrap(pulls.first)
        XCTAssertEqual(requests[..<pull].filter { $0.method != "GET" }.map(\.label), C.queuedRequests)
        XCTAssertEqual(requests.filter(\.isDownload).map(\.label), [])
        XCTAssertEqual(requests.filter { $0.path.hasPrefix("/api/paper/folders") }.map(\.label), [])
        try assertDrained(store)
        let cursor = Int(store.syncValue("changes.seq") ?? "") ?? 0
        XCTAssertGreaterThan(cursor, C.cursor)
        XCTAssertEqual(try store.folderRecords().map(\.id), C.folders.map(PaperFolderID.legacy(path:)))

        // The server is updated: its boot pass writes a record for every
        // directory, each at a new seq.
        MigrationServer.config.isOld = false
        for path in C.folders { MockPaperServer.state.seedFolder(path: path) }
        let seen = requests.count
        await engine.syncNow()

        requests = Array(MigrationServer.config.requests.dropFirst(seen))
        XCTAssertEqual(requests.map(\.label), ["GET /api/paper/changes"])
        XCTAssertEqual(requests.first?.since, cursor, "an ordinary pull")
        try assertDrained(store)
        let folders = try store.folderRecords()
        XCTAssertEqual(folders.map(\.path), C.folders)
        XCTAssertEqual(folders.map(\.id), C.folders.map(PaperFolderID.legacy(path:)))
        XCTAssertTrue(folders.allSatisfy { $0.seq > cursor })
    }

    /// Offline for days after the migration: nothing is sent, the work done
    /// meanwhile queues behind the old work, and the first sync online sends
    /// all of it in order before the full pull.
    func testDaysOfflineQueueBehindTheOldWorkAndDrainInOrder() async throws {
        seedServer(bootPass: true)
        let container = try container()
        let store = try container.openStore()
        let engine = try engine(for: store)
        await engine.setOnline(false)

        try await PaperMigrations.run(defaults: container.defaults, store: store, engine: engine)
        await engine.syncNow()
        XCTAssertEqual(MigrationServer.config.requests.map(\.label), [])

        try store.writeDrawing(Data("ledger, a week later".utf8), notebookPath: C.ledgerPath, pageID: C.ledgerPage)
        try store.journal.append(.init(kind: .pagePut, notebookPath: C.ledgerPath, pageID: C.ledgerPage))
        let taxes = try store.createFolder(name: "Taxes", in: "paper/Work")
        XCTAssertEqual(store.syncValue("changes.seq"), "0")

        await engine.setOnline(true)
        await engine.syncNow()

        let requests = MigrationServer.config.requests
        let pulls = requests.indices.filter { requests[$0].path == "/api/paper/changes" }
        XCTAssertEqual(pulls.map { requests[$0].since }, [C.cursor, 0])
        let pull = try XCTUnwrap(pulls.first)
        XCTAssertEqual(
            requests[..<pull].filter { $0.method != "GET" }.map(\.label),
            C.queuedRequests + [
                "PUT /api/paper/pages/\(C.ledgerPath)/\(C.ledgerPage)", "POST /api/paper/folders",
            ])
        XCTAssertEqual(requests.filter(\.isDownload).map(\.label), [])
        try assertDrained(store)
        XCTAssertEqual(MockPaperServer.state.withFolders { $0["paper/Work/Taxes"]?.id }, taxes.id)
    }

    /// The engine runs before the migration on a real launch, so a pull can
    /// be on its way when the cursor is reset. It lands what it fetched but
    /// leaves the reset alone, and the next pull is the full one.
    func testAPullUnderWayCannotUndoTheReset() async throws {
        seedServer(bootPass: true)
        let container = try container()
        let store = try container.openStore()
        let engine = try engine(for: store)

        MigrationServer.config.holdNextChanges()
        let pulling = Task { await engine.pull() }
        try await MigrationServer.config.waitUntilHeld()

        try await PaperMigrations.run(defaults: container.defaults, store: store, engine: engine)
        XCTAssertEqual(store.syncValue("changes.seq"), "0")
        MigrationServer.config.releaseHeld()
        await pulling.value

        XCTAssertEqual(MigrationServer.config.requests.first?.since, C.cursor, "the old cursor's pull")
        XCTAssertEqual(store.syncValue("changes.seq"), "0", "not written over")
        await engine.pull()
        let pulls = MigrationServer.config.requests.filter { $0.path == "/api/paper/changes" }
        XCTAssertEqual(pulls.map(\.since), [C.cursor, 0])
        XCTAssertGreaterThan(Int(store.syncValue("changes.seq") ?? "") ?? 0, C.cursor)
    }

    /// The full pull is additive and carries no tombstones, so the cursor
    /// the migration resets is first pulled from once: a notebook deleted
    /// upstream since this device last pulled goes to its trash here too,
    /// rather than staying on the iPad alone for good.
    func testADeleteMadeUpstreamSinceTheLastPullIsMirroredBeforeTheFullPull() async throws {
        seedServer(bootPass: true)
        MockPaperServer.state.recordDeletion(path: C.ideasPath, kind: "notebook")
        let container = try container()
        let store = try container.openStore()
        let engine = try engine(for: store)

        try await PaperMigrations.run(defaults: container.defaults, store: store, engine: engine)
        XCTAssertEqual(store.syncValue("changes.seq"), "0")
        XCTAssertEqual(store.syncValue("changes.reset_from"), "\(C.cursor)")
        // Nothing of the deleted notebook is queued here: work queued for
        // it would keep it, by the pull's rule.
        try store.journal.cancelUnpushed(
            kind: .pagePut, notebookPath: C.ideasPath, pageID: C.ideasPage2)
        await engine.syncNow()

        XCTAssertNil(try store.notebook(path: C.ideasPath), "the deletion was not mirrored")
        XCTAssertNotNil(try store.notebook(path: C.ledgerPath))
        XCTAssertNil(store.syncValue("changes.reset_from"))
        let pulls = MigrationServer.config.requests.filter { $0.path == "/api/paper/changes" }
        XCTAssertEqual(pulls.map(\.since), [C.cursor, 0])
        XCTAssertGreaterThan(Int(store.syncValue("changes.seq") ?? "") ?? 0, C.cursor)

        // The next pull is an ordinary one.
        await engine.pull()
        XCTAssertEqual(
            MigrationServer.config.requests.filter { $0.path == "/api/paper/changes" }.last?.since,
            Int(store.syncValue("changes.seq") ?? ""))
    }

    /// (Round 2, item 4) The pull from the old cursor met a network error or
    /// a 5xx: the mark stays, no full pull runs yet, and the next pull tries
    /// again and lands the delete. It used to clear the mark on any failure,
    /// so one flaky network on the first pull after the update lost the
    /// deletions it was there to catch.
    func testAPullFromTheOldCursorThatFailsOnTheWireIsTriedAgain() async throws {
        seedServer(bootPass: true)
        MockPaperServer.state.recordDeletion(path: C.ideasPath, kind: "notebook")
        let container = try container()
        let store = try container.openStore()
        let engine = try engine(for: store)
        try await PaperMigrations.run(defaults: container.defaults, store: store, engine: engine)
        try store.journal.cancelUnpushed(
            kind: .pagePut, notebookPath: C.ideasPath, pageID: C.ideasPage2)
        await engine.pushPending()
        MockPaperServer.state.failStatus = 503
        MockPaperServer.state.failPathEquals = "/api/paper/changes"
        MockPaperServer.state.failuresRemaining = 1

        await engine.pull()
        XCTAssertEqual(store.syncValue("changes.reset_from"), "\(C.cursor)", "the mark went")
        XCTAssertNotNil(try store.notebook(path: C.ideasPath))
        XCTAssertEqual(store.syncValue("changes.seq"), "0", "the full pull ran ahead of it")

        await engine.pull()
        XCTAssertNil(try store.notebook(path: C.ideasPath), "the deletion was lost")
        XCTAssertNil(store.syncValue("changes.reset_from"))
        let pulls = MigrationServer.config.requests.filter { $0.path == "/api/paper/changes" }
        XCTAssertEqual(pulls.map(\.since), [C.cursor, C.cursor, 0])
    }

    /// A refusal that will never change (a 4xx) goes on to the full pull, and
    /// so does a failure on the wire that keeps coming back, after a few
    /// pulls: the mark never holds the full pull back for good.
    func testAPullFromTheOldCursorThatCannotSucceedDoesNotHoldTheFullPullBack() async throws {
        for (status, attempts) in [(400, 1), (503, 5)] {
            MockPaperServer.state.reset()
            MigrationServer.config.reset()
            seedServer(bootPass: true)
            let container = try container()
            let store = try container.openStore()
            let engine = try engine(for: store)
            try await PaperMigrations.run(defaults: container.defaults, store: store, engine: engine)
            await engine.pushPending()
            MockPaperServer.state.failStatus = status
            MockPaperServer.state.failPathEquals = "/api/paper/changes"
            MockPaperServer.state.failuresRemaining = attempts

            for _ in 0..<attempts { await engine.pull() }

            XCTAssertNil(store.syncValue("changes.reset_from"), "\(status)")
            XCTAssertGreaterThan(Int(store.syncValue("changes.seq") ?? "") ?? 0, C.cursor, "\(status)")
            let pulls = MigrationServer.config.requests.filter { $0.path == "/api/paper/changes" }
            XCTAssertEqual(pulls.map(\.since), Array(repeating: C.cursor, count: attempts) + [0], "\(status)")
        }
    }

    /// With no engine the cursor is simply 0, as before.
    func testWithNoEngineTheCursorIsSimplyReset() async throws {
        let container = try container()
        let store = try container.openStore()
        try await PaperMigrations.run(defaults: container.defaults, store: store, engine: nil)
        XCTAssertEqual(store.syncValue("changes.seq"), "0")
        XCTAssertNil(store.syncValue("changes.reset_from"))
    }

    // MARK: - Helpers

    private func engine(for store: PaperStore) throws -> SyncEngine {
        let settings = try XCTUnwrap(ServerSettings(urlString: "https://paper.test"))
        let api = PaperAPI(
            settings: settings, auth: AnonymousAuthProvider(), session: MigrationServer.makeSession())
        return SyncEngine(
            store: store, api: api, clientID: C.clientID, clock: TestClock(), jitter: { $0 })
    }

    private func assertDrained(_ store: PaperStore, file: StaticString = #filePath, line: UInt = #line)
        throws
    {
        XCTAssertEqual(try store.journal.pendingCount(), 0, file: file, line: line)
        XCTAssertEqual(try store.journal.parked(), [], file: file, line: line)
    }

    /// The server as this device last saw it, at seq 5: the two notebooks
    /// it had pushed, page for page at the revisions and seqs the device
    /// keeps. With `bootPass`, the records a server with folder records
    /// writes at boot for directories that had none, each at a new seq.
    private func seedServer(bootPass: Bool) {
        let state = MockPaperServer.state
        state.seed(
            notebook: C.ideasPath, id: C.ideas, title: "Ideas",
            pages: [
                .init(
                    id: C.ideasPage1, revision: 3, renderRevision: 3, textRevision: 3,
                    drawing: Data("ideas, page 1".utf8), render: Data("png 1".utf8)),
                .init(
                    id: C.ideasPage2, revision: 2, renderRevision: 2,
                    drawing: Data("ideas, page 2, as pushed".utf8), render: Data("png 2".utf8)),
            ])
        state.seed(
            notebook: C.ledgerPath, id: C.ledger, title: "Ledger", paperSize: "letter",
            pages: [
                .init(id: C.ledgerPage, revision: 1, renderRevision: 1, drawing: Data("ledger".utf8))
            ])
        state.withState { notebooks in
            notebooks[C.ideasPath]?.cover = nil
            notebooks[C.ledgerPath]?.cover = nil
            notebooks[C.ledgerPath]?.tags = ["tax"]
        }
        // The seqs the device holds.
        XCTAssertEqual(
            state.withState { [$0[C.ideasPath]?.seq, $0[C.ideasPath]?.pages[C.ideasPage1]?.seq,
                $0[C.ideasPath]?.pages[C.ideasPage2]?.seq, $0[C.ledgerPath]?.seq,
                $0[C.ledgerPath]?.pages[C.ledgerPage]?.seq] },
            [1, 2, 3, 4, 5])
        if bootPass {
            for path in C.folders { state.seedFolder(path: path) }
        } else {
            state.seedDirectory("paper/Empty")
        }
    }
}

// MARK: - The shipped build's container

/// A container as the shipped build (`main` before the rebuild) leaves it,
/// written here the way that build writes it:
///
/// - `vault/paper/` holds `Work/Ideas.paper`, `Work/Accounting/Ledger.paper`,
///   `Inbox.paper` and an empty `Empty/`, with no `folder.json` anywhere;
///   `notebook.json` and `page.json` in that build's keys, dates in
///   milliseconds; ink, renders and a transcript; a snapshot; a device trash
///   entry.
/// - `paper.sqlite` at that build's schema (`v1` to `v3-ops-notebook-id`),
///   with its index rows, a change cursor at 5, and five ops it never sent:
///   ink on Ideas page 2, the offline-made Inbox (its create, its page, its
///   ink), and a favourite on Ledger.
/// - `UserDefaults` with every key that build wrote: the capsule's
///   `paper.tool*`, the two retired settings, the languages as a
///   comma-joined string, the server and the client id.
struct V1Container {
    static let ideas = "6f1d2c0a9b8e4d7c8a5b3e2f1d0c9b8a"
    static let ledger = "7a2e3d1b0c9f5e8d9b6c4f3a2e1d0cab"
    static let inbox = "8b3f4e2c1d0a6f9e0c7d5a4b3f2e1dbc"
    static let ideasPage1 = "a1000000000000000000000000000001"
    static let ideasPage2 = "a1000000000000000000000000000002"
    static let ledgerPage = "b2000000000000000000000000000003"
    static let inboxPage = "c3000000000000000000000000000004"
    static let clientID = "ipad-0123456789abcdef"
    static let ideasPath = "paper/Work/Ideas.paper"
    static let ledgerPath = "paper/Work/Accounting/Ledger.paper"
    static let inboxPath = "paper/Inbox.paper"

    /// Sorted by path, as `folderRecords()` lists them.
    static let folders = ["paper/Empty", "paper/Work", "paper/Work/Accounting"]
    static let notebooks = [inboxPath, ledgerPath, ideasPath]
    /// The notebooks the server has once the queue has drained, by path.
    static let serverNotebooks = [inboxPath, ledgerPath, ideasPath]
    /// The change cursor of the last pull.
    static let cursor = 5

    static let capsuleKeys = [
        "paper.tool.penWidth", "paper.tool.penColor", "paper.tool.highlighterWidth",
        "paper.tool.highlighterColor", "paper.tool.eraserWidth", "paper.tool.eraserBitmap",
        "paper.tool.capsuleEdge", "paper.tool.capsuleOffset",
    ]

    /// The writes the five queued ops become, in order.
    static let queuedRequests = [
        "PUT /api/paper/pages/\(ideasPath)/\(ideasPage2)",
        "POST /api/paper/notebooks",
        "POST /api/paper/notebooks/\(inboxPath)/pages",
        "PUT /api/paper/pages/\(inboxPath)/\(inboxPage)",
        "PATCH /api/paper/notebooks/\(ledgerPath)",
    ]

    let root: URL
    let scratch: ScratchDefaults
    var suiteName: String { scratch.name }
    var defaults: UserDefaults { scratch.defaults }
    var paths: PaperPaths { PaperPaths(root: root) }

    /// The suite's own keys, without the argument and global domains.
    var domain: [String: Any] { defaults.persistentDomain(forName: suiteName) ?? [:] }

    func openStore() throws -> PaperStore { try PaperStore(root: root) }

    func remove() {
        scratch.remove()
        try? FileManager.default.removeItem(at: root)
    }

    func hasFolderJSON(_ path: String) -> Bool {
        FileManager.default.fileExists(
            atPath: paths.url(forVaultPath: path).appending(path: "folder.json")
                .path(percentEncoded: false))
    }

    /// A first install: no tree, no database, no settings.
    static func empty() throws -> V1Container {
        let name = "MigrationTests-\(UUID().uuidString)"
        let root = FileManager.default.temporaryDirectory.appending(path: name, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        return V1Container(root: root, scratch: try ScratchDefaults("MigrationTests"))
    }

    static func make() throws -> V1Container {
        let container = try empty()
        try container.writeTree()
        try container.writeDatabase()
        container.writeDefaults()
        return container
    }

    // MARK: Writing

    private static let created = "2026-09-03T09:00:00.000Z"
    private static let modified = "2026-09-20T18:30:00.000Z"
    /// The same instants as seconds since 1970, as that build's rows hold them.
    private static let createdSeconds = 1_756_890_000.0
    private static let modifiedSeconds = 1_758_393_000.0

    private struct V1Page {
        var id: String
        var notebook: String
        var notebookID: String
        var template: String
        var size: String
        var revision: Int
        var renderRevision = 0
        var textRevision = 0
        var seq: Int
        var ink: String
        var render = false
        var transcript: String?
    }

    private struct V1Notebook {
        var id: String
        var path: String
        var title: String
        var template: String
        var size: String
        var tags: [String] = []
        var favorite = false
        var pages: [String]
        var seq: Int
    }

    private static let v1Notebooks = [
        V1Notebook(
            id: ideas, path: ideasPath, title: "Ideas", template: "ruled", size: "a4",
            pages: [ideasPage1, ideasPage2], seq: 1),
        V1Notebook(
            id: ledger, path: ledgerPath, title: "Ledger", template: "ruled", size: "letter",
            tags: ["tax"], favorite: true, pages: [ledgerPage], seq: 4),
        V1Notebook(
            id: inbox, path: inboxPath, title: "Inbox", template: "dotted", size: "a4",
            pages: [inboxPage], seq: 0),
    ]

    private static let v1Pages = [
        V1Page(
            id: ideasPage1, notebook: ideasPath, notebookID: ideas, template: "ruled", size: "a4",
            revision: 3, renderRevision: 3, textRevision: 3, seq: 2, ink: "ideas, page 1",
            render: true, transcript: "quarterly review"),
        // Written on after its last push: the ink differs from the server's.
        V1Page(
            id: ideasPage2, notebook: ideasPath, notebookID: ideas, template: "ruled", size: "a4",
            revision: 2, renderRevision: 2, seq: 3, ink: "ideas, page 2, written offline",
            render: true),
        V1Page(
            id: ledgerPage, notebook: ledgerPath, notebookID: ledger, template: "ruled",
            size: "letter", revision: 1, renderRevision: 1, seq: 5, ink: "ledger"),
        V1Page(
            id: inboxPage, notebook: inboxPath, notebookID: inbox, template: "dotted", size: "a4",
            revision: 0, seq: 0, ink: "inbox, never sent"),
    ]

    private func write(_ data: Data, _ relative: String) throws {
        let url = root.appending(path: relative)
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try data.write(to: url)
    }

    private func writeJSON(_ object: [String: Any], _ relative: String) throws {
        try write(
            JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys]),
            relative)
    }

    private func writeTree() throws {
        for notebook in Self.v1Notebooks {
            // That build's `Notebook` encoding: no `cover` while it is nil.
            try writeJSON(
                [
                    "id": notebook.id, "title": notebook.title, "created": Self.created,
                    "modified": Self.modified, "template": notebook.template,
                    "paperSize": notebook.size, "tags": notebook.tags, "favorite": notebook.favorite,
                    "pages": notebook.pages, "seq": notebook.seq,
                ], "vault/\(notebook.path)/notebook.json")
        }
        for page in Self.v1Pages {
            let directory = "vault/\(page.notebook)/pages/\(page.id)"
            try writeJSON(
                [
                    "id": page.id, "bookmarked": false, "template": page.template, "size": page.size,
                    "created": Self.created, "modified": Self.modified, "revision": page.revision,
                    "render_revision": page.renderRevision, "text_revision": page.textRevision,
                    "objects": [Any](), "seq": page.seq,
                ], "\(directory)/page.json")
            try write(Data(page.ink.utf8), "\(directory)/drawing.pkdrawing")
            if page.render { try write(Data("png of \(page.ink)".utf8), "\(directory)/render.png") }
            if let transcript = page.transcript {
                try writeJSON(
                    [
                        "lines": [Any](), "transcript": transcript, "corrections": [Any](),
                        "for_revision": page.textRevision,
                    ], "\(directory)/text.json")
            }
        }
        try FileManager.default.createDirectory(
            at: paths.url(forVaultPath: "paper/Empty"), withIntermediateDirectories: true)
        try write(Data("ideas, page 1".utf8), "snapshots/\(Self.ideasPage1)/1758393000000.pkdrawing")
        try writeJSON(
            [
                "kind": "page", "notebookPath": Self.ideasPath, "notebookID": Self.ideas,
                "pageID": "d4000000000000000000000000000005", "position": 2,
                "deletedAt": Self.modified,
            ], ".trash/e5000000000000000000000000000006/manifest.json")
        try write(Data("a deleted page".utf8), ".trash/e5000000000000000000000000000006/page/drawing.pkdrawing")
    }

    private func writeDatabase() throws {
        let queue = try DatabaseQueue(path: paths.database.path(percentEncoded: false))
        try PaperDatabase.migrator.migrate(queue, upTo: "v3-ops-notebook-id")
        try queue.write { db in
            for notebook in Self.v1Notebooks {
                try db.execute(
                    sql: """
                        INSERT INTO notebooks (id, path, title, folder, template, paper_size, cover,
                            tags, favorite, created, modified, opened, seq)
                        VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
                        """,
                    arguments: [
                        notebook.id, notebook.path, notebook.title,
                        PaperPaths.folder(ofNotebookPath: notebook.path), notebook.template,
                        notebook.size,
                        String(decoding: try JSONEncoder().encode(notebook.tags), as: UTF8.self),
                        notebook.favorite, Self.createdSeconds, Self.modifiedSeconds,
                        Self.modifiedSeconds, notebook.seq,
                    ])
            }
            for page in Self.v1Pages {
                let position =
                    Self.v1Notebooks.first { $0.id == page.notebookID }?.pages.firstIndex(of: page.id)
                    ?? 0
                try db.execute(
                    sql: """
                        INSERT INTO pages (id, notebook_id, position, template, paper_size, created,
                            modified, revision, render_revision, text_revision, conflict_of, seq,
                            title, bookmarked, custom_width, custom_height)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, 0, NULL, NULL)
                        """,
                    arguments: [
                        page.id, page.notebookID, position, page.template, page.size,
                        Self.createdSeconds, Self.modifiedSeconds, page.revision,
                        page.renderRevision, page.textRevision, page.seq,
                    ])
            }
            try db.execute(
                sql: """
                    INSERT INTO notebook_state (notebook_id, last_page_id, zoom, offset_x, offset_y)
                    VALUES (?, ?, 1.25, 0, 840)
                    """, arguments: [Self.ideas, Self.ideasPage2])
            // The queue, as that build's `OpJournal.insert` wrote it.
            let ops: [(String, String, String, String, [String: String])] = [
                ("pagePut", Self.ideasPath, Self.ideas, Self.ideasPage2, [:]),
                ("notebookCreate", Self.inboxPath, Self.inbox, "", [:]),
                ("pageCreate", Self.inboxPath, Self.inbox, Self.inboxPage, [:]),
                ("pagePut", Self.inboxPath, Self.inbox, Self.inboxPage, [:]),
                ("notebookPatch", Self.ledgerPath, Self.ledger, "", ["favorite": "true"]),
            ]
            for (index, op) in ops.enumerated() {
                try db.execute(
                    sql: """
                        INSERT INTO ops (kind, notebook_path, notebook_id, page_id, payload,
                            created_at, pushed)
                        VALUES (?, ?, ?, ?, ?, ?, 0)
                        """,
                    arguments: [
                        op.0, op.1, op.2, op.3,
                        String(decoding: try JSONEncoder().encode(op.4), as: UTF8.self),
                        Self.modifiedSeconds + Double(index),
                    ])
            }
            for (key, value) in [
                ("changes.seq", "\(Self.cursor)"), ("changes.epoch", "epoch-1"),
                ("assets.backfill.v1", "done"),
                ("etag.\(Self.ideasPath)/pages/\(Self.ideasPage1).drawing.pkdrawing", "\"3-abc\""),
            ] {
                try db.execute(
                    sql: "INSERT INTO sync_state (key, value) VALUES (?, ?)", arguments: [key, value])
            }
            try db.execute(
                sql: """
                    INSERT INTO snapshots (page_id, path, bytes, created_at) VALUES (?, ?, ?, ?)
                    """,
                arguments: [Self.ideasPage1, "1758393000000.pkdrawing", 13, Self.modifiedSeconds])
        }
        try queue.close()
    }

    private func writeDefaults() {
        let values: [String: Any] = [
            "paper.tool": "highlighter",
            "paper.tool.penWidth": 4.0,
            "paper.tool.penColor": 2,
            "paper.tool.highlighterWidth": 18.0,
            "paper.tool.highlighterColor": 1,
            "paper.tool.eraserWidth": 30.0,
            "paper.tool.eraserBitmap": false,
            "paper.tool.capsuleEdge": "bottom",
            "paper.tool.capsuleOffset": 0.3,
            "paper.drawWithFinger": true,
            "paper.useSystemToolPicker": true,
            "paper.recognitionLanguages": "en-US,de-DE",
            "paper.serverURL": "https://paper.test",
            "paper.clientID": Self.clientID,
        ]
        for (key, value) in values { defaults.set(value, forKey: key) }
    }

    // MARK: Reading

    /// Everything the migration may or may not change, dates of new folder
    /// records aside.
    struct State: Equatable {
        var defaults: NSDictionary
        var syncState: [String: String]
        var folders: [String]
        var notebooks: [String]
        var ops: [[String]]
        /// SHA-256 of every file under the root but the database and the
        /// folder records, by path.
        var files: [String: String]
        var recordedFolders: [String]
    }

    /// The state with no store open: what the shipped build left.
    func state() throws -> State {
        let queue = try DatabaseQueue(path: paths.database.path(percentEncoded: false))
        defer { try? queue.close() }
        return State(
            defaults: NSDictionary(dictionary: domain),
            syncState: try Self.syncState(queue), folders: [], notebooks: [],
            ops: try Self.ops(queue), files: try files(), recordedFolders: recordedFolders())
    }

    func state(_ store: PaperStore) throws -> State {
        State(
            defaults: NSDictionary(dictionary: domain),
            syncState: try syncState(store),
            folders: try store.folderRecords().map {
                "\($0.id) \($0.path) \($0.name) \($0.color.rawValue) \($0.icon?.wire ?? "-") \($0.favorite) \($0.seq)"
            },
            notebooks: try store.notebooks().map {
                "\($0.id) \($0.path) \($0.title) \($0.kind.rawValue) \($0.pages) \($0.tags) \($0.favorite)"
            }.sorted(),
            ops: try ops(store), files: try files(), recordedFolders: recordedFolders())
    }

    func ops(_ store: PaperStore) throws -> [[String]] { try Self.ops(store.database.queue) }

    func syncState(_ store: PaperStore) throws -> [String: String] {
        try Self.syncState(store.database.queue)
    }

    private static func ops(_ queue: DatabaseQueue) throws -> [[String]] {
        try queue.read { db in
            try Row.fetchAll(
                db,
                sql: """
                    SELECT id, kind, notebook_path, notebook_id, page_id, payload, created_at,
                        pushed, parked_reason
                    FROM ops ORDER BY id
                    """
            ).map { row in row.databaseValues.map(\.description) }
        }
    }

    private static func syncState(_ queue: DatabaseQueue) throws -> [String: String] {
        try queue.read { db in
            Dictionary(
                uniqueKeysWithValues: try Row.fetchAll(db, sql: "SELECT key, value FROM sync_state")
                    .map { ($0["key"] as String, $0["value"] as String) })
        }
    }

    private func files() throws -> [String: String] {
        let base = root.resolvingSymlinksInPath().path(percentEncoded: false)
        var result: [String: String] = [:]
        guard
            let walk = FileManager.default.enumerator(
                at: root, includingPropertiesForKeys: [.isRegularFileKey])
        else { return result }
        for case let url as URL in walk {
            guard (try url.resourceValues(forKeys: [.isRegularFileKey])).isRegularFile == true else {
                continue
            }
            let name = url.lastPathComponent
            if name.hasPrefix("paper.sqlite") || name == "folder.json" { continue }
            let path = url.resolvingSymlinksInPath().path(percentEncoded: false)
            let relative = path.hasPrefix(base) ? String(path.dropFirst(base.count)) : path
            let digest = SHA256.hash(data: try Data(contentsOf: url))
            result[relative] = digest.map { String(format: "%02x", $0) }.joined()
        }
        return result
    }

    private func recordedFolders() -> [String] {
        var found: [String] = []
        func walk(_ path: String) {
            let url = paths.url(forVaultPath: path)
            let children =
                (try? FileManager.default.contentsOfDirectory(
                    at: url, includingPropertiesForKeys: [.isDirectoryKey],
                    options: [.skipsHiddenFiles])) ?? []
            for child in children where child.hasDirectoryPath && !child.lastPathComponent.hasSuffix(".paper") {
                let inner = "\(path)/\(child.lastPathComponent)"
                if hasFolderJSON(inner) { found.append(inner) }
                walk(inner)
            }
        }
        walk("paper")
        return found.sorted()
    }
}

// MARK: - The server, recorded, old or held

/// `MockPaperServer`'s state behind a protocol of this file's own, so the
/// mock itself stays as its slice left it. It records every request with its
/// query, can answer as a server from before folder records (no
/// `folder_records`, `folders` or `folder_moves`, none of the new document
/// fields, and no folder routes), and can hold the change feed's answer
/// back while a test does something else.
final class MigrationServer: URLProtocol, @unchecked Sendable {
    struct Request: Sendable {
        var method: String
        var path: String
        var since: Int?

        var label: String { "\(method) \(path)" }
        /// A page, or one of its files, fetched.
        var isDownload: Bool {
            method == "GET" && (path.hasPrefix("/api/paper/pages/") || path.hasPrefix("/api/paper/blob/"))
        }
    }

    final class Config: @unchecked Sendable {
        private let lock = NSLock()
        private var _isOld = false
        private var _requests: [Request] = []
        private var holding = false
        private var held = false
        private var release = DispatchSemaphore(value: 0)

        var isOld: Bool {
            get { lock.withLock { _isOld } }
            set { lock.withLock { _isOld = newValue } }
        }

        var requests: [Request] { lock.withLock { _requests } }

        func record(_ request: Request) { lock.withLock { _requests.append(request) } }

        func reset() {
            let stuck = lock.withLock { () -> DispatchSemaphore? in
                defer {
                    _isOld = false
                    _requests = []
                    holding = false
                    held = false
                    release = DispatchSemaphore(value: 0)
                }
                return held ? release : nil
            }
            stuck?.signal()
        }

        /// The next change feed waits until `releaseHeld()`.
        func holdNextChanges() { lock.withLock { holding = true } }

        func releaseHeld() { lock.withLock { release }.signal() }

        func waitUntilHeld() async throws {
            for _ in 0..<500 {
                if lock.withLock({ held }) { return }
                try await Task.sleep(nanoseconds: 10_000_000)
            }
            XCTFail("the change feed was never asked for")
        }

        /// True once, for the request that is to wait.
        fileprivate func shouldHold() -> DispatchSemaphore? {
            lock.withLock {
                guard holding, !held else { return nil }
                held = true
                return release
            }
        }
    }

    static let config = Config()

    static func makeSession() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpShouldSetCookies = false
        configuration.protocolClasses = [MigrationServer.self]
        return URLSession(configuration: configuration)
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let url = request.url else {
            client?.urlProtocol(self, didFailWithError: URLError(.badURL))
            return
        }
        let path = url.path(percentEncoded: false)
        let query = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        let method = request.httpMethod ?? "GET"
        Self.config.record(
            Request(
                method: method, path: path,
                since: query.first { $0.name == "since" }?.value.flatMap { Int($0) }))
        if path == "/api/paper/changes", let release = Self.config.shouldHold() {
            _ = release.wait(timeout: .now() + 10)
        }

        let status: Int
        let headers: [String: String]
        var data: Data
        if Self.config.isOld, path.hasPrefix("/api/paper/folders") {
            (status, headers, data) = (404, ["Content-Type": "application/json"], Data(#"{"detail":"Not Found"}"#.utf8))
        } else {
            (status, headers, data) = MockPaperServer.state.handle(
                request, body: request.httpBody ?? Self.read(request.httpBodyStream))
            if Self.config.isOld { data = Self.asOldServer(path: path, data) }
        }
        guard
            let response = HTTPURLResponse(
                url: url, statusCode: status, httpVersion: "HTTP/1.1", headerFields: headers)
        else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}

    /// What a server from before folder records answered.
    static func asOldServer(path: String, _ data: Data) -> Data {
        guard var object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            return data
        }
        func strip(_ value: Any) -> Any {
            guard var fields = value as? [String: Any] else { return value }
            for key in ["kind", "orientation", "paperColor", "folder_id"] {
                fields.removeValue(forKey: key)
            }
            return fields
        }
        switch path {
        case "/api/paper":
            object.removeValue(forKey: "folder_records")
            object["notebooks"] = (object["notebooks"] as? [Any])?.map(strip) ?? []
        case "/api/paper/changes":
            object.removeValue(forKey: "folders")
            object.removeValue(forKey: "folder_moves")
            object["notebooks"] = (object["notebooks"] as? [Any])?.map(strip) ?? []
            object["deleted"] =
                (object["deleted"] as? [[String: Any]])?.filter { $0["kind"] as? String != "folder" }
                ?? []
        default:
            if path.hasPrefix("/api/paper/notebooks") {
                object = strip(object) as? [String: Any] ?? object
            }
        }
        return (try? JSONSerialization.data(withJSONObject: object)) ?? data
    }

    private static func read(_ stream: InputStream?) -> Data {
        guard let stream else { return Data() }
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 64 * 1024)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            if count <= 0 { break }
            data.append(contentsOf: buffer[0..<count])
        }
        return data
    }
}
