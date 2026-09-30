// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "PaperCore",
    platforms: [.iOS(.v18), .macOS(.v15)],
    products: [
        .library(name: "PaperCore", targets: ["PaperCore"])
    ],
    dependencies: [
        .package(url: "https://github.com/groue/GRDB.swift.git", from: "7.0.0")
    ],
    targets: [
        .target(
            name: "PaperCore",
            dependencies: [.product(name: "GRDB", package: "GRDB.swift")],
            swiftSettings: [.swiftLanguageMode(.v6)]
        ),
        .testTarget(
            name: "PaperCoreTests",
            dependencies: ["PaperCore"],
            // Written by backend/tests/test_paper_wire_fixtures.py.
            resources: [.copy("Fixtures")],
            swiftSettings: [.swiftLanguageMode(.v6)]
        )
    ]
)
