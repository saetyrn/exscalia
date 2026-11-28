import Foundation
import XCTest

@testable import PaperCore

final class LiveEventsTests: XCTestCase {
    func testABlankLineDispatchesTheEvent() {
        var parser = SSEParser()
        let events = parser.consume(
            text: "event: paper\ndata: {\"type\":\"paper\"}\n\n")

        XCTAssertEqual(events.count, 1)
        XCTAssertEqual(events.first?.event, "paper")
        XCTAssertEqual(events.first?.data, "{\"type\":\"paper\"}")
    }

    func testAnEventWithoutANameIsAMessage() {
        var parser = SSEParser()
        let events = parser.consume(text: "data: hello\n\n")

        XCTAssertEqual(events.first?.event, "message")
        XCTAssertEqual(events.first?.data, "hello")
    }

    func testDataLinesAccumulateWithNewlines() {
        var parser = SSEParser()
        let events = parser.consume(text: "event: paper\ndata: one\ndata: two\n\n")

        XCTAssertEqual(events.first?.data, "one\ntwo")
    }

    func testCommentsAndKeepalivesAreIgnored() {
        var parser = SSEParser()
        let events = parser.consume(text: ": keepalive\n\n: another\n\nevent: ready\ndata: {}\n\n")

        XCTAssertEqual(events.count, 1)
        XCTAssertEqual(events.first?.event, "ready")
    }

    func testCarriageReturnsFromTheWireAreStripped() {
        var parser = SSEParser()
        let events = parser.consume(text: "event: paper\r\ndata: {\"type\":\"paper\"}\r\n\r\n")

        XCTAssertEqual(events.first?.event, "paper")
        XCTAssertEqual(events.first?.data, "{\"type\":\"paper\"}")
    }

    func testAFieldWithNoValueAndNoSpaceStillParses() {
        var parser = SSEParser()
        let events = parser.consume(text: "event:paper\ndata:x\n\n")

        XCTAssertEqual(events.first?.event, "paper")
        XCTAssertEqual(events.first?.data, "x")
    }

    func testAnIdIsCarriedAndResetBetweenEvents() {
        var parser = SSEParser()
        let events = parser.consume(text: "id: 7\ndata: a\n\ndata: b\n\n")

        XCTAssertEqual(events.count, 2)
        XCTAssertEqual(events.first?.id, "7")
        XCTAssertNil(events.last?.id)
    }

    func testAnEventIsHeldUntilItsBlankLineArrives() {
        var parser = SSEParser()
        XCTAssertNil(parser.consume(line: "event: paper"))
        XCTAssertNil(parser.consume(line: "data: half"))

        let event = parser.consume(line: "")

        XCTAssertEqual(event?.event, "paper")
        XCTAssertEqual(event?.data, "half")
    }

    func testThePaperPayloadDecodesAndNamesItsOrigin() {
        var parser = SSEParser()
        let events = parser.consume(
            text: """
                event: paper
                data: {"type":"paper","path":"paper/Work/Ideas.paper/pages/abc","revision":4,"origin":"ipad-9"}

                """ + "\n")

        guard let paper = events.first?.paperEvent else {
            return XCTFail("expected a decoded paper event")
        }
        XCTAssertEqual(paper.type, "paper")
        XCTAssertEqual(paper.revision, 4)
        XCTAssertEqual(paper.origin, "ipad-9")
        XCTAssertEqual(paper.path, "paper/Work/Ideas.paper/pages/abc")
    }

    func testANonPaperEventHasNoPaperPayload() {
        var parser = SSEParser()
        let events = parser.consume(text: "event: note\ndata: {\"type\":\"note\"}\n\n")

        XCTAssertNil(events.first?.paperEvent)
    }
}
