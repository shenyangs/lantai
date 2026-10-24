import AppKit
import Foundation
import PDFKit
import Vision

struct OcrRequest: Decodable {
    let path: String
    let pages: [Int]
    let languages: [String]?
}

struct OcrLine: Encodable {
    let text: String
    let confidence: Float
    let bbox: [Double]
}

struct OcrPage: Encodable {
    let pageIndex: Int
    let width: Double
    let height: Double
    let lines: [OcrLine]
}

struct OcrResponse: Encodable {
    let ok: Bool
    let pages: [OcrPage]?
    let error: String?
}

enum OcrFailure: Error, LocalizedError {
    case invalidInput(String)

    var errorDescription: String? {
        switch self {
        case .invalidInput(let message): return message
        }
    }
}

func render(_ page: PDFPage, scale: CGFloat = 2.0) throws -> CGImage {
    let bounds = page.bounds(for: .mediaBox)
    let width = max(1, Int(ceil(bounds.width * scale)))
    let height = max(1, Int(ceil(bounds.height * scale)))
    guard let context = CGContext(
        data: nil,
        width: width,
        height: height,
        bitsPerComponent: 8,
        bytesPerRow: 0,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ) else {
        throw OcrFailure.invalidInput("Unable to create the OCR render surface")
    }
    context.setFillColor(NSColor.white.cgColor)
    context.fill(CGRect(x: 0, y: 0, width: width, height: height))
    context.saveGState()
    context.scaleBy(x: scale, y: scale)
    page.draw(with: .mediaBox, to: context)
    context.restoreGState()
    guard let image = context.makeImage() else {
        throw OcrFailure.invalidInput("Unable to render the PDF page")
    }
    return image
}

func recognize(_ image: CGImage, preferredLanguages: [String]) throws -> [OcrLine] {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    let supported = try VNRecognizeTextRequest.supportedRecognitionLanguages(
        for: .accurate,
        revision: request.revision
    )
    let selected = preferredLanguages.filter { supported.contains($0) }
    if !selected.isEmpty { request.recognitionLanguages = selected }
    try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
    let observations = (request.results ?? []).sorted {
        let left = $0.boundingBox
        let right = $1.boundingBox
        if abs(left.maxY - right.maxY) > 0.01 { return left.maxY > right.maxY }
        return left.minX < right.minX
    }
    return observations.compactMap { observation in
        guard let candidate = observation.topCandidates(1).first else { return nil }
        let box = observation.boundingBox
        return OcrLine(
            text: candidate.string,
            confidence: candidate.confidence,
            bbox: [box.minX, box.minY, box.width, box.height].map(Double.init)
        )
    }
}

func run() throws -> OcrResponse {
    let input = FileHandle.standardInput.readDataToEndOfFile()
    let request = try JSONDecoder().decode(OcrRequest.self, from: input)
    guard request.pages.count <= 10 else {
        throw OcrFailure.invalidInput("At most 10 pages can be recognized per request")
    }
    let url = URL(fileURLWithPath: request.path)
    guard let document = PDFDocument(url: url) else {
        throw OcrFailure.invalidInput("The PDF could not be opened")
    }
    let languages = request.languages ?? ["zh-Hans", "zh-Hant", "en-US"]
    var output: [OcrPage] = []
    for pageIndex in request.pages {
        guard pageIndex >= 0, pageIndex < document.pageCount,
              let page = document.page(at: pageIndex) else {
            throw OcrFailure.invalidInput("Page index \(pageIndex) is out of range")
        }
        let bounds = page.bounds(for: .mediaBox)
        let lines = try recognize(try render(page), preferredLanguages: languages)
        output.append(OcrPage(
            pageIndex: pageIndex,
            width: Double(bounds.width),
            height: Double(bounds.height),
            lines: lines
        ))
    }
    return OcrResponse(ok: true, pages: output, error: nil)
}

let response: OcrResponse
do {
    response = try run()
} catch {
    response = OcrResponse(ok: false, pages: nil, error: error.localizedDescription)
}
let encoded = try JSONEncoder().encode(response)
FileHandle.standardOutput.write(encoded)
FileHandle.standardOutput.write(Data("\n".utf8))
