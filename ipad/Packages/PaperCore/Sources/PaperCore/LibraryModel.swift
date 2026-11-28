import Foundation

// MARK: - Items

/// One row or tile of the library: a folder or a document. Pure values, so the
/// arrangement below is the same function for the device's library and its
/// tests.
public enum LibraryItem: Identifiable, Equatable, Sendable {
    case folder(PaperFolder)
    case document(Notebook)

    /// Unique across both cases: `folder/<id>` or `document/<id>`.
    public var id: String {
        switch self {
        case .folder(let folder): return "folder/\(folder.id)"
        case .document(let notebook): return "document/\(notebook.id)"
        }
    }

    /// A folder's name or a document's title.
    public var name: String {
        switch self {
        case .folder(let folder): return folder.name
        case .document(let notebook): return notebook.title
        }
    }

    /// What the date line shows and the date sort orders by.
    public var modified: Date {
        switch self {
        case .folder(let folder): return folder.modified
        case .document(let notebook): return notebook.modified
        }
    }

    public var isFavorite: Bool {
        switch self {
        case .folder(let folder): return folder.favorite
        case .document(let notebook): return notebook.favorite
        }
    }

    /// The document's kind, or `nil` for a folder.
    public var documentKind: DocumentKind? {
        if case .document(let notebook) = self { return notebook.kind }
        return nil
    }

    /// Folders, then notebooks, then whiteboards: the order of the type sort.
    fileprivate var typeRank: Int {
        switch documentKind {
        case nil: return 0
        case .notebook?: return 1
        case .whiteboard?: return 2
        }
    }
}

// MARK: - Sort, filter, layout

/// The library's sort, kept in `UserDefaults` under `storageKey`.
public enum LibrarySort: String, CaseIterable, Sendable {
    /// `modified`, newest first, folders and documents interleaved; ties by name.
    case date
    /// Name ascending by `localizedStandardCompare`, interleaved; ties by date.
    case name
    /// Folders, then notebooks, then whiteboards, each by name.
    case type

    public static let storageKey = "paper.library.sort"
    public static let standard: LibrarySort = .date

    public var displayName: String {
        switch self {
        case .date: return "Date"
        case .name: return "Name"
        case .type: return "Type"
        }
    }
}

/// The library's filter, kept in `UserDefaults` under `storageKey`.
public enum LibraryFilter: String, CaseIterable, Sendable {
    case all, folders, notebooks, whiteboards

    public static let storageKey = "paper.library.filter"
    public static let standard: LibraryFilter = .all

    public var displayName: String {
        switch self {
        case .all: return "All"
        case .folders: return "Folders"
        case .notebooks: return "Notebooks"
        case .whiteboards: return "Whiteboards"
        }
    }

    public func includes(_ item: LibraryItem) -> Bool {
        switch self {
        case .all: return true
        case .folders: return item.documentKind == nil
        case .notebooks: return item.documentKind == .notebook
        case .whiteboards: return item.documentKind == .whiteboard
        }
    }
}

/// List or grid, kept in `UserDefaults` under `storageKey`.
public enum LibraryLayout: String, Sendable {
    case list, grid

    public static let storageKey = "paper.library.layout"
    public static let standard: LibraryLayout = .list
}

// MARK: - Arrangement

public enum LibraryArrangement {
    /// Filters, then sorts. Every order is total -- the last tie-break is the
    /// item's id -- so a list never reshuffles between two identical reads.
    public static func arrange(
        _ items: [LibraryItem], sort: LibrarySort, filter: LibraryFilter
    ) -> [LibraryItem] {
        items.filter(filter.includes).sorted { lhs, rhs in
            switch sort {
            case .date:
                if lhs.modified != rhs.modified { return lhs.modified > rhs.modified }
                if let order = nameOrder(lhs, rhs) { return order }
            case .name:
                if let order = nameOrder(lhs, rhs) { return order }
                if lhs.modified != rhs.modified { return lhs.modified > rhs.modified }
            case .type:
                if lhs.typeRank != rhs.typeRank { return lhs.typeRank < rhs.typeRank }
                if let order = nameOrder(lhs, rhs) { return order }
                if lhs.modified != rhs.modified { return lhs.modified > rhs.modified }
            }
            return lhs.id < rhs.id
        }
    }

    /// Favorites: the starred folders and documents at any depth, arranged by
    /// the current sort and filter.
    public static func favorites(
        folders: [PaperFolder], documents: [Notebook], sort: LibrarySort, filter: LibraryFilter
    ) -> [LibraryItem] {
        let items =
            folders.filter(\.favorite).map(LibraryItem.folder)
            + documents.filter(\.favorite).map(LibraryItem.document)
        return arrange(items, sort: sort, filter: filter)
    }

    /// How many documents Recents lists.
    public static let recentsLimit = 20

    /// Recents: the documents this device has opened, most recently opened
    /// first, at most `recentsLimit`. The filter applies; the sort does not.
    /// `opened` maps a notebook id to when it was last opened here
    /// (`PaperStore.openedDates()`); a document never opened is not recent.
    public static func recents(
        _ documents: [Notebook], opened: [String: Date], filter: LibraryFilter
    ) -> [LibraryItem] {
        let candidates = documents.compactMap { notebook -> (Notebook, Date)? in
            guard let date = opened[notebook.id] else { return nil }
            guard filter.includes(.document(notebook)) else { return nil }
            return (notebook, date)
        }
        return candidates.sorted { lhs, rhs in
            if lhs.1 != rhs.1 { return lhs.1 > rhs.1 }
            if lhs.0.modified != rhs.0.modified { return lhs.0.modified > rhs.0.modified }
            return lhs.0.id < rhs.0.id
        }
        .prefix(recentsLimit)
        .map { LibraryItem.document($0.0) }
    }

    /// The grey line under a row's name: `modified`, as
    /// `.dateTime.month(.abbreviated).day().year().hour().minute()`.
    public static func dateLine(
        _ date: Date, locale: Locale = .current, timeZone: TimeZone = .current
    ) -> String {
        var style = Date.FormatStyle.dateTime.month(.abbreviated).day().year().hour().minute()
        style.locale = locale
        style.timeZone = timeZone
        return date.formatted(style)
    }

    /// `nil` when the names compare the same.
    private static func nameOrder(_ lhs: LibraryItem, _ rhs: LibraryItem) -> Bool? {
        switch lhs.name.localizedStandardCompare(rhs.name) {
        case .orderedAscending: return true
        case .orderedDescending: return false
        case .orderedSame: return nil
        }
    }
}

// MARK: - Windows

/// What a window opened with "Open in New Window" shows: the library at a
/// folder (`nil` for the top level), or a document at an optional page. Its
/// JSON rides `NSUserActivity.userInfo["target"]`.
public enum PaperWindowTarget: Codable, Equatable, Sendable {
    case library(folderID: String?)
    case document(id: String, pageID: String?)
}
