import Foundation
import AppKit
import ApplicationServices
import CoreGraphics
import Darwin

private let protocolVersion = 1
private let maximumRequestBytes = 512 * 1024
private let maximumTextCharacters = 100_000
private let maximumElements = 500

private struct Arguments {
    var requestPath = ""
    var responsePath = ""
}

private struct WindowInfo {
    let id: Int
    let pid: pid_t
    let processStartedAt: Double
    let owner: String
    let title: String
    let layer: Int
    let onScreen: Bool
    let bounds: CGRect
    let bundleID: String
    let appPath: String
}

private func parseArguments() -> Arguments {
    var result = Arguments()
    var index = 1
    let args = CommandLine.arguments
    while index < args.count {
        switch args[index] {
        case "--request" where index + 1 < args.count:
            index += 1
            result.requestPath = args[index]
        case "--response" where index + 1 < args.count:
            index += 1
            result.responsePath = args[index]
        default:
            break
        }
        index += 1
    }
    return result
}

private let arguments = parseArguments()

private func emit(_ payload: [String: Any], exitCode: Int32 = 0) -> Never {
    var final = payload
    final["protocol_version"] = protocolVersion
    guard JSONSerialization.isValidJSONObject(final),
          let data = try? JSONSerialization.data(withJSONObject: final, options: []) else {
        exit(70)
    }
    if !arguments.responsePath.isEmpty {
        do {
            let responseURL = URL(fileURLWithPath: arguments.responsePath)
            let directory = responseURL.deletingLastPathComponent()
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            let temporary = directory.appendingPathComponent(".result.\(UUID().uuidString).json")
            try data.write(to: temporary, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: temporary.path)
            if FileManager.default.fileExists(atPath: responseURL.path) {
                _ = try FileManager.default.replaceItemAt(responseURL, withItemAt: temporary)
            } else {
                try FileManager.default.moveItem(at: temporary, to: responseURL)
            }
        } catch {
            exit(74)
        }
    } else {
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([0x0A]))
    }
    exit(exitCode)
}

private func baseResult(_ request: [String: Any]) -> [String: Any] {
    [
        "action": request["action"] as? String ?? "status",
        "run_id": request["run_id"] as? String ?? "",
        "action_id": request["action_id"] as? String ?? "",
        "effect_id": request["effect_id"] as? String ?? "",
        "policy_status": "allowed",
    ]
}

private func fail(
    _ request: [String: Any],
    code: String,
    message: String,
    dispatch: String = "not_sent",
    effect: String = "failed",
    retrySafe: Bool = true,
    exitCode: Int32 = 2
) -> Never {
    var result = baseResult(request)
    result.merge([
        "ok": false,
        "dispatch_status": dispatch,
        "effect_status": effect,
        "error_code": code,
        "error": message,
        "retry_safe": retrySafe,
        "data": [:],
        "evidence": [],
    ]) { _, new in new }
    emit(result, exitCode: exitCode)
}

private func readRequest() -> [String: Any] {
    let data: Data?
    if !arguments.requestPath.isEmpty {
        guard let attributes = try? FileManager.default.attributesOfItem(atPath: arguments.requestPath),
              let size = attributes[.size] as? NSNumber,
              size.intValue <= maximumRequestBytes else {
            emit(["ok": false, "error_code": "invalid_request", "error": "Request file is missing or too large"], exitCode: 65)
        }
        data = FileManager.default.contents(atPath: arguments.requestPath)
    } else {
        let input = FileHandle.standardInput.readDataToEndOfFile()
        data = input.count <= maximumRequestBytes ? input : nil
    }
    guard let data,
          let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
        emit(["ok": false, "error_code": "invalid_request", "error": "Request is missing, too large, or invalid JSON"], exitCode: 65)
    }
    return object
}

private func trustedElectronParent() -> Bool {
    let parentPID = getppid()
    guard parentPID > 1,
          let parent = NSRunningApplication(processIdentifier: parentPID),
          let parentPath = parent.executableURL?.standardizedFileURL.path else {
        return false
    }
    let helperPath = Bundle.main.bundleURL.standardizedFileURL.path
    let packagedMarker = "/Contents/Resources/mac-use/Lantai Mac Use Helper.app"
    if let range = helperPath.range(of: packagedMarker) {
        let outerApp = String(helperPath[..<range.lowerBound])
        return parent.bundleIdentifier == "com.lantai.app"
            && parentPath.hasPrefix(outerApp + "/Contents/MacOS/")
    }
    let developmentMarker = "/electron-dist/mac-use/Lantai Mac Use Helper.app"
    if let range = helperPath.range(of: developmentMarker) {
        let consoleRoot = String(helperPath[..<range.lowerBound])
        let expected = consoleRoot
            + "/node_modules/electron/dist/Electron.app/Contents/MacOS/Electron"
        return ["com.github.Electron", "org.electronjs.Electron"].contains(
            parent.bundleIdentifier ?? ""
        ) && parentPath == expected
    }
    return false
}

private func application(for pid: pid_t) -> NSRunningApplication? {
    NSRunningApplication(processIdentifier: pid)
}

private func screenLocked() -> Bool? {
    guard let state = CGSessionCopyCurrentDictionary() as? [String: Any] else {
        return nil
    }
    func boolean(_ key: String) -> Bool? {
        if let value = state[key] as? Bool { return value }
        if let value = state[key] as? NSNumber { return value.boolValue }
        return nil
    }
    if let locked = boolean("CGSSessionScreenIsLocked") {
        return locked
    }
    let userID = (state["kCGSSessionUserIDKey"] as? NSNumber)?.uint32Value
        ?? state["kCGSSessionUserIDKey"] as? UInt32
    guard boolean("kCGSSessionOnConsoleKey") == true,
          boolean("kCGSessionLoginDoneKey") == true,
          userID == getuid() else {
        return nil
    }
    // On an active console session macOS omits CGSSessionScreenIsLocked when
    // the screen is unlocked. The three checks above prevent treating an
    // incomplete or non-console session dictionary as proof of that state.
    return false
}

