import PaperCore
import UIKit
import XCTest

@testable import PaperUI

/// The logic the New Notebook, New Whiteboard and New Folder sheets hold:
/// what a typed title becomes, the one-emoji rule of the folder icon, and the
/// choices a "Create" leaves behind for the next sheet.
@MainActor
final class NewDocumentSheetsTests: XCTestCase {
    private var scratch: ScratchDefaults?
    private var defaults = UserDefaults.standard

    override func setUp() {
        super.setUp()
        do {
            let scratch = try ScratchDefaults("NewDocumentSheetsTests")
            self.scratch = scratch
            defaults = scratch.defaults
        } catch {
            XCTFail("could not make a defaults suite")
        }
    }

    override func tearDown() {
        scratch?.remove()
        scratch = nil
        super.tearDown()
    }

    // MARK: - Titles

    func testTitleIsTrimmedAndEmptyReadsUntitled() {
        XCTAssertEqual(NewDocumentTitle.resolved("  Midterm Chapter 2 \n"), "Midterm Chapter 2")
        XCTAssertEqual(NewDocumentTitle.resolved(""), "Untitled")
        XCTAssertEqual(NewDocumentTitle.resolved(" \t\n "), "Untitled")
    }

    func testTitleTurnsLineBreaksAndTabsIntoSpaces() {
        XCTAssertEqual(NewDocumentTitle.resolved("Week\n12\tnotes"), "Week 12 notes")
        XCTAssertEqual(NewDocumentTitle.resolved("A\u{2028}B"), "A B")
    }

    func testTitleKeepsEmojiAndAccentsVerbatim() {
        XCTAssertEqual(NewDocumentTitle.resolved("Café 📒"), "Café 📒")
    }

    func testTitleIsCutAtTwoHundredScalarsOnACharacterBoundary() {
        let long = String(repeating: "a", count: 250)
        XCTAssertEqual(NewDocumentTitle.resolved(long).unicodeScalars.count, 200)

        // 198 letters and a three-scalar emoji: the emoji would make 201, so
        // it is left out whole rather than cut in half.
        let edge = String(repeating: "b", count: 198) + "👩‍💻"
        let resolved = NewDocumentTitle.resolved(edge)
        XCTAssertEqual(resolved, String(repeating: "b", count: 198))
        XCTAssertTrue(resolved.unicodeScalars.count <= NewDocumentTitle.maximumScalars)
    }

    // MARK: - The one-emoji rule

    func testSingleEmojiAcceptsOneEmojiOfAnyShape() {
        for emoji in ["📒", "❤️", "👍🏽", "👩‍💻", "🇩🇪", "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "👨‍👩‍👧‍👦"] {
            XCTAssertEqual(FolderEmoji.single(emoji), emoji, emoji)
            XCTAssertNotNil(FolderIcon(wire: "emoji:" + emoji), emoji)
        }
        XCTAssertEqual(FolderEmoji.single(" 📒 "), "📒")
    }

    func testSingleEmojiRefusesTextAndPairs() {
        for text in ["", " ", "a", "A", "é", "字", "ß", "©", "™", "❤", "1️⃣", "#", "📒📒", "📒a", "a📒", "📒 🎉"] {
            XCTAssertNil(FolderEmoji.single(text), "accepted \(text)")
        }
    }

    func testFieldRefusesTextAndKeepsWhatItHad() {
        XCTAssertEqual(FolderEmoji.filtered("a", current: ""), "")
        XCTAssertEqual(FolderEmoji.filtered("📒a", current: "📒"), "📒")
        XCTAssertEqual(FolderEmoji.filtered("📒 ", current: "📒"), "📒")
        XCTAssertEqual(FolderEmoji.filtered("hi🎉", current: ""), "")
    }

    func testASecondEmojiReplacesTheFirst() {
        XCTAssertEqual(FolderEmoji.filtered("📒🎉", current: "📒"), "🎉")
        XCTAssertEqual(FolderEmoji.filtered("🎉📒", current: "📒"), "🎉")
        XCTAssertEqual(FolderEmoji.filtered("🎉", current: ""), "🎉")
        XCTAssertEqual(FolderEmoji.filtered("🎉🎊", current: ""), "🎊")
        XCTAssertEqual(FolderEmoji.filtered("📒📒", current: "📒"), "📒")
    }

