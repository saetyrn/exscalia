import Foundation

/// A `multipart/form-data` body, built by hand.
///
/// `URLSession` has no multipart encoder, and the page PUT is the one request
/// whose exact bytes matter: the source ink, its render and its transcript all
/// have to arrive together so the server can land them on one revision. The
/// boundary is injectable so a test can assert the encoding byte for byte.
public struct MultipartBody: Sendable {
    /// One file part: `page`, `drawing`, `render` or `text`.
    public struct Part: Sendable, Equatable {
        public let name: String
        public let filename: String
        public let contentType: String
        public let data: Data

        public init(name: String, filename: String, contentType: String, data: Data) {
            self.name = name
            self.filename = filename
            self.contentType = contentType
            self.data = data
        }

        public static func json(name: String, data: Data) -> Part {
            Part(name: name, filename: "\(name).json", contentType: "application/json", data: data)
        }

        public static func png(name: String, data: Data) -> Part {
            Part(name: name, filename: "\(name).png", contentType: "image/png", data: data)
        }

        public static func binary(name: String, filename: String, data: Data) -> Part {
            Part(
                name: name, filename: filename, contentType: "application/octet-stream", data: data)
        }
    }

    public let boundary: String
    private var fields: [(String, String)] = []
    private var parts: [Part] = []

    public init(boundary: String = "exscalia-\(UUID().uuidString)") {
        self.boundary = boundary
    }

    /// Plain form fields, in the order they are added. `nil` values are simply
    /// absent, which is what FastAPI's `Form(default=None)` expects.
    public mutating func addField(_ name: String, _ value: String?) {
        guard let value else { return }
        fields.append((name, value))
    }

    public mutating func addPart(_ part: Part?) {
        guard let part else { return }
        parts.append(part)
    }

    public var contentType: String { "multipart/form-data; boundary=\(boundary)" }

    public var isEmpty: Bool { fields.isEmpty && parts.isEmpty }

    public func encoded() -> Data {
        var body = Data()
        for (name, value) in fields {
            body.append(Data("--\(boundary)\r\n".utf8))
            body.append(
                Data("Content-Disposition: form-data; name=\"\(name)\"\r\n\r\n".utf8))
            body.append(Data(value.utf8))
            body.append(Data("\r\n".utf8))
        }
        for part in parts {
            body.append(Data("--\(boundary)\r\n".utf8))
            let disposition =
                "Content-Disposition: form-data; name=\"\(part.name)\";"
                + " filename=\"\(part.filename)\"\r\n"
            body.append(Data(disposition.utf8))
            body.append(Data("Content-Type: \(part.contentType)\r\n\r\n".utf8))
            body.append(part.data)
            body.append(Data("\r\n".utf8))
        }
        body.append(Data("--\(boundary)--\r\n".utf8))
        return body
    }
}