private func writeAllowedWhenScreenLockState(_ state: Bool?) -> Bool {
    state == false
}

private func allWindows() -> [WindowInfo] {
    let options: CGWindowListOption = [.optionAll, .excludeDesktopElements]
    let rows = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] ?? []
    return rows.compactMap { row in
        guard let id = row[kCGWindowNumber as String] as? Int,
              let pidValue = row[kCGWindowOwnerPID as String] as? Int,
              let boundsData = row[kCGWindowBounds as String] as? [String: Any],
              let bounds = CGRect(dictionaryRepresentation: boundsData as CFDictionary) else {
            return nil
        }
        let pid = pid_t(pidValue)
        let running = application(for: pid)
        return WindowInfo(
            id: id,
            pid: pid,
            processStartedAt: running?.launchDate?.timeIntervalSince1970 ?? 0,
            owner: row[kCGWindowOwnerName as String] as? String ?? "",
            title: row[kCGWindowName as String] as? String ?? "",
            layer: row[kCGWindowLayer as String] as? Int ?? 0,
            onScreen: row[kCGWindowIsOnscreen as String] as? Bool ?? false,
            bounds: bounds,
            bundleID: running?.bundleIdentifier ?? "",
            appPath: running?.bundleURL?.path ?? ""
        )
    }
}

private func publicWindow(_ window: WindowInfo) -> [String: Any] {
    [
        "window_id": String(window.id),
        "process_id": Int(window.pid),
        "process_started_at": window.processStartedAt,
        "owner_name": window.owner,
        "window_title": window.title,
        "app_bundle_id": window.bundleID,
        "app_path": window.appPath,
        "on_screen": window.onScreen,
        "bounds": [
            "x": window.bounds.origin.x,
            "y": window.bounds.origin.y,
            "width": window.bounds.width,
            "height": window.bounds.height,
        ],
    ]
}

private func targetPayload(_ request: [String: Any]) -> [String: Any] {
    request["target"] as? [String: Any] ?? [:]
}

private func targetWindow(_ request: [String: Any]) -> WindowInfo? {
    let target = targetPayload(request)
    guard let id = Int(target["window_id"] as? String ?? "") else { return nil }
    let expectedBundle = target["app_bundle_id"] as? String ?? ""
    let expectedPID = (target["process_id"] as? NSNumber)?.intValue
        ?? target["process_id"] as? Int
        ?? 0
    let expectedStartedAt = (target["process_started_at"] as? NSNumber)?.doubleValue
        ?? target["process_started_at"] as? Double
        ?? 0
    return allWindows().first {
        $0.id == id
            && !expectedBundle.isEmpty
            && $0.bundleID == expectedBundle
            && expectedPID > 0
            && Int($0.pid) == expectedPID
            && expectedStartedAt > 0
            && abs($0.processStartedAt - expectedStartedAt) < 0.001
    }
}

private func observationWindowIsCurrent(_ request: [String: Any], window: WindowInfo) -> Bool {
    guard let observed = request["observation_target"] as? [String: Any],
          let bounds = observed["bounds"] as? [String: Any] else { return true }
    func number(_ key: String) -> Double? {
        if let value = bounds[key] as? Double { return value }
        if let value = bounds[key] as? NSNumber { return value.doubleValue }
        return nil
    }
    guard let x = number("x"), let y = number("y"),
          let width = number("width"), let height = number("height") else { return false }
    return abs(window.bounds.origin.x - x) <= 1
        && abs(window.bounds.origin.y - y) <= 1
        && abs(window.bounds.width - width) <= 1
        && abs(window.bounds.height - height) <= 1
}

private func isCancelled(_ request: [String: Any]) -> Bool {
    guard let path = request["cancel_path"] as? String, !path.isEmpty else { return false }
    return FileManager.default.fileExists(atPath: path)
}

private func checkCancelled(
    _ request: [String: Any],
    afterDispatch: Bool = false
) {
    if isCancelled(request) {
        fail(
            request,
            code: "cancelled",
            message: afterDispatch
                ? "Mac Use stopped after part of the action may have reached the target."
                : "Mac Use session was stopped before input was sent.",
            dispatch: afterDispatch ? "sent" : "not_sent",
            effect: afterDispatch ? "ambiguous" : "failed",
            retrySafe: !afterDispatch
        )
    }
}

private func axAttribute(_ element: AXUIElement, _ name: String) -> Any? {
    var value: CFTypeRef?
    return AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success ? value : nil
}

private func axChildren(_ element: AXUIElement) -> [AXUIElement] {
    axAttribute(element, kAXChildrenAttribute as String) as? [AXUIElement] ?? []
}

private func axPoint(_ value: Any?) -> CGPoint? {
    guard let value else { return nil }
    let reference = value as CFTypeRef
    guard CFGetTypeID(reference) == AXValueGetTypeID() else { return nil }
    let axValue = value as! AXValue
    guard AXValueGetType(axValue) == .cgPoint else { return nil }
    var point = CGPoint.zero
    return AXValueGetValue(axValue, .cgPoint, &point) ? point : nil
}

private func axSize(_ value: Any?) -> CGSize? {
    guard let value else { return nil }
    let reference = value as CFTypeRef
    guard CFGetTypeID(reference) == AXValueGetTypeID() else { return nil }
    let axValue = value as! AXValue
    guard AXValueGetType(axValue) == .cgSize else { return nil }
    var size = CGSize.zero
    return AXValueGetValue(axValue, .cgSize, &size) ? size : nil
}

