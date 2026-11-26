import Foundation
import os

/// `log stream --predicate 'subsystem == "art.exscalia.ipad" AND category == "migration"'`
/// shows each run. Counts and step names only: paths are the owner's.
private let log = Logger(subsystem: "art.exscalia.ipad", category: "migration")

/// Brings forward, once per install, the container an earlier build left on
/// the device: notebooks in folders that have no records, a change cursor,
/// an op journal that may hold work not yet sent, and the tool capsule's
/// settings.
///
/// It runs over the owner's real notebooks, so it is narrow on purpose. Of
/// its own accord it deletes two settings keys and nothing else (the tool
/// settings it loads delete the capsule's keys once they are carried over);
/// it never touches a page's ink or an op in the journal; and it never waits
/// on the network: the reset cursor makes the next pull a full one, and the
/// reconcile gives legacy folders their records here and now. Every step is
/// safe to repeat and the version is recorded only after all of them have
/// succeeded, so a launch killed half-way is finished by the next one. Each
/// step is an existing operation; this file only orders and checks them.
public enum PaperMigrations {
    /// The last migration this install finished, in `UserDefaults`. Absent,
    /// which reads as 0, until one has.
    public static let versionKey = "paper.migrations.version"
    /// 1: the container of the builds before folders, whiteboards and the
    /// toolbar. 2: the pen widths of the first rebuilt build, measured too
    /// thin against the reference. 3: the pencil and highlighter widths,
    /// scaled with the pen.
    public static let currentVersion = 3

    /// Migration 1, in the contract's order, then migration 2.
    public enum Step: Int, CaseIterable, Sendable {
        /// `ToolSettingsStore.load`, which turns the capsule's `paper.tool.*`
        /// keys into `paper.tools.v2`.
        case toolSettings = 1
        /// Deletes `paper.drawWithFinger` and `paper.useSystemToolPicker`.
        case retiredSettings
        /// A comma-joined `paper.recognitionLanguages` becomes `[String]`,
        /// which is what recognition reads.
        case recognitionLanguages
        /// The change cursor goes back to 0, so the next pull brings the
        /// folder records and the new fields with everything else.
        case changeCursor
        /// The reconcile, after which every folder directory has a record.
        case reconcile
        /// Migration 2: pen, Draw Shape and Shapes slots still at the first
        /// rebuilt build's factory widths (1, 2 and 3.5) take the calibrated
        /// ones; slots the owner changed are left alone.
        case penWidths
        /// Migration 3: the same for the pencil (1.5, 2.5 and 4) and the
        /// highlighter (10, 16 and 24).
        case otherBrushWidths

        /// The migration this step belongs to.
        public var version: Int {
            switch self {
            case .penWidths: return 2
            case .otherBrushWidths: return 3
            default: return 1
            }
        }
    }

    /// The pen widths the first rebuilt build shipped with, before the
    /// medium slot was measured against the reference.
    static let uncalibratedPenWidths: [Double] = [1.0, 2.0, 3.5]
    /// The pencil and highlighter widths of the builds before migration 3.
    static let uncalibratedWidths: [PaperTool: [Double]] = [
        .pencil: [1.5, 2.5, 4.0], .highlighter: [10, 16, 24],
    ]

    public enum Outcome: Equatable, Sendable {
        /// This install had finished already; nothing ran.
        case current
        /// Every step ran, the reconcile among them, and the version is
        /// recorded.
        case migrated
    }

    public enum Failure: Error, Equatable, Sendable {
        /// `ToolSettingsStore.load` left no readable `paper.tools.v2`.
        case toolSettingsNotSaved
        /// Directories the reconcile left without a folder record.
        case foldersWithoutRecords([String])
    }

    /// The settings that "the Pencil draws, fingers navigate" and the one
    /// toolbar retired. Nothing reads them any more.
    static let retiredKeys = ["paper.drawWithFinger", "paper.useSystemToolPicker"]