    func testClearingTheFieldClearsIt() {
        XCTAssertEqual(FolderEmoji.filtered("", current: "📒"), "")
        XCTAssertEqual(FolderEmoji.filtered("  ", current: "📒"), "")
    }

    func testEveryCuratedSymbolExistsAndIsNamed() {
        XCTAssertEqual(FolderIcon.symbolChoices.count, 24)
        for symbol in FolderIcon.symbolChoices {
            XCTAssertNotNil(UIImage(systemName: symbol), symbol)
            XCTAssertTrue(FolderIcon.isValidSymbol(symbol), symbol)
            XCTAssertFalse(FolderSymbolNames.name(for: symbol).contains("."), symbol)
        }
        XCTAssertNotNil(UIImage(systemName: "nosign"))
    }

    /// The folder is drawn, not a symbol: the back deeper than the front in
    /// every colour and appearance, the front over the bottom of the back
    /// with no gap between them, and nothing outside the frame.
    func testTheFolderIsTwoTonesOfItsColourWithNoGap() {
        for style in [UIUserInterfaceStyle.light, .dark] {
            let traits = UITraitCollection(userInterfaceStyle: style)
            for color in FolderColor.allCases {
                let front = color.uiColor.resolvedColor(with: traits)
                let back = UIColor(color.backSwatch).resolvedColor(with: traits)
                var frontValue: CGFloat = 0, backValue: CGFloat = 0
                var frontSaturation: CGFloat = 0, backSaturation: CGFloat = 0
                front.getHue(nil, saturation: &frontSaturation, brightness: &frontValue, alpha: nil)
                back.getHue(nil, saturation: &backSaturation, brightness: &backValue, alpha: nil)
                XCTAssertLessThan(backValue, frontValue, "\(color) \(style.rawValue)")
                XCTAssertGreaterThanOrEqual(backSaturation, frontSaturation, "\(color) \(style.rawValue)")
            }
        }
        let rect = CGRect(x: 0, y: 0, width: 100, height: 100 * FolderShape.aspect)
        let back = FolderShape(part: .back).path(in: rect).boundingRect
        let front = FolderShape(part: .front).path(in: rect).boundingRect
        XCTAssertTrue(rect.insetBy(dx: -0.01, dy: -0.01).contains(back))
        XCTAssertEqual(front.maxY, rect.maxY, accuracy: 0.01)
        XCTAssertEqual(front.width, rect.width, accuracy: 0.01)
        XCTAssertEqual(back.minY, rect.minY, accuracy: 0.01)
        XCTAssertGreaterThan(back.maxY, front.minY, "the back stops short of the front")
        // The tab is a little over a third of the width.
        let tabRow = CGPoint(x: rect.width * 0.3, y: rect.height * 0.04)
        let pastTab = CGPoint(x: rect.width * 0.6, y: rect.height * 0.04)
        XCTAssertTrue(FolderShape(part: .back).path(in: rect).contains(tabRow))
        XCTAssertFalse(FolderShape(part: .back).path(in: rect).contains(pastTab))
    }

    func testEveryFolderColorHasADistinctSystemColor() {
        for style in [UIUserInterfaceStyle.light, .dark] {
            let traits = UITraitCollection(userInterfaceStyle: style)
            let colors = Set(
                FolderColor.allCases.map { color -> String in
                    var red: CGFloat = 0, green: CGFloat = 0, blue: CGFloat = 0, alpha: CGFloat = 0
                    color.uiColor.resolvedColor(with: traits).getRed(
                        &red, green: &green, blue: &blue, alpha: &alpha)
                    return String(format: "%.3f %.3f %.3f", red, green, blue)
                })
            XCTAssertEqual(colors.count, 12)
        }
    }

    // MARK: - Options through defaults

    func testANewNotebookSheetOpensWithTheFactoryChoices() {
        let draft = NewDocumentDraft(kind: .notebook, defaults: defaults)
        XCTAssertEqual(draft.options, .notebookFactory)
        XCTAssertEqual(draft.title, "")
        XCTAssertEqual(draft.pageSize, PaperSize.iPad.points)
    }

