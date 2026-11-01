import AVFoundation
import AppKit
import ApplicationServices
import UserNotifications

final class SetupController: NSObject, NSWindowDelegate {
  typealias PermissionCompletion = (Bool) -> Void

  private let changeAPIKey: (@escaping PermissionCompletion) -> Void
  private let enableVoice: (@escaping PermissionCompletion) -> Void
  private let filesystemRoots: () -> [String]
  private let changeFilesystemRoots: ([String], @escaping PermissionCompletion) -> Void
  private let installSandbox: (@escaping PermissionCompletion) -> Void
  private let verifySandbox: (@escaping PermissionCompletion) -> Void
  private let onComplete: () -> Void

  private var window: NSWindow?
  private var completionSent = false
  private weak var apiStatus: NSTextField?
  private weak var voiceStatus: NSTextField?
  private weak var notificationStatus: NSTextField?
  private weak var accessibilityStatus: NSTextField?
  private weak var filesystemStatus: NSTextField?
  private weak var sandboxStatus: NSTextField?

  init(
    changeAPIKey: @escaping (@escaping PermissionCompletion) -> Void,
    enableVoice: @escaping (@escaping PermissionCompletion) -> Void,
    filesystemRoots: @escaping () -> [String],
    changeFilesystemRoots: @escaping ([String], @escaping PermissionCompletion) -> Void,
    installSandbox: @escaping (@escaping PermissionCompletion) -> Void,
    verifySandbox: @escaping (@escaping PermissionCompletion) -> Void,
    onComplete: @escaping () -> Void
  ) {
    self.changeAPIKey = changeAPIKey
    self.enableVoice = enableVoice
    self.filesystemRoots = filesystemRoots
    self.changeFilesystemRoots = changeFilesystemRoots
    self.installSandbox = installSandbox
    self.verifySandbox = verifySandbox
    self.onComplete = onComplete
  }

  func show(apiKeyConfigured: Bool) {
    if let window {
      NSRunningApplication.current.activate(options: [.activateAllWindows])
      window.makeKeyAndOrderFront(nil)
      refreshStatuses(apiKeyConfigured: apiKeyConfigured)
      return
    }

    completionSent = false
    let window = NSWindow(
      contentRect: NSRect(x: 0, y: 0, width: 620, height: 770),
      styleMask: [.titled, .closable],
      backing: .buffered,
      defer: false
    )
    window.title = "Setup Jarvis"
    window.isReleasedWhenClosed = false
    window.delegate = self
    window.center()
    window.contentView = makeContent(apiKeyConfigured: apiKeyConfigured)
    self.window = window

    NSRunningApplication.current.activate(options: [.activateAllWindows])
    window.makeKeyAndOrderFront(nil)
    refreshNotificationStatus()
  }

  func windowWillClose(_ notification: Notification) {
    window = nil
    finishOnce()
  }