private func dictionaryNumber(_ object: [String: Any], _ key: String) -> Double? {
    if let value = object[key] as? Double { return value }
    if let value = object[key] as? NSNumber { return value.doubleValue }
    return nil
}

private func rectFromDictionary(_ object: [String: Any]) -> CGRect? {
    guard let x = dictionaryNumber(object, "x"),
          let y = dictionaryNumber(object, "y"),
          let width = dictionaryNumber(object, "width"),
          let height = dictionaryNumber(object, "height") else {
        return nil
    }
    return CGRect(x: x, y: y, width: width, height: height)
}

private func uniqueGeometryMatchIndex(
    _ candidates: [CGRect?],
    target: CGRect,
    tolerance: CGFloat = 2
) -> Int? {
    let matches = candidates.indices.filter { index in
        guard let candidate = candidates[index] else { return false }
        return abs(candidate.origin.x - target.origin.x) <= tolerance
            && abs(candidate.origin.y - target.origin.y) <= tolerance
            && abs(candidate.width - target.width) <= tolerance
            && abs(candidate.height - target.height) <= tolerance
    }
    return matches.count == 1 ? matches[0] : nil
}

private func axWindow(for window: WindowInfo) -> AXUIElement? {
    let app = AXUIElementCreateApplication(window.pid)
    guard let windows = axAttribute(app, kAXWindowsAttribute as String) as? [AXUIElement] else { return nil }
    for candidate in windows {
        if let number = axAttribute(candidate, "AXWindowNumber") as? NSNumber,
           number.intValue == window.id {
            return candidate
        }
    }
    let geometries = windows.map { candidate -> CGRect? in
        guard let point = axPoint(axAttribute(candidate, kAXPositionAttribute as String)),
              let size = axSize(axAttribute(candidate, kAXSizeAttribute as String)) else {
            return nil
        }
        return CGRect(origin: point, size: size)
    }
    if let match = uniqueGeometryMatchIndex(geometries, target: window.bounds) {
        return windows[match]
    }
    return nil
}

private let actionableRoles: Set<String> = [
    "AXButton", "AXCheckBox", "AXComboBox", "AXDisclosureTriangle", "AXLink",
    "AXMenuButton", "AXPopUpButton", "AXRadioButton", "AXScrollArea", "AXSlider",
    "AXTab", "AXTextArea", "AXTextField",
]

private func isSensitiveElement(
    role: String,
    subrole: String,
    containsProtectedContent: Bool = false
) -> Bool {
    role == "AXSecureTextField"
        || subrole == "AXSecureTextField"
        || containsProtectedContent
}

private func stringValue(_ value: Any?, maximum: Int = 240) -> String {
    guard let value else { return "" }
    let text = value as? String ?? String(describing: value)
    return String(text.prefix(maximum))
}

private func collectElements(_ root: AXUIElement) -> [[String: Any]] {
    var result: [[String: Any]] = []
    func walk(_ element: AXUIElement, path: [Int], depth: Int) {
        guard result.count < maximumElements, depth <= 24 else { return }
        let role = stringValue(axAttribute(element, kAXRoleAttribute as String), maximum: 60)
        if actionableRoles.contains(role) {
            let subrole = stringValue(
                axAttribute(element, kAXSubroleAttribute as String),
                maximum: 80
            )
            let title = stringValue(
                axAttribute(element, kAXTitleAttribute as String)
                    ?? axAttribute(element, kAXDescriptionAttribute as String)
                    ?? axAttribute(element, "AXPlaceholderValue")
            )
            let protectedValue = axAttribute(element, "AXContainsProtectedContent")
            let containsProtectedContent = (protectedValue as? NSNumber)?.boolValue
                ?? protectedValue as? Bool
                ?? false
            let sensitive = isSensitiveElement(
                role: role,
                subrole: subrole,
                containsProtectedContent: containsProtectedContent
            )
            let point = axPoint(axAttribute(element, kAXPositionAttribute as String))
            let size = axSize(axAttribute(element, kAXSizeAttribute as String))
            result.append([
                "element_id": path.map(String.init).joined(separator: "."),
                "role": role,
                "subrole": subrole,
                "title": title,
                "identifier": stringValue(
                    axAttribute(element, "AXIdentifier"),
                    maximum: 240
                ),
                "value": sensitive ? "" : stringValue(axAttribute(element, kAXValueAttribute as String)),
                "sensitive": sensitive,
                "enabled": axAttribute(element, kAXEnabledAttribute as String) as? Bool ?? true,
                "focused": axAttribute(element, kAXFocusedAttribute as String) as? Bool ?? false,
                "bounds": [
                    "x": point?.x ?? 0,
                    "y": point?.y ?? 0,
                    "width": size?.width ?? 0,
                    "height": size?.height ?? 0,
                ],
            ])
        }
        for (index, child) in axChildren(element).enumerated() {
            walk(child, path: path + [index], depth: depth + 1)
            if result.count >= maximumElements { return }
        }
    }
    walk(root, path: [], depth: 0)
    return result
}

private func resolveElement(_ root: AXUIElement, id: String) -> AXUIElement? {
    if id.isEmpty { return nil }
    var current = root
    for part in id.split(separator: ".") {
        guard let index = Int(part) else { return nil }
        let children = axChildren(current)
        guard index >= 0, index < children.count else { return nil }
        current = children[index]
    }
    return current
}

