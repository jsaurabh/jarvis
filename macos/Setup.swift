import AVFoundation
import AppKit
import ApplicationServices
import UserNotifications

final class APIKeyEntryView: NSView {
  let field = NSSecureTextField(frame: .zero)

  override init(frame frameRect: NSRect) {
    super.init(frame: NSRect(x: 0, y: 0, width: 460, height: 28))

    field.placeholderString = "sk-…"
    field.translatesAutoresizingMaskIntoConstraints = false

    let pasteButton = NSButton(
      title: "Paste from Clipboard",
      target: self,
      action: #selector(pasteFromClipboard)
    )
    pasteButton.bezelStyle = .rounded
    pasteButton.translatesAutoresizingMaskIntoConstraints = false

    addSubview(field)
    addSubview(pasteButton)
    NSLayoutConstraint.activate([
      field.leadingAnchor.constraint(equalTo: leadingAnchor),
      field.centerYAnchor.constraint(equalTo: centerYAnchor),
      field.trailingAnchor.constraint(equalTo: pasteButton.leadingAnchor, constant: -8),
      pasteButton.trailingAnchor.constraint(equalTo: trailingAnchor),
      pasteButton.centerYAnchor.constraint(equalTo: centerYAnchor),
      pasteButton.widthAnchor.constraint(greaterThanOrEqualToConstant: 150),
    ])
  }

  @available(*, unavailable)
  required init?(coder: NSCoder) {
    nil
  }

  @objc private func pasteFromClipboard() {
    guard
      let value = NSPasteboard.general.string(forType: .string)?
        .trimmingCharacters(in: .whitespacesAndNewlines),
      !value.isEmpty
    else {
      NSSound.beep()
      return
    }
    field.stringValue = value
    window?.makeFirstResponder(field)
  }
}

final class SetupController: NSObject, NSWindowDelegate {
  typealias PermissionCompletion = (Bool) -> Void

  private let changeAPIKey: (@escaping PermissionCompletion) -> Void
  private let enableVoice: (@escaping PermissionCompletion) -> Void
  private let onComplete: () -> Void

  private var window: NSWindow?
  private var completionSent = false
  private weak var apiStatus: NSTextField?
  private weak var voiceStatus: NSTextField?
  private weak var notificationStatus: NSTextField?
  private weak var accessibilityStatus: NSTextField?

  init(
    changeAPIKey: @escaping (@escaping PermissionCompletion) -> Void,
    enableVoice: @escaping (@escaping PermissionCompletion) -> Void,
    onComplete: @escaping () -> Void
  ) {
    self.changeAPIKey = changeAPIKey
    self.enableVoice = enableVoice
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
      contentRect: NSRect(x: 0, y: 0, width: 620, height: 650),
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

    stack.addArrangedSubview(
      row(
        title: "Files and folders",
        detail: statusLabel("Use Full Disk Access for protected locations"),
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
    refreshNotificationStatus()
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