  private func makeContent(apiKeyConfigured: Bool) -> NSView {
    let content = NSView()
    let stack = NSStackView()
    stack.orientation = .vertical
    stack.alignment = .leading
    stack.spacing = 16
    stack.translatesAutoresizingMaskIntoConstraints = false
    content.addSubview(stack)

    let heading = NSTextField(labelWithString: "Set up Jarvis")
    heading.font = .systemFont(ofSize: 24, weight: .semibold)
    stack.addArrangedSubview(heading)

    let introduction = NSTextField(
      wrappingLabelWithString:
        "Connect your model and review the macOS permissions Jarvis uses. You can return here at any time from the menu."
    )
    introduction.textColor = .secondaryLabelColor
    stack.addArrangedSubview(introduction)

    let apiStatus = statusLabel(apiKeyConfigured ? "Configured in macOS Keychain" : "Required")
    self.apiStatus = apiStatus
    stack.addArrangedSubview(
      row(
        title: "OpenAI API key",
        detail: apiStatus,
        buttonTitle: apiKeyConfigured ? "Change…" : "Set…",
        action: #selector(changeKey)
      ))

    let voiceStatus = statusLabel(voicePermissionText())
    self.voiceStatus = voiceStatus
    stack.addArrangedSubview(
      row(
        title: "Voice",
        detail: voiceStatus,
        buttonTitle: "Enable…",
        action: #selector(enableVoiceAccess)
      ))

    let notificationStatus = statusLabel("Checking…")
    self.notificationStatus = notificationStatus
    stack.addArrangedSubview(
      row(
        title: "Notifications",
        detail: notificationStatus,
        buttonTitle: "Enable…",
        action: #selector(enableNotifications)
      ))

    let accessibilityStatus = statusLabel(
      AXIsProcessTrusted() ? "Allowed" : "Needed to control other applications")
    self.accessibilityStatus = accessibilityStatus
    stack.addArrangedSubview(
      row(
        title: "Accessibility",
        detail: accessibilityStatus,
        buttonTitle: "Review…",
        action: #selector(reviewAccessibility)
      ))

    let filesystemStatus = statusLabel(filesystemPermissionText())
    self.filesystemStatus = filesystemStatus
    stack.addArrangedSubview(filesystemRow(detail: filesystemStatus))

    let sandboxStatus = statusLabel(sandboxPermissionText())
    self.sandboxStatus = sandboxStatus
    stack.addArrangedSubview(sandboxRow(detail: sandboxStatus))

    stack.addArrangedSubview(
      row(
        title: "Protected files",
        detail: statusLabel("Full Disk Access is optional and remains controlled by macOS"),
        buttonTitle: "Open Settings…",
        action: #selector(openFullDiskAccess)
      ))

    stack.addArrangedSubview(
      row(
        title: "Calendar",
        detail: statusLabel("macOS asks when Jarvis first accesses Calendar"),
        buttonTitle: "Open Settings…",
        action: #selector(openCalendarPrivacy)
      ))

    stack.addArrangedSubview(
      row(
        title: "Application automation",
        detail: statusLabel("Permissions are granted separately for each application"),
        buttonTitle: "Open Settings…",
        action: #selector(openAutomationPrivacy)
      ))

    stack.addArrangedSubview(
      row(
        title: "Open at login",
        detail: statusLabel("Managed by macOS"),
        buttonTitle: "Open Settings…",
        action: #selector(openLoginItems)
      ))

    let privacy = NSTextField(
      wrappingLabelWithString:
        "Jarvis stores its API key in Keychain. macOS controls device and application permissions, which you can revoke at any time."
    )
    privacy.textColor = .secondaryLabelColor
    privacy.font = .systemFont(ofSize: 11)
    stack.addArrangedSubview(privacy)

    let done = NSButton(title: "Done", target: self, action: #selector(done))
    done.keyEquivalent = "\r"
    done.bezelStyle = .rounded
    let buttonRow = NSStackView(views: [NSView(), done])
    buttonRow.orientation = .horizontal
    buttonRow.alignment = .centerY
    stack.addArrangedSubview(buttonRow)

    for view in stack.arrangedSubviews {
      view.translatesAutoresizingMaskIntoConstraints = false
      view.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
    }

    NSLayoutConstraint.activate([
      stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 28),
      stack.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -28),
      stack.topAnchor.constraint(equalTo: content.topAnchor, constant: 24),
      stack.bottomAnchor.constraint(lessThanOrEqualTo: content.bottomAnchor, constant: -20),
    ])
    return content
  }

  private func row(
    title: String,
    detail: NSTextField,
    buttonTitle: String,
    action: Selector
  ) -> NSView {
    let titleLabel = NSTextField(labelWithString: title)
    titleLabel.font = .systemFont(ofSize: 13, weight: .medium)
    let labels = NSStackView(views: [titleLabel, detail])
    labels.orientation = .vertical
    labels.alignment = .leading
    labels.spacing = 2

    let button = NSButton(title: buttonTitle, target: self, action: action)
    button.bezelStyle = .rounded
    button.setContentHuggingPriority(.required, for: .horizontal)

    let row = NSStackView(views: [labels, button])
    row.orientation = .horizontal
    row.alignment = .centerY
    row.distribution = .fill
    row.spacing = 16
    return row
  }

  private func filesystemRow(detail: NSTextField) -> NSView {
    let titleLabel = NSTextField(labelWithString: "Files and folders")
    titleLabel.font = .systemFont(ofSize: 13, weight: .medium)
    let labels = NSStackView(views: [titleLabel, detail])
    labels.orientation = .vertical
    labels.alignment = .leading
    labels.spacing = 2

    let add = NSButton(title: "Add…", target: self, action: #selector(addFilesystemRoot))
    add.bezelStyle = .rounded
    let remove = NSButton(
      title: "Remove…", target: self, action: #selector(removeFilesystemRoot))
    remove.bezelStyle = .rounded
    let buttons = NSStackView(views: [add, remove])
    buttons.orientation = .horizontal
    buttons.spacing = 6
    buttons.setContentHuggingPriority(.required, for: .horizontal)

    let row = NSStackView(views: [labels, buttons])
    row.orientation = .horizontal
    row.alignment = .centerY
    row.distribution = .fill
    row.spacing = 16
    return row
  }

  private func sandboxRow(detail: NSTextField) -> NSView {
    let title = NSTextField(labelWithString: "Isolated execution")
    title.font = .systemFont(ofSize: 13, weight: .medium)
    let labels = NSStackView(views: [title, detail])
    labels.orientation = .vertical
    labels.alignment = .leading
    labels.spacing = 2
    let setup = NSButton(title: "Set up…", target: self, action: #selector(setupSandbox))
    let test = NSButton(title: "Test", target: self, action: #selector(testSandbox))
    let buttons = NSStackView(views: [setup, test])
    buttons.orientation = .horizontal
    buttons.spacing = 6
    buttons.setContentHuggingPriority(.required, for: .horizontal)
    let row = NSStackView(views: [labels, buttons])
    row.orientation = .horizontal
    row.alignment = .centerY
    row.spacing = 16
    return row
  }

  private func statusLabel(_ text: String) -> NSTextField {
    let label = NSTextField(labelWithString: text)
    label.textColor = .secondaryLabelColor
    label.font = .systemFont(ofSize: 11)
    return label
  }

  private func refreshStatuses(apiKeyConfigured: Bool) {
    apiStatus?.stringValue = apiKeyConfigured ? "Configured in macOS Keychain" : "Required"
    voiceStatus?.stringValue = voicePermissionText()
    accessibilityStatus?.stringValue =
      AXIsProcessTrusted() ? "Allowed" : "Needed to control other applications"
    filesystemStatus?.stringValue = filesystemPermissionText()
    sandboxStatus?.stringValue = sandboxPermissionText()
    refreshNotificationStatus()
  }

  private func filesystemPermissionText() -> String {
    let roots = filesystemRoots()
    if roots.isEmpty { return "No folders authorized" }
    if roots == ["/"] { return "Entire Mac, subject to macOS permissions" }
    let abbreviated = roots.map { NSString(string: $0).abbreviatingWithTildeInPath }
    if abbreviated.count <= 2 { return abbreviated.joined(separator: ", ") }
    return "\(abbreviated.count) authorized locations"
  }

  private func sandboxPermissionText() -> String {
    let files = FileManager.default
    let versionURL = files.homeDirectoryForCurrentUser.appendingPathComponent(
      ".local/share/shuru/VERSION")
    let version = (try? String(contentsOf: versionURL, encoding: .utf8))?
      .trimmingCharacters(in: .whitespacesAndNewlines)
    if files.isExecutableFile(atPath: "/opt/homebrew/bin/shuru")
      && files.isExecutableFile(atPath: "/opt/homebrew/opt/squid/sbin/squid")
      && version == "0.11.0"
    {
      return "Installed; versions are verified when used"
    }
    return "Needed for isolated Linux commands and approved network access"
  }

  @objc private func setupSandbox(_ sender: NSButton) {
    let alert = NSAlert()
    alert.icon = NSApp.applicationIconImage
    alert.messageText = "Set up isolated execution?"
    alert.informativeText =
      "Jarvis will install Shuru and Squid with Homebrew and download a Linux image. Homebrew may update dependencies."
    alert.addButton(withTitle: "Set Up")
    alert.addButton(withTitle: "Cancel")
    guard alert.runModal() == .alertFirstButtonReturn else { return }
    sender.isEnabled = false
    sandboxStatus?.stringValue = "Installing; this may take a few minutes…"
    installSandbox { [weak self, weak sender] success in
      sender?.isEnabled = true
      self?.sandboxStatus?.stringValue =
        success
        ? self?.sandboxPermissionText() ?? "Installed" : "Setup failed; see the Jarvis setup log"
    }
  }

  @objc private func testSandbox(_ sender: NSButton) {
    sender.isEnabled = false
    sandboxStatus?.stringValue = "Testing isolated execution and the OpenAI connection…"
    verifySandbox { [weak self, weak sender] success in
      sender?.isEnabled = true
      self?.sandboxStatus?.stringValue = success ? "Verified" : "Test failed; see the error"
    }
  }

  private func voicePermissionText() -> String {
    switch AVCaptureDevice.authorizationStatus(for: .audio) {
    case .authorized: return "Microphone access allowed"
    case .denied, .restricted: return "Microphone access denied in System Settings"
    case .notDetermined: return "Microphone access not requested"
    @unknown default: return "Microphone permission status unavailable"
    }
  }

  private func refreshNotificationStatus() {
    UNUserNotificationCenter.current().getNotificationSettings { [weak self] settings in
      let text: String
      switch settings.authorizationStatus {
      case .authorized, .provisional, .ephemeral: text = "Allowed"
      case .denied: text = "Denied in System Settings"
      case .notDetermined: text = "Not requested"
      @unknown default: text = "Permission status unavailable"
      }
      DispatchQueue.main.async { self?.notificationStatus?.stringValue = text }
    }
  }

  @objc private func changeKey() {
    changeAPIKey { [weak self] changed in
      guard changed else { return }
      self?.apiStatus?.stringValue = "Configured in macOS Keychain"
    }
  }

  @objc private func enableVoiceAccess() {
    enableVoice { [weak self] _ in
      self?.voiceStatus?.stringValue = self?.voicePermissionText() ?? "Permission updated"
    }
  }

  @objc private func enableNotifications() {
    UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) {
      [weak self] _, _ in
      self?.refreshNotificationStatus()
    }
  }

  @objc private func reviewAccessibility() {
    let options =
      [
        kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true
      ] as CFDictionary
    _ = AXIsProcessTrustedWithOptions(options)
    DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in
      self?.accessibilityStatus?.stringValue =
        AXIsProcessTrusted() ? "Allowed" : "Review in System Settings"
    }
  }

  @objc private func addFilesystemRoot() {
    let panel = NSOpenPanel()
    panel.title = "Choose folders or volumes Jarvis may access"
    panel.message =
      "Jarvis will only read or modify files inside the locations listed in Setup. macOS permissions still apply."
    panel.prompt = "Add Access"
    panel.canChooseFiles = false
    panel.canChooseDirectories = true
    panel.allowsMultipleSelection = true
    panel.canCreateDirectories = false
    panel.directoryURL = FileManager.default.homeDirectoryForCurrentUser
    guard panel.runModal() == .OK else { return }
    let roots = filesystemRoots() + panel.urls.map(\.path)
    applyFilesystemRoots(roots)
  }

  @objc private func removeFilesystemRoot() {
    let roots = filesystemRoots()
    guard !roots.isEmpty else { return }
    let picker = NSPopUpButton(frame: NSRect(x: 0, y: 0, width: 420, height: 26))
    picker.addItems(
      withTitles: roots.map { NSString(string: $0).abbreviatingWithTildeInPath })
    let alert = NSAlert()
    alert.icon = NSApp.applicationIconImage
    alert.messageText = "Remove file access"
    alert.informativeText = "Choose the location Jarvis should no longer access."
    alert.accessoryView = picker
    alert.addButton(withTitle: "Remove Access")
    alert.addButton(withTitle: "Cancel")
    guard alert.runModal() == .alertFirstButtonReturn else { return }
    var updated = roots
    updated.remove(at: picker.indexOfSelectedItem)
    applyFilesystemRoots(updated)
  }

  private func applyFilesystemRoots(_ roots: [String]) {
    changeFilesystemRoots(roots) { [weak self] changed in
      guard changed else { return }
      self?.filesystemStatus?.stringValue = self?.filesystemPermissionText() ?? "Updated"
    }
  }

  @objc private func openFullDiskAccess() {
    openSettings("x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles")
  }

  @objc private func openCalendarPrivacy() {
    openSettings("x-apple.systempreferences:com.apple.preference.security?Privacy_Calendars")
  }

  @objc private func openAutomationPrivacy() {
    openSettings("x-apple.systempreferences:com.apple.preference.security?Privacy_Automation")
  }

  @objc private func openLoginItems() {
    openSettings("x-apple.systempreferences:com.apple.LoginItems-Settings.extension")
  }

  private func openSettings(_ value: String) {
    if let url = URL(string: value), NSWorkspace.shared.open(url) { return }
    NSWorkspace.shared.open(URL(fileURLWithPath: "/System/Applications/System Settings.app"))
  }

  @objc private func done() {
    window?.close()
  }

  private func finishOnce() {
    guard !completionSent else { return }
    completionSent = true
    onComplete()
  }
}