private func validateElement(_ element: AXUIElement, request: [String: Any]) -> Bool {
    guard let fingerprint = request["element_fingerprint"] as? [String: Any],
          let expectedRole = fingerprint["role"] as? String,
          !expectedRole.isEmpty,
          let expectedSubrole = fingerprint["subrole"] as? String,
          let expectedTitle = fingerprint["title"] as? String,
          let expectedIdentifier = fingerprint["identifier"] as? String,
          let expectedBounds = fingerprint["bounds"] as? [String: Any],
          fingerprint["element_id"] as? String == request["element_id"] as? String else {
        return false
    }
    func number(_ key: String) -> Double? {
        if let value = expectedBounds[key] as? Double { return value }
        if let value = expectedBounds[key] as? NSNumber { return value.doubleValue }
        return nil
    }
    guard let expectedX = number("x"),
          let expectedY = number("y"),
          let expectedWidth = number("width"),
          let expectedHeight = number("height") else {
        return false
    }
    let role = stringValue(axAttribute(element, kAXRoleAttribute as String), maximum: 60)
    let subrole = stringValue(
        axAttribute(element, kAXSubroleAttribute as String),
        maximum: 80
    )
    let title = stringValue(
        axAttribute(element, kAXTitleAttribute as String)
            ?? axAttribute(element, kAXDescriptionAttribute as String)
            ?? axAttribute(element, "AXPlaceholderValue")
    )
    let identifier = stringValue(axAttribute(element, "AXIdentifier"), maximum: 240)
    let point = axPoint(axAttribute(element, kAXPositionAttribute as String))
    let size = axSize(axAttribute(element, kAXSizeAttribute as String))
    guard let point, let size else { return false }
    return expectedRole == role
        && expectedSubrole == subrole
        && expectedTitle == title
        && expectedIdentifier == identifier
        && abs(point.x - expectedX) <= 1
        && abs(point.y - expectedY) <= 1
        && abs(size.width - expectedWidth) <= 1
        && abs(size.height - expectedHeight) <= 1
}

private func acquireInputLock(_ request: [String: Any]) -> Int32 {
    guard let path = request["input_lock_path"] as? String, !path.isEmpty else {
        fail(request, code: "input_lock_missing", message: "Mac Use input lock was not configured.")
    }
    let descriptor = open(path, O_CREAT | O_RDWR, S_IRUSR | S_IWUSR)
    guard descriptor >= 0 else {
        fail(request, code: "input_lock_failed", message: "Mac Use input lock could not be opened.")
    }
    guard flock(descriptor, LOCK_EX | LOCK_NB) == 0 else {
        close(descriptor)
        fail(request, code: "input_busy", message: "Another Mac Use action currently owns shared input.")
    }
    return descriptor
}

private func syntheticTrail(_ request: [String: Any]) -> [String: Double] {
    guard let path = request["synthetic_trail_path"] as? String,
          let data = FileManager.default.contents(atPath: path),
          let object = try? JSONSerialization.jsonObject(with: data) as? [String: Double] else {
        return [:]
    }
    return object
}

private func markSynthetic(_ request: [String: Any], kind: String) {
    guard let path = request["synthetic_trail_path"] as? String, !path.isEmpty else { return }
    var trail = syntheticTrail(request)
    trail[kind] = Date().timeIntervalSince1970
    guard let data = try? JSONSerialization.data(withJSONObject: trail, options: []) else { return }
    try? data.write(to: URL(fileURLWithPath: path), options: .atomic)
    try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path)
}

private func userIdleSeconds(
    _ request: [String: Any],
    syntheticOverride: [String: Double] = [:]
) -> Double {
    let trail = syntheticTrail(request).merging(syntheticOverride) { _, latest in latest }
    let now = Date().timeIntervalSince1970
    let signals: [(String, Double)] = [
        ("key", CGEventSource.secondsSinceLastEventType(.hidSystemState, eventType: .keyDown)),
        ("flags", CGEventSource.secondsSinceLastEventType(.hidSystemState, eventType: .flagsChanged)),
        ("move", CGEventSource.secondsSinceLastEventType(.hidSystemState, eventType: .mouseMoved)),
        ("left-drag", CGEventSource.secondsSinceLastEventType(.hidSystemState, eventType: .leftMouseDragged)),
        ("right-drag", CGEventSource.secondsSinceLastEventType(.hidSystemState, eventType: .rightMouseDragged)),
        ("other-drag", CGEventSource.secondsSinceLastEventType(.hidSystemState, eventType: .otherMouseDragged)),
        ("click", CGEventSource.secondsSinceLastEventType(.hidSystemState, eventType: .leftMouseDown)),
        ("right-click", CGEventSource.secondsSinceLastEventType(.hidSystemState, eventType: .rightMouseDown)),
        ("other-click", CGEventSource.secondsSinceLastEventType(.hidSystemState, eventType: .otherMouseDown)),
        ("scroll", CGEventSource.secondsSinceLastEventType(.hidSystemState, eventType: .scrollWheel)),
    ]
    let cleaned = signals.compactMap { signal -> Double? in
        let (kind, idle) = signal
        guard let timestamp = trail[kind] else { return idle }
        let age = now - timestamp
        if age >= 0 && age < 10 && abs(age - idle) < 0.15 { return nil }
        return idle
    }
    return cleaned.min() ?? 3600
}

private func requireUserIdle(
    _ request: [String: Any],
    syntheticOverride: [String: Double] = [:],
    afterDispatch: Bool = false
) {
    let required = request["required_idle_seconds"] as? Double ?? 2.0
    let idle = userIdleSeconds(request, syntheticOverride: syntheticOverride)
    if idle < required {
        fail(
            request,
            code: "user_active",
            message: String(format: "The user used the keyboard or pointer %.2f seconds ago.", idle),
            dispatch: afterDispatch ? "sent" : "not_sent",
            effect: afterDispatch ? "ambiguous" : "failed",
            retrySafe: !afterDispatch
        )
    }
}