    /// Runs every migration this install has not finished, in order.
    ///
    /// `engine` is the one running on `store`, if any: the cursor is reset
    /// through it, so a pull already under way cannot write its own cursor
    /// back over the reset. `afterStep` runs after each step; a test throws
    /// from it to stand for a launch killed there.
    @discardableResult
    public static func run(
        defaults: UserDefaults, store: PaperStore, engine: SyncEngine?,
        afterStep: (Step) throws -> Void = { _ in }
    ) async throws -> Outcome {
        let finished = defaults.integer(forKey: versionKey)
        guard finished < currentVersion else { return .current }
        for step in Step.allCases where step.version > finished {
            do {
                try await perform(step, defaults: defaults, store: store, engine: engine)
            } catch {
                log.error(
                    "migration \(currentVersion) stopped at \(String(describing: step), privacy: .public): \(String(describing: error), privacy: .private)"
                )
                throw error
            }
            try afterStep(step)
        }
        defaults.set(currentVersion, forKey: versionKey)
        let folders = (try? store.folderRecords().count) ?? 0
        let queued = (try? store.journal.pendingCount()) ?? 0
        log.notice(
            "migration \(currentVersion) finished: \(folders) folders, \(queued) ops still to send")
        return .migrated
    }

    static func perform(
        _ step: Step, defaults: UserDefaults, store: PaperStore, engine: SyncEngine?
    ) async throws {
        switch step {
        case .toolSettings:
            _ = ToolSettingsStore.load(defaults)
            guard ToolSettingsStore.decode(defaults.object(forKey: ToolSettingsStore.key)) != nil
            else { throw Failure.toolSettingsNotSaved }
        case .retiredSettings:
            for key in retiredKeys { defaults.removeObject(forKey: key) }
        case .recognitionLanguages:
            let key = ServerSettings.Key.recognitionLanguages
            // Earlier builds stored "en-US,de-DE" through `@AppStorage`; an
            // array is left as it is.
            if let joined = defaults.object(forKey: key) as? String {
                defaults.set(languages(joined: joined), forKey: key)
            }
        case .changeCursor:
            if let engine {
                try await engine.forgetCursor()
            } else {
                try SyncEngine.forgetCursor(in: store)
            }
        case .reconcile:
            try store.reconcile()
            let bare = try foldersWithoutRecords(in: store)
            guard bare.isEmpty else { throw Failure.foldersWithoutRecords(bare) }
        case .penWidths:
            recalibrate(
                [.pen: uncalibratedPenWidths, .drawShape: uncalibratedPenWidths, .shape: uncalibratedPenWidths],
                in: defaults)
        case .otherBrushWidths:
            recalibrate(uncalibratedWidths, in: defaults)
        }
    }

    /// Slots still at the widths given take their tool's factory widths;
    /// a slot the owner changed is left alone.
    static func recalibrate(_ uncalibrated: [PaperTool: [Double]], in defaults: UserDefaults) {
        var settings = ToolSettingsStore.load(defaults)
        var changed = false
        for (tool, old) in uncalibrated {
            guard var slot = settings.slots[tool], slot.widths == old,
                let factory = ToolSettings.factorySlots(tool)
            else { continue }
            slot.widths = factory.widths
            settings.slots[tool] = slot
            changed = true
        }
        if changed { ToolSettingsStore.save(settings, defaults) }
    }

    /// `"en-US, de-DE"` as `["en-US", "de-DE"]`.
    static func languages(joined: String) -> [String] {
        joined.split(separator: ",")
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
    }

    /// The directories the reconcile visits -- every one under `paper/` that
    /// is not hidden and not a package -- that have no folder record. Read
    /// under the store's write lock, so a folder a pull is making is never
    /// caught between its directory and its record.
    static func foldersWithoutRecords(in store: PaperStore) throws -> [String] {
        store.writeLock.lock()
        defer { store.writeLock.unlock() }
        var directories: [String] = []
        collectFolders(in: store.paths.paper, prefix: [FolderPath.root], into: &directories)
        let recorded = Set(try store.folderRecords().map(\.path))
        return directories.filter { !recorded.contains($0) }.sorted()
    }

    private static func collectFolders(
        in directory: URL, prefix: [String], into result: inout [String]
    ) {
        let children =
            (try? FileManager.default.contentsOfDirectory(
                at: directory, includingPropertiesForKeys: [.isDirectoryKey],
                options: [.skipsHiddenFiles])) ?? []
        for child in children where child.hasDirectoryPath {
            let name = child.lastPathComponent
            guard !name.hasSuffix(".paper") else { continue }
            result.append((prefix + [name]).joined(separator: "/"))
            collectFolders(in: child, prefix: prefix + [name], into: &result)
        }
    }
}