    func testCreateRemembersTheNotebookChoicesButNotTheTitle() {
        var draft = NewDocumentDraft(kind: .notebook, defaults: defaults)
        draft.title = "  Plans "
        draft.options.cover = true
        draft.options.coverColor = .oxblood
        draft.options.template = .dotted
        draft.options.paperColor = .dark
        draft.options.size = .a4
        draft.options.orientation = .landscape
        XCTAssertEqual(draft.pageSize, CGSize(width: 842, height: 595))

        let result = draft.commit(defaults: defaults)
        XCTAssertEqual(result.title, "Plans")
        XCTAssertEqual(result.options, draft.options)

        let next = NewDocumentDraft(kind: .notebook, defaults: defaults)
        XCTAssertEqual(next.options, draft.options)
        XCTAssertEqual(next.title, "")
        // The whiteboard's choices are their own.
        XCTAssertEqual(NewDocumentDraft(kind: .whiteboard, defaults: defaults).options, .whiteboardFactory)
    }

    func testCreateRemembersTheWhiteboardChoices() {
        var draft = NewDocumentDraft(kind: .whiteboard, defaults: defaults)
        XCTAssertEqual(draft.options, .whiteboardFactory)
        draft.options.template = .dotted
        draft.options.paperColor = .dark
        let result = draft.commit(defaults: defaults)
        XCTAssertEqual(result.title, "Untitled")
        XCTAssertEqual(result.options.size, .board)
        XCTAssertFalse(result.options.cover)

        let next = NewDocumentDraft(kind: .whiteboard, defaults: defaults)
        XCTAssertEqual(next.options.template, .dotted)
        XCTAssertEqual(next.options.paperColor, .dark)
        XCTAssertEqual(NewDocumentDraft(kind: .notebook, defaults: defaults).options, .notebookFactory)
    }

    func testCreateHandsBackOnlyWhatTheSheetOffers() {
        var draft = NewDocumentDraft(kind: .whiteboard, defaults: defaults)
        draft.options.template = .ruled
        draft.options.cover = true
        draft.options.orientation = .landscape
        draft.options.paperColor = .navy
        let result = draft.commit(defaults: defaults)
        XCTAssertEqual(result.options, .whiteboardFactory)

        var notebook = NewDocumentDraft(kind: .notebook, defaults: defaults)
        notebook.options.size = .infinite
        notebook.options.template = .cover
        let made = notebook.commit(defaults: defaults)
        XCTAssertEqual(made.options.size, .iPad)
        XCTAssertEqual(made.options.template, .ruled)
    }

    func testALaunchArgumentStyleStringSetsTheNextSheet() {
        defaults.set(#"{"cover":true,"orientation":"landscape","paperColor":"cream"}"#, forKey: "paper.newNotebook.options")
        let draft = NewDocumentDraft(kind: .notebook, defaults: defaults)
        XCTAssertTrue(draft.options.cover)
        XCTAssertEqual(draft.options.orientation, .landscape)
        XCTAssertEqual(draft.options.paperColor, .cream)
        XCTAssertEqual(draft.options.template, .ruled)
    }

    // MARK: - Folder

    func testANewFolderIsBlueWithNoIcon() {
        let draft = FolderDraft()
        XCTAssertTrue(draft.isNew)
        XCTAssertEqual(draft.color, .blue)
        XCTAssertNil(draft.icon)
        let result = draft.commit()
        XCTAssertEqual(result.name, "Untitled")
        XCTAssertEqual(result.color, .blue)
        XCTAssertNil(result.icon)
    }

    func testEditingAFolderStartsFromItAndHandsBackTheEdits() {
        let folder = PaperFolder(name: "Accounting", color: .red, icon: .emoji("📒"))
        var draft = FolderDraft(editing: folder)
        XCTAssertFalse(draft.isNew)
        XCTAssertEqual(draft.name, "Accounting")
        XCTAssertEqual(draft.color, .red)
        XCTAssertEqual(draft.icon, .emoji("📒"))

        draft.name = " Accounting 2026 "
        draft.color = .orange
        draft.icon = .symbol("chart.bar")
        let result = draft.commit()
        XCTAssertEqual(result.name, "Accounting 2026")
        XCTAssertEqual(result.color, .orange)
        XCTAssertEqual(result.icon, .symbol("chart.bar"))
    }

    func testAnIconTheWireWouldRefuseIsDropped() {
        var draft = FolderDraft()
        draft.icon = .emoji("abc")
        XCTAssertNil(draft.commit().icon)
        draft.icon = .symbol("Not A Symbol")
        XCTAssertNil(draft.commit().icon)
    }
}