private func showActionIndicator(owner: String) -> [NSPanel] {
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    var panels: [NSPanel] = []
    for (index, screen) in NSScreen.screens.enumerated() {
        let panel = NSPanel(
            contentRect: screen.frame,
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.level = .screenSaver
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = false
        panel.ignoresMouseEvents = true
        panel.sharingType = .none
        panel.collectionBehavior = [
            .canJoinAllSpaces,
            .stationary,
            .fullScreenAuxiliary,
            .ignoresCycle,
        ]
        let content = NSView(frame: NSRect(origin: .zero, size: screen.frame.size))
        content.wantsLayer = true
        content.layer?.borderWidth = 4
        content.layer?.borderColor = NSColor.systemOrange.withAlphaComponent(0.9).cgColor
        content.layer?.cornerRadius = 10
        if index == 0 {
            let label = NSTextField(labelWithString: "澜台正在操作“\(owner)”")
            label.font = NSFont.systemFont(ofSize: 14, weight: .semibold)
            label.textColor = .white
            label.backgroundColor = NSColor.black.withAlphaComponent(0.78)
            label.drawsBackground = true
            label.isBezeled = false
            label.alignment = .center
            label.sizeToFit()
            let width = min(max(label.frame.width + 28, 180), screen.frame.width - 40)
            label.frame = NSRect(
                x: (screen.frame.width - width) / 2,
                y: screen.frame.height - 62,
                width: width,
                height: 30
            )
            label.wantsLayer = true
            label.layer?.cornerRadius = 15
            label.layer?.masksToBounds = true
            content.addSubview(label)
        }
        panel.contentView = content
        panel.orderFrontRegardless()
        panels.append(panel)
    }
    RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.08))
    return panels
}

private func activate(_ window: WindowInfo, request: [String: Any]) {
    checkCancelled(request)

    if !observationWindowIsCurrent(request, window: window) {
        fail(request, code: "stale_observation", message: "The target window moved or resized after it was observed.")
    }
    guard let app = application(for: window.pid) else {
        fail(request, code: "stale_target", message: "The target application is no longer running.")
    }
    if !app.isActive {
        _ = app.activate(options: [.activateAllWindows])
        let deadline = Date().addingTimeInterval(1.5)
        while !app.isActive && Date() < deadline {
            RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.01))
        }
    }
    guard app.isActive else {
        fail(request, code: "focus_failed", message: "The target application did not become active.")
    }
    checkCancelled(request, afterDispatch: true)
}

private func topWindow(at point: CGPoint) -> WindowInfo? {
    allWindows().first {
        $0.layer == 0 && $0.onScreen && $0.bounds.width > 1 && $0.bounds.height > 1 && $0.bounds.contains(point)
    }
}

private func postMouseClick(_ point: CGPoint, request: [String: Any]) {
    let source = CGEventSource(stateID: .privateState)
    for type in [CGEventType.leftMouseDown, .leftMouseUp] {
        let event = CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: point, mouseButton: .left)
        event?.setIntegerValueField(.mouseEventClickState, value: 1)
        event?.post(tap: .cghidEventTap)
        usleep(35_000)
    }
    markSynthetic(request, kind: "click")
}

private func postText(
    _ text: String,
    request: [String: Any],
    effectAlreadyDispatched: Bool = false
) {
    let source = CGEventSource(stateID: .privateState)
    let characters = Array(text.prefix(maximumTextCharacters))
    var lastSyntheticKeyAt: Double?
    var hasDispatched = effectAlreadyDispatched
    for offset in stride(from: 0, to: characters.count, by: 12) {
        checkCancelled(request, afterDispatch: hasDispatched)
        requireUserIdle(
            request,
            syntheticOverride: lastSyntheticKeyAt.map { ["key": $0] } ?? [:],
            afterDispatch: hasDispatched
        )
        var units = Array(String(characters[offset..<min(offset + 12, characters.count)]).utf16)
        for down in [true, false] {
            let event = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: down)
            event?.keyboardSetUnicodeString(stringLength: units.count, unicodeString: &units)
            event?.post(tap: .cghidEventTap)
        }
        lastSyntheticKeyAt = Date().timeIntervalSince1970
        hasDispatched = true
        usleep(8_000)
    }
    markSynthetic(request, kind: "key")
}

private let keyCodes: [String: CGKeyCode] = [
    "enter": 36, "return": 36, "tab": 48, "space": 49, "backspace": 51,
    "delete": 51, "escape": 53, "left": 123, "right": 124, "down": 125, "up": 126,
]

private let terminalOrIDEBundleIDs: Set<String> = [
    "com.apple.terminal",
    "com.googlecode.iterm2",
    "dev.warp.warp-stable",
    "dev.warp.warp-preview",
    "com.mitchellh.ghostty",
    "com.github.wez.wezterm",
    "net.kovidgoyal.kitty",
    "org.alacritty",
    "io.alacritty",
    "co.zeit.hyper",
    "com.microsoft.vscode",
    "com.microsoft.vscodeinsiders",
    "com.todesktop.230313mzl4w4u92",
    "com.apple.dt.xcode",
    "com.exafunction.windsurf",
]

private let terminalOrIDEOwners: Set<String> = [
    "terminal", "终端", "iterm2", "iterm", "warp", "ghostty", "wezterm",
    "kitty", "alacritty", "hyper", "cursor", "code", "visual studio code",
    "visual studio code - insiders", "xcode", "windsurf", "codex", "claude",
]

private func isTerminalOrIDE(bundleID: String, owner: String) -> Bool {
    let normalizedBundle = bundleID.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    let normalizedOwner = owner.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    return terminalOrIDEBundleIDs.contains(normalizedBundle)
        || terminalOrIDEOwners.contains(normalizedOwner)
        || normalizedBundle.hasPrefix("dev.warp.")
        || normalizedBundle.hasPrefix("com.microsoft.vscode.")
}

