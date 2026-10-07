// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "PaperUI",
    platforms: [.iOS(.v18)],
    products: [
        .library(name: "PaperUI", targets: ["PaperUI"])
    ],
    dependencies: [
        .package(path: "../PaperCore")
    ],
    targets: [
        .target(
            name: "PaperUI",
            dependencies: ["PaperCore"],
            swiftSettings: [.swiftLanguageMode(.v6)]
        )
    ]
)