private func containsSubmissionSeparator(_ text: String) -> Bool {
    text.unicodeScalars.contains {
        [0x0A, 0x0D, 0x85, 0x2028, 0x2029].contains($0.value)
    }
}

private func postKey(_ key: String, request: [String: Any]) -> Bool {
    guard let code = keyCodes[key.lowercased()] else { return false }
    let source = CGEventSource(stateID: .privateState)
    for down in [true, false] {
        let event = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: down)
        event?.post(tap: .cghidEventTap)
        usleep(25_000)
    }
    markSynthetic(request, kind: "key")
    return true
}

private func safeCapturePath(_ request: [String: Any]) -> String? {
    guard let path = request["output_path"] as? String,
          let root = request["allowed_output_root"] as? String,
          !path.isEmpty, !root.isEmpty else { return nil }
    let standardizedPath = URL(fileURLWithPath: path).standardizedFileURL.path
    let standardizedRoot = URL(fileURLWithPath: root).standardizedFileURL.path + "/"
    return standardizedPath.hasPrefix(standardizedRoot) ? standardizedPath : nil
}

private func captureWindow(_ window: WindowInfo, path: String) -> Bool {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
    process.arguments = ["-x", "-o", "-l\(window.id)", path]
    process.standardOutput = FileHandle.nullDevice
    process.standardError = FileHandle.nullDevice
    do {
        try process.run()
        process.waitUntilExit()
    } catch {
        return false
    }
    return process.terminationStatus == 0
        && (try? FileManager.default.attributesOfItem(atPath: path)[.size] as? NSNumber)?.intValue ?? 0 > 0
}

private func success(
    _ request: [String: Any],
    dispatch: String,
    effect: String,
    message: String,
    data: [String: Any] = [:],
    evidence: [[String: Any]] = [],
    observedChange: Bool? = nil
) -> Never {
    var result = baseResult(request)
    result.merge([
        "ok": true,
        "dispatch_status": dispatch,
        "effect_status": effect,
        "message": message,
        "data": data,
        "evidence": evidence,
        "retry_safe": effect == "no_effect_verified",
    ]) { _, new in new }
    if let observedChange { result["observed_change"] = observedChange }
    emit(result)
}

private func handle(_ request: [String: Any]) -> Never {
    let action = request["action"] as? String ?? ""
    if request["protocol_version"] as? Int != protocolVersion {
        fail(request, code: "protocol_version_unsupported", message: "Unsupported Mac Use protocol version.")
    }

    if action == "_test_input_lock", request["test_only"] as? Bool == true {
        let startAt = request["start_at"] as? Double ?? 0
        while Date().timeIntervalSince1970 < startAt { usleep(500) }
        let descriptor = acquireInputLock(request)
        let holdMilliseconds = min(max(request["hold_ms"] as? Int ?? 250, 1), 2000)
        usleep(useconds_t(holdMilliseconds * 1000))
        flock(descriptor, LOCK_UN)
        close(descriptor)
        success(
            request,
            dispatch: "not_sent",
            effect: "verified",
            message: "Test-only input lock acquired atomically."
        )
    }

    if action == "_test_terminal_policy", request["test_only"] as? Bool == true {
        let target = targetPayload(request)
        let bundleID = target["app_bundle_id"] as? String ?? ""
        let owner = target["owner_name"] as? String ?? ""
        let text = request["text"] as? String ?? ""
        success(
            request,
            dispatch: "not_sent",
            effect: "verified",
            message: "Terminal submission policy evaluated without application access.",
            data: [
                "terminal_or_ide": isTerminalOrIDE(bundleID: bundleID, owner: owner),
                "contains_submission_separator": containsSubmissionSeparator(text),
                "blocked": isTerminalOrIDE(bundleID: bundleID, owner: owner)
                    && containsSubmissionSeparator(text),
            ]
        )
    }

    if action == "_test_screen_lock_policy", request["test_only"] as? Bool == true {
        let state = request["screen_locked"] as? Bool
        success(
            request,
            dispatch: "not_sent",
            effect: "verified",
            message: "Screen-lock fail-closed policy evaluated without application access.",
            data: ["write_allowed": writeAllowedWhenScreenLockState(state)]
        )
    }

    if action == "_test_window_geometry_mapping", request["test_only"] as? Bool == true {
        let target = targetPayload(request)
        let targetBounds = target["bounds"] as? [String: Any]
        let rows = request["candidate_bounds"] as? [[String: Any]] ?? []
        let match = targetBounds.flatMap(rectFromDictionary).flatMap { bounds in
            uniqueGeometryMatchIndex(rows.map(rectFromDictionary), target: bounds)
        }
        success(
            request,
            dispatch: "not_sent",
            effect: "verified",
            message: "Exact-window geometry mapping policy evaluated without application access.",
            data: ["match_index": match.map { $0 } ?? NSNull()]
        )
    }

    if action == "_test_sensitive_element_policy", request["test_only"] as? Bool == true {
        let role = request["element_role"] as? String ?? ""
        let subrole = request["element_subrole"] as? String ?? ""
        let containsProtectedContent = request["contains_protected_content"] as? Bool ?? false
        success(
            request,
            dispatch: "not_sent",
            effect: "verified",
            message: "Sensitive-element policy evaluated without application access.",
            data: [
                "sensitive": isSensitiveElement(
                    role: role,
                    subrole: subrole,
                    containsProtectedContent: containsProtectedContent
                ),
            ]
        )
    }

    if action != "status" && !trustedElectronParent() {
        fail(
            request,
            code: "untrusted_parent",
            message: "Mac Use helper accepts application data and input only from its owning Lantai Electron process."
        )
    }

    if action == "status" {
        success(
            request,
            dispatch: "not_sent",
            effect: "not_applicable",
            message: "Mac Use helper is available.",
            data: [
                "helper_version": "1",
                "accessibility": AXIsProcessTrusted() ? "granted" : "unknown",
                "screen_recording": CGPreflightScreenCaptureAccess() ? "granted" : "unknown",
                "screen_locked": screenLocked().map { $0 } ?? NSNull(),
            ]
        )
    }

    if action == "request_accessibility" {
        let options = [
            kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true,
        ] as CFDictionary
        let trusted = AXIsProcessTrustedWithOptions(options)
        success(
            request,
            dispatch: "not_sent",
            effect: "not_applicable",
            message: trusted
                ? "Accessibility permission is available to the Mac Use helper."
                : "macOS opened or retained the Mac Use helper Accessibility request.",
            data: ["accessibility": trusted ? "granted" : "unknown"]
        )
    }

    if action == "list_windows" {
        let windows = allWindows()
            .filter { $0.layer == 0 && $0.bounds.width >= 120 && $0.bounds.height >= 80 && !$0.bundleID.isEmpty }
            .map(publicWindow)
        success(
            request,
            dispatch: "not_sent",
            effect: "not_applicable",
            message: "Mac application windows inspected without input.",
            data: [
                "windows": windows,
                "permissions": [
                    "accessibility": AXIsProcessTrusted() ? "granted" : "unknown",
                    "screen_recording": CGPreflightScreenCaptureAccess() ? "granted" : "unknown",
                ],
            ]
        )
    }

    guard let window = targetWindow(request) else {
        fail(request, code: "stale_target", message: "The selected application window no longer matches its identity.")
    }

    let terminalOrIDE = isTerminalOrIDE(bundleID: window.bundleID, owner: window.owner)
    if action == "type_text",
       terminalOrIDE,
       containsSubmissionSeparator(request["text"] as? String ?? "") {
        fail(
            request,
            code: "terminal_submit_blocked",
            message: "Submitting input in terminal or IDE windows is not available through Mac Use."
        )
    }
    if action == "press_key",
       terminalOrIDE,
       ["enter", "return"].contains((request["key"] as? String ?? "").lowercased()) {
        fail(
            request,
            code: "terminal_submit_blocked",
            message: "Submitting input in terminal or IDE windows is not available through Mac Use."
        )
    }

    if action == "select_target" {
        success(
            request,
            dispatch: "not_sent",
            effect: "not_applicable",
            message: "The selected application window identity is current.",
            data: ["target": publicWindow(window)]
        )
    }

    if action == "observe_window" {
        let root = AXIsProcessTrusted() ? axWindow(for: window) : nil
        let elements = root.map(collectElements) ?? []
        var data: [String: Any] = [
            "target": publicWindow(window),
            "elements": elements,
            "accessibility": AXIsProcessTrusted() ? "granted" : "unknown",
            "screen_recording": CGPreflightScreenCaptureAccess() ? "granted" : "unknown",
        ]
        var evidence: [[String: Any]] = []
        if let path = safeCapturePath(request) {
            if captureWindow(window, path: path) {
                data["screenshot_path"] = path
                evidence.append(["kind": "window_screenshot", "path": path])
            } else {
                data["screenshot_error"] = "capture_failed"
            }
        }
        if let elementID = request["element_id"] as? String, !elementID.isEmpty,
           let root,
           let element = resolveElement(root, id: elementID),
           validateElement(element, request: request) {
            let value = stringValue(axAttribute(element, kAXValueAttribute as String), maximum: maximumTextCharacters)
            let expected = request["expected_value"] as? String ?? ""
            data["verified_element"] = ["element_id": elementID, "value": value]
            let verification = (request["metadata"] as? [String: Any])?["verification"] as? Bool ?? false
            if verification {
                data["expected_value_matches"] = value == expected
            }
        }
        success(
            request,
            dispatch: "not_sent",
            effect: "not_applicable",
            message: "Selected window observed without input.",
            data: data,
            evidence: evidence
        )
    }

    checkCancelled(request)

    let currentScreenLockState = screenLocked()
    if !writeAllowedWhenScreenLockState(currentScreenLockState) {
        fail(
            request,
            code: currentScreenLockState == true ? "screen_locked" : "screen_lock_unknown",
            message: currentScreenLockState == true
                ? "Mac Use does not write while the screen is locked."
                : "Mac Use could not verify that the screen is unlocked, so no input was sent."
        )
    }

    if !observationWindowIsCurrent(request, window: window) {
        fail(request, code: "stale_observation", message: "The target window moved or resized after it was observed.")
    }

    let inputLockDescriptor = acquireInputLock(request)
    _ = inputLockDescriptor
    requireUserIdle(request)
    let actionIndicator = showActionIndicator(owner: window.owner)
    _ = actionIndicator

    if action == "set_value" || action == "press_element" {
        guard AXIsProcessTrusted() else {
            fail(request, code: "permission_unknown", message: "Accessibility permission is not available to the Mac Use helper.")
        }
        guard let root = axWindow(for: window),
              let elementID = request["element_id"] as? String,
              let element = resolveElement(root, id: elementID),
              validateElement(element, request: request) else {
            fail(request, code: "stale_observation", message: "The observed accessibility element is no longer the same control.")
        }
        let elementRole = stringValue(
            axAttribute(element, kAXRoleAttribute as String),
            maximum: 60
        )
        let elementSubrole = stringValue(
            axAttribute(element, kAXSubroleAttribute as String),
            maximum: 80
        )
        let protectedValue = axAttribute(element, "AXContainsProtectedContent")
        let containsProtectedContent = (protectedValue as? NSNumber)?.boolValue
            ?? protectedValue as? Bool
            ?? false
        if isSensitiveElement(
            role: elementRole,
            subrole: elementSubrole,
            containsProtectedContent: containsProtectedContent
        ) {
            fail(
                request,
                code: "sensitive_target",
                message: "Mac Use does not write to password or protected-content controls."
            )
        }
        if action == "set_value" {
            let text = String((request["text"] as? String ?? "").prefix(maximumTextCharacters))
            requireUserIdle(request)
            checkCancelled(request)
            let focusError = AXUIElementSetAttributeValue(
                element,
                kAXFocusedAttribute as CFString,
                kCFBooleanTrue
            )
            requireUserIdle(
                request,
                afterDispatch: focusError == .success
            )
            checkCancelled(request, afterDispatch: focusError == .success)
            let error = AXUIElementSetAttributeValue(element, kAXValueAttribute as CFString, text as CFTypeRef)
            let readback = stringValue(axAttribute(element, kAXValueAttribute as String), maximum: maximumTextCharacters)
            guard error == .success else {
                fail(request, code: "ax_write_failed", message: "Accessibility rejected the value update (\(error.rawValue)).")
            }
            if readback == text {
                success(
                    request,
                    dispatch: "sent",
                    effect: "verified",
                    message: "The target control value was updated and read back.",
                    data: ["element_id": elementID, "value": readback],
                    observedChange: true
                )
            }
            success(
                request,
                dispatch: "sent",
                effect: "ambiguous",
                message: "Accessibility accepted the update, but exact readback was unavailable.",
                data: ["element_id": elementID],
                observedChange: nil
            )
        }
        requireUserIdle(request)
        checkCancelled(request)
        let error = AXUIElementPerformAction(element, kAXPressAction as CFString)
        guard error == .success else {
            fail(request, code: "ax_press_failed", message: "Accessibility rejected the control action (\(error.rawValue)).")
        }
        success(
            request,
            dispatch: "sent",
            effect: "ambiguous",
            message: "The control accepted the press; verify the application state before continuing."
        )
    }

    checkCancelled(request)
    activate(window, request: request)
    requireUserIdle(request, afterDispatch: true)
    checkCancelled(request, afterDispatch: true)

    guard let current = targetWindow(request), current.onScreen else {
        fail(request, code: "stale_target", message: "The target changed or remained outside the active Space.")
    }

    if action == "focus_window" {
        success(
            request,
            dispatch: "sent",
            effect: "verified",
            message: "The selected application became active.",
            data: ["target": publicWindow(current)]
        )
    }

    if action == "click_at" {
        guard let x = request["x"] as? Double, let y = request["y"] as? Double,
              x >= 0, x <= 1, y >= 0, y <= 1 else {
            fail(request, code: "invalid_coordinates", message: "click_at requires normalized coordinates.")
        }
        let point = CGPoint(
            x: current.bounds.minX + current.bounds.width * x,
            y: current.bounds.minY + current.bounds.height * y
        )
        guard let top = topWindow(at: point), top.id == current.id else {
            fail(request, code: "blocked_by_occlusion", message: "Another window covers the requested click point.")
        }
        checkCancelled(request, afterDispatch: true)
        postMouseClick(point, request: request)
        success(
            request,
            dispatch: "sent",
            effect: "ambiguous",
            message: "The click was sent to the selected window; verify the application state before continuing."
        )
    }

    if action == "type_text" {
        let text = request["text"] as? String ?? ""
        guard let fingerprint = request["element_fingerprint"] as? [String: Any],
              let role = fingerprint["role"] as? String,
              ["AXTextArea", "AXTextField"].contains(role),
              fingerprint["enabled"] as? Bool == true,
              fingerprint["sensitive"] as? Bool == false else {
            fail(
                request,
                code: "invalid_text_target",
                message: "Text input requires an enabled, non-sensitive observed text control."
            )
        }
        guard AXIsProcessTrusted(),
              let root = axWindow(for: current),
              let elementID = request["element_id"] as? String,
              let element = resolveElement(root, id: elementID),
              validateElement(element, request: request) else {
            fail(
                request,
                code: "stale_observation",
                message: "The observed text control is no longer the same control."
            )
        }
        checkCancelled(request)
        let focusError = AXUIElementSetAttributeValue(
            element,
            kAXFocusedAttribute as CFString,
            kCFBooleanTrue
        )
        let focusedValue = axAttribute(element, kAXFocusedAttribute as String)
        let focused = (focusedValue as? NSNumber)?.boolValue
            ?? focusedValue as? Bool
            ?? false
        guard focusError == .success, focused else {
            fail(
                request,
                code: "focus_failed",
                message: "The observed text control could not be focused safely.",
                dispatch: focusError == .success ? "sent" : "not_sent",
                effect: focusError == .success ? "ambiguous" : "failed",
                retrySafe: focusError != .success
            )
        }
        checkCancelled(request, afterDispatch: true)
        postText(text, request: request, effectAlreadyDispatched: true)
        success(
            request,
            dispatch: "sent",
            effect: "ambiguous",
            message: "Text input was sent to the active target; verify the application state before continuing.",
            data: ["text_length": min(text.count, maximumTextCharacters)]
        )
    }

    if action == "press_key" {
        let key = request["key"] as? String ?? ""
        checkCancelled(request, afterDispatch: true)
        guard postKey(key, request: request) else {
            fail(request, code: "unsupported_key", message: "This key is not in the Mac Use allowlist.")
        }
        success(
            request,
            dispatch: "sent",
            effect: "ambiguous",
            message: "The key was sent to the selected window; verify the application state before continuing."
        )
    }

    fail(request, code: "action_unsupported", message: "The requested Mac Use action is not implemented.")
}

handle(readRequest())
