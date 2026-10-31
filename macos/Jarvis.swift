import AVFoundation
import AppKit
import Foundation
import Security
import Speech
import UserNotifications

private let keychainService = "com.cray.jarvis"
private let keychainAccount = "openai-api-key"
private let launchAgentLabel = "com.cray.jarvis"

private func jarvisIdentityImage() -> NSImage? {
  guard let url = Bundle.main.url(forResource: "JarvisAppIcon", withExtension: "png") else {
    return nil
  }
  return NSImage(contentsOf: url)
}

private func makeJarvisAlert() -> NSAlert {
  let alert = NSAlert()
  alert.icon = jarvisIdentityImage()
  return alert
}

private func projectRootURL() -> URL {
  if let configuredPath = Bundle.main.object(forInfoDictionaryKey: "JarvisProjectPath") as? String,
    !configuredPath.isEmpty
  {
    return URL(fileURLWithPath: configuredPath, isDirectory: true)
  }
  return Bundle.main.bundleURL
    .deletingLastPathComponent()
    .deletingLastPathComponent()
}

private func keychainAPIKey() -> String? {
  let query: [String: Any] = [
    kSecClass as String: kSecClassGenericPassword,
    kSecAttrService as String: keychainService,
    kSecAttrAccount as String: keychainAccount,
    kSecReturnData as String: true,
    kSecMatchLimit as String: kSecMatchLimitOne,
  ]
  var item: CFTypeRef?
  guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
    let data = item as? Data
  else { return nil }
  return String(data: data, encoding: .utf8)
}

private func projectAPIKey() -> String? {
  let envURL = projectRootURL().appendingPathComponent(".env")
  NSLog("Jarvis startup: checking key source at %@", envURL.path)
  guard let contents = try? String(contentsOf: envURL, encoding: .utf8) else { return nil }
  for line in contents.split(whereSeparator: \Character.isNewline) {
    guard line.hasPrefix("OPENAI_API_KEY=") else { continue }
    var value = String(line.dropFirst("OPENAI_API_KEY=".count))
      .trimmingCharacters(in: .whitespacesAndNewlines)
    if value.count >= 2,
      value.hasPrefix("\"") && value.hasSuffix("\"") || value.hasPrefix("'") && value.hasSuffix("'")
    {
      value.removeFirst()
      value.removeLast()
    }
    return value.isEmpty ? nil : value
  }
  return nil
}

@discardableResult
private func saveAPIKey(_ value: String) -> Bool {
  let identity: [String: Any] = [
    kSecClass as String: kSecClassGenericPassword,
    kSecAttrService as String: keychainService,
    kSecAttrAccount as String: keychainAccount,
  ]
  SecItemDelete(identity as CFDictionary)
  var item = identity
  item[kSecValueData as String] = value.data(using: .utf8)
  return SecItemAdd(item as CFDictionary, nil) == errSecSuccess
}

final class LoginController {
  private var plistURL: URL {
    FileManager.default.homeDirectoryForCurrentUser
      .appendingPathComponent("Library/LaunchAgents/\(launchAgentLabel).plist")
  }

  func setEnabled(_ enabled: Bool) throws {
    if enabled {
      guard let executable = Bundle.main.executablePath else {
        throw NSError(
          domain: "Jarvis", code: 1,
          userInfo: [NSLocalizedDescriptionKey: "App executable is unavailable."])
      }
      let directory = plistURL.deletingLastPathComponent()
      try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
      let payload: [String: Any] = [
        "Label": launchAgentLabel,
        "ProgramArguments": [executable],
        "RunAtLoad": true,
        "ProcessType": "Interactive",
        "LimitLoadToSessionType": "Aqua",
      ]
      let data = try PropertyListSerialization.data(
        fromPropertyList: payload, format: .xml, options: 0)
      try data.write(to: plistURL, options: .atomic)
    } else if FileManager.default.fileExists(atPath: plistURL.path) {
      try FileManager.default.removeItem(at: plistURL)
    }
  }
}

struct BridgeResult {
  let status: String
  let message: String
  let runID: String?
  let taskID: String?
  let approvalID: String?
  let approvalDescription: String?
  let details: [String: Any]
}

final class BackendController {
  typealias Completion = (Result<BridgeResult, Error>) -> Void

  var onEvent: (([String: Any]) -> Void)?

  private let apiKey: String
  private let projectURL: URL
  private let queue = DispatchQueue(label: "com.cray.jarvis.core")
  private var process: Process?
  private var inputPipe: Pipe?
  private var outputPipe: Pipe?
  private var errorPipe: Pipe?
  private var readBuffer = Data()
  private var pending: [String: Completion] = [:]

  init(apiKey: String) {
    self.apiKey = apiKey
    self.projectURL = projectRootURL()
    queue.async { [weak self] in
      do { try self?.startIfNeeded() } catch {
        NSLog("Jarvis Core failed to start: %@", error.localizedDescription)
      }
    }
  }

  deinit { stop() }

  func chat(_ text: String, completion: @escaping Completion) {
    request("chat", payload: ["text": text], completion: completion)
  }

  func approve(_ id: String, approved: Bool, completion: @escaping Completion) {
    request("approve", payload: ["approval_id": id, "approved": approved], completion: completion)
  }

  func feedback(
    runID: String, rating: Int, correction: String? = nil, completion: @escaping Completion
  ) {
    var payload: [String: Any] = ["run_id": runID, "rating": rating]
    if let correction { payload["correction"] = correction }
    request("feedback", payload: payload, completion: completion)
  }

  func status(taskID: String? = nil, completion: @escaping Completion) {
    var payload: [String: Any] = [:]
    if let taskID { payload["task_id"] = taskID }
    request("status", payload: payload, completion: completion)
  }

  func cancel(taskID: String? = nil, completion: @escaping Completion) {
    var payload: [String: Any] = [:]
    if let taskID { payload["task_id"] = taskID }
    request("cancel", payload: payload, completion: completion)
  }

  func listTasks(completion: @escaping Completion) {
    request("tasks", payload: ["limit": 10], completion: completion)
  }

  func remember(_ content: String, kind: String = "fact", completion: @escaping Completion) {
    request("memory-remember", payload: ["content": content, "kind": kind], completion: completion)
  }

  func listMemories(completion: @escaping Completion) {
    request("memory-list", payload: ["limit": 50], completion: completion)
  }

  func forgetMemory(_ id: String, completion: @escaping Completion) {
    request("memory-forget", payload: ["memory_id": id], completion: completion)
  }

  func scheduleReminder(
    message: String,
    dueAt: String,
    repeatSeconds: Int?,
    completion: @escaping Completion
  ) {
    var payload: [String: Any] = ["message": message, "due_at": dueAt]
    if let repeatSeconds { payload["repeat_seconds"] = repeatSeconds }
    request("reminder-create", payload: payload, completion: completion)
  }

  func listReminders(completion: @escaping Completion) {
    request("reminders", payload: ["limit": 50], completion: completion)
  }

  func cancelReminder(_ id: String, completion: @escaping Completion) {
    request("reminder-cancel", payload: ["reminder_id": id], completion: completion)
  }

  func createAutomation(
    name: String,
    prompt: String,
    scheduleType: String,
    runAt: String?,
    intervalSeconds: Int?,
    localTime: String?,
    timezone: String?,
    completion: @escaping Completion
  ) {
    var payload: [String: Any] = [
      "name": name,
      "prompt": prompt,
      "schedule_type": scheduleType,
    ]
    if let runAt { payload["run_at"] = runAt }
    if let intervalSeconds { payload["interval_seconds"] = intervalSeconds }
    if let localTime { payload["local_time"] = localTime }
    if let timezone { payload["timezone"] = timezone }
    request("automation-create", payload: payload, completion: completion)
  }

  func listAutomations(completion: @escaping Completion) {
    request("automations", payload: ["limit": 50], completion: completion)
  }

  func manageAutomation(_ id: String, operation: String, completion: @escaping Completion) {
    request(
      "automation-manage",
      payload: ["automation_id": id, "operation": operation],
      completion: completion
    )
  }

  func listSkills(completion: @escaping Completion) {
    request("skills", payload: [:], completion: completion)
  }

  func qualityReport(completion: @escaping Completion) {
    request("quality", payload: [:], completion: completion)
  }

  func traceViewer(completion: @escaping Completion) {
    request("trace-viewer", payload: [:], completion: completion)
  }

  func recordRealtimeTurn(
    userText: String,
    assistantText: String,
    metadata: [String: Any],
    completion: @escaping (Result<BridgeResult, Error>) -> Void
  ) {
    request(
      "trace-turn",
      payload: [
        "user_text": userText,
        "assistant_text": assistantText,
        "model": jarvisRealtimeModel,
        "metadata": metadata,
      ], completion: completion)
  }

  func stop() {
    queue.async { [weak self] in
      guard let self else { return }
      self.outputPipe?.fileHandleForReading.readabilityHandler = nil
      self.errorPipe?.fileHandleForReading.readabilityHandler = nil
      self.inputPipe?.fileHandleForWriting.closeFile()
      if self.process?.isRunning == true { self.process?.terminate() }
      self.process = nil
      self.failPending("Jarvis Core stopped.")
    }
  }

  private func request(_ action: String, payload: [String: Any], completion: @escaping Completion) {
    let requestID = UUID().uuidString
    queue.async { [weak self] in
      guard let self else { return }
      do {
        try self.startIfNeeded()
        let object: [String: Any] = ["id": requestID, "action": action, "payload": payload]
        guard JSONSerialization.isValidJSONObject(object) else {
          throw NSError(
            domain: "Jarvis", code: 2,
            userInfo: [NSLocalizedDescriptionKey: "Could not encode a Jarvis Core request."])
        }
        var data = try JSONSerialization.data(withJSONObject: object)
        data.append(0x0A)
        self.pending[requestID] = completion
        guard let handle = self.inputPipe?.fileHandleForWriting else {
          throw NSError(
            domain: "Jarvis", code: 3,
            userInfo: [NSLocalizedDescriptionKey: "Jarvis Core input is unavailable."])
        }
        try handle.write(contentsOf: data)
      } catch {
        self.pending.removeValue(forKey: requestID)
        DispatchQueue.main.async { completion(.failure(error)) }
      }
    }
  }

  private func startIfNeeded() throws {
    if process?.isRunning == true { return }
    let python = projectURL.appendingPathComponent(".venv/bin/python")
    let process = Process()
    let input = Pipe()
    let output = Pipe()
    let errors = Pipe()
    process.executableURL = python
    process.arguments = ["-m", "jarvis.daemon"]
    process.currentDirectoryURL = projectURL
    process.standardInput = input
    process.standardOutput = output
    process.standardError = errors
    var environment = ProcessInfo.processInfo.environment
    environment["OPENAI_API_KEY"] = apiKey
    environment["JARVIS_ALLOWED_ROOTS"] = "/"
    environment["JARVIS_DB_PATH"] = projectURL.appendingPathComponent("data/jarvis.db").path
    environment["PYTHONUNBUFFERED"] = "1"
    process.environment = environment
    output.fileHandleForReading.readabilityHandler = { [weak self] handle in
      let data = handle.availableData
      guard !data.isEmpty else { return }
      self?.queue.async { self?.consume(data) }
    }
    errors.fileHandleForReading.readabilityHandler = { handle in
      let data = handle.availableData
      if !data.isEmpty, let text = String(data: data, encoding: .utf8) {
        NSLog("Jarvis Core: %@", text.trimmingCharacters(in: .whitespacesAndNewlines))
      }
    }
    process.terminationHandler = { [weak self] terminated in
      self?.queue.async {
        guard let self, self.process === terminated else { return }
        self.process = nil
        self.failPending("Jarvis Core exited unexpectedly.")
      }
    }
    try process.run()
    self.process = process
    inputPipe = input
    outputPipe = output
    errorPipe = errors
    readBuffer.removeAll(keepingCapacity: true)
  }

  private func consume(_ data: Data) {
    readBuffer.append(data)
    while let newline = readBuffer.firstIndex(of: 0x0A) {
      let line = Data(readBuffer[..<newline])
      readBuffer.removeSubrange(...newline)
      guard !line.isEmpty,
        let object = try? JSONSerialization.jsonObject(with: line) as? [String: Any]
      else {
        continue
      }
      if object["type"] as? String == "event", let event = object["event"] as? [String: Any] {
        DispatchQueue.main.async { [weak self] in self?.onEvent?(event) }
        continue
      }
      guard let requestID = object["id"] as? String,
        let completion = pending.removeValue(forKey: requestID),
        let result = object["result"] as? [String: Any]
      else { continue }
      let approval = result["approval"] as? [String: Any]
      let response = BridgeResult(
        status: result["status"] as? String ?? "error",
        message: result["message"] as? String ?? approval?["description"] as? String ?? "",
        runID: result["run_id"] as? String,
        taskID: result["task_id"] as? String,
        approvalID: approval?["id"] as? String,
        approvalDescription: approval?["description"] as? String,
        details: result
      )
      DispatchQueue.main.async { completion(.success(response)) }
    }
  }

  private func failPending(_ message: String) {
    let callbacks = pending.values
    pending.removeAll()
    for completion in callbacks {
      let error = NSError(domain: "Jarvis", code: 4, userInfo: [NSLocalizedDescriptionKey: message])
      DispatchQueue.main.async { completion(.failure(error)) }
    }
  }
}

enum ListenMode { case wake, command, approval }

final class SpeechController: NSObject, SFSpeechRecognizerDelegate {
  var onCommand: ((String) -> Void)?
  var onApproval: ((Bool) -> Void)?
  var onStatus: ((String) -> Void)?

  private let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
  private let engine = AVAudioEngine()
  private var request: SFSpeechAudioBufferRecognitionRequest?
  private var task: SFSpeechRecognitionTask?
  private var silenceTimer: Timer?
  private var commandTimeout: Timer?
  private var mode: ListenMode = .wake
  private var commandText = ""
  private var running = false

  override init() {
    super.init()
    recognizer?.delegate = self
  }

  func requestAccess(_ completion: @escaping (Bool, String?) -> Void) {
    SFSpeechRecognizer.requestAuthorization { speechStatus in
      AVCaptureDevice.requestAccess(for: .audio) { microphoneAllowed in
        DispatchQueue.main.async {
          guard speechStatus == .authorized else {
            completion(false, "Speech recognition permission is required.")
            return
          }
          guard microphoneAllowed else {
            completion(false, "Microphone permission is required.")
            return
          }
          completion(true, nil)
        }
      }
    }
  }

  func startWakeListening() {
    mode = .wake
    commandText = ""
    startRecognition()
  }

  func startApprovalListening() {
    mode = .approval
    commandText = ""
    startRecognition()
    onStatus?("Say approve or deny")
  }

  func stop() {
    running = false
    silenceTimer?.invalidate()
    commandTimeout?.invalidate()
    task?.cancel()
    task = nil
    request?.endAudio()
    request = nil
    if engine.isRunning { engine.stop() }
    engine.inputNode.removeTap(onBus: 0)
  }

  private func startRecognition() {
    stop()
    running = true
    let request = SFSpeechAudioBufferRecognitionRequest()
    request.shouldReportPartialResults = true
    request.taskHint = .dictation
    request.contextualStrings = ["Jarvis"]
    self.request = request
    let input = engine.inputNode
    let format = input.outputFormat(forBus: 0)
    input.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, _ in
      request.append(buffer)
    }
    engine.prepare()
    do { try engine.start() } catch {
      onStatus?("Microphone error")
      return
    }
    task = recognizer?.recognitionTask(with: request) { [weak self] result, error in
      DispatchQueue.main.async {
        guard let self, self.running else { return }
        if let result { self.handle(result.bestTranscription.formattedString) }
        if let error {
          NSLog("Jarvis speech recognition ended: %@", error.localizedDescription)
          FileHandle.standardError.write(Data("Jarvis speech error: \(error)\n".utf8))
          self.restartSoon()
        } else if result?.isFinal == true {
          self.restartSoon()
        }
      }
    }
    onStatus?(
      mode == .wake
        ? "Listening for Jarvis" : mode == .approval ? "Say approve or deny" : "Listening")
  }

  private func handle(_ transcript: String) {
    let lower = transcript.lowercased()
    switch mode {
    case .wake:
      guard let range = lower.range(of: "jarvis") else { return }
      mode = .command
      commandText = String(transcript[range.upperBound...]).trimmingCharacters(
        in: .whitespacesAndNewlines.union(.punctuationCharacters))
      NSSound(named: "Ping")?.play()
      onStatus?("Listening")
      scheduleCommandTimeout()
      if !commandText.isEmpty { scheduleFinalize() }
    case .command:
      var candidate = transcript
      if let range = lower.range(of: "jarvis") {
        candidate = String(transcript[range.upperBound...])
      }
      candidate = candidate.trimmingCharacters(
        in: .whitespacesAndNewlines.union(.punctuationCharacters))
      if !candidate.isEmpty && candidate != commandText {
        commandText = candidate
        scheduleFinalize()
      }
    case .approval:
      let yesWords = ["approve", "approved", "allow", "yes", "do it", "go ahead"]
      let noWords = ["deny", "denied", "no", "cancel", "stop"]
      if yesWords.contains(where: lower.contains) {
        stop()
        onApproval?(true)
      } else if noWords.contains(where: lower.contains) {
        stop()
        onApproval?(false)
      }
    }
  }

  private func scheduleFinalize() {
    silenceTimer?.invalidate()
    silenceTimer = Timer.scheduledTimer(withTimeInterval: 1.35, repeats: false) { [weak self] _ in
      guard let self, !self.commandText.isEmpty else { return }
      let command = self.commandText
      self.stop()
      self.onCommand?(command)
    }
  }

  private func scheduleCommandTimeout() {
    commandTimeout?.invalidate()
    commandTimeout = Timer.scheduledTimer(withTimeInterval: 10, repeats: false) { [weak self] _ in
      guard let self, self.mode == .command, self.commandText.isEmpty else { return }
      self.startWakeListening()
    }
  }

  private func restartSoon() {
    guard running else { return }
    task?.cancel()
    task = nil
    request?.endAudio()
    request = nil
    if engine.isRunning { engine.stop() }
    engine.inputNode.removeTap(onBus: 0)
    DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
      guard self?.running == true else { return }
      self?.startRecognition()
    }
  }
}

final class JarvisApp: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
  private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
  private lazy var menuBarImage: NSImage? = {
    guard let url = Bundle.main.url(forResource: "JarvisMenuIcon", withExtension: "png"),
      let image = NSImage(contentsOf: url)
    else { return nil }
    image.size = NSSize(width: 18, height: 18)
    image.isTemplate = true
    return image
  }()
  private let login = LoginController()
  private var backend: BackendController?
  private var liveVoice: RealtimeVoiceController?
  private var statusMenuItem = NSMenuItem()
  private var listeningItem = NSMenuItem()
  private var rateResultItem = NSMenuItem()
  private var reviewApprovalItem = NSMenuItem()
  private var lastRunID: String?
  private var activeTaskID: String?
  private var cachedMemories: [[String: Any]] = []
  private var cachedReminders: [[String: Any]] = []
  private var cachedAutomations: [[String: Any]] = []
  private var pendingApprovalID: String?
  private var pendingApprovalDescription: String?
  private lazy var setupController = SetupController(
    changeAPIKey: { [weak self] completion in self?.changeAPIKey(completion: completion) },
    enableVoice: { [weak self] completion in self?.requestSpeechAccess(completion: completion) },
    onComplete: { [weak self] in self?.completeSetup() }
  )

  func applicationDidFinishLaunching(_ notification: Notification) {
    ProcessInfo.processInfo.disableAutomaticTermination("Jarvis is an always-on menu-bar agent.")
    if let identity = jarvisIdentityImage() { NSApp.applicationIconImage = identity }
    NSLog("Jarvis startup: application finished launching")
    configureMenu()
    NSLog("Jarvis startup: menu configured")
    let notificationCenter = UNUserNotificationCenter.current()
    notificationCenter.delegate = self
    if !UserDefaults.standard.bool(forKey: "didInitializeLogin") {
      try? login.setEnabled(true)
      UserDefaults.standard.set(true, forKey: "didInitializeLogin")
    }
    refreshMenu()
    let key: String?
    NSLog("Jarvis startup: checking configured key source")
    if let importedKey = projectAPIKey() {
      NSLog("Jarvis startup: using configured project credential")
      key = importedKey
      if !UserDefaults.standard.bool(forKey: "didAttemptKeychainImport") {
        UserDefaults.standard.set(true, forKey: "didAttemptKeychainImport")
        DispatchQueue.global(qos: .utility).async {
          if saveAPIKey(importedKey) {
            NSLog("Jarvis startup: persisted configured credential to Keychain")
          } else {
            NSLog("Jarvis startup: Keychain persistence was unavailable")
          }
        }
      }
    } else if let storedKey = keychainAPIKey() {
      NSLog("Jarvis startup: found Keychain credential")
      key = storedKey
    } else {
      NSLog("Jarvis startup: requesting credential interactively")
      key = promptForAPIKey()
    }
    guard let key else {
      NSApp.terminate(nil)
      return
    }
    configureBackend(apiKey: key)
    configureLiveVoice(apiKey: key)
    // Launch as a normal foreground app so first-run dialogs are guaranteed to
    // appear, then become a Dock-less menu-bar app once setup is complete.
    NSApp.setActivationPolicy(.accessory)
    if !UserDefaults.standard.bool(forKey: "didCompleteSetup") {
      DispatchQueue.main.async { [weak self] in self?.showSetup() }
    } else if listeningEnabled {
      NSLog("Jarvis startup: requesting microphone access for live voice")
      requestSpeechAccess()
    }
  }

  private var listeningEnabled: Bool {
    get { !UserDefaults.standard.bool(forKey: "listeningDisabled") }
    set { UserDefaults.standard.set(!newValue, forKey: "listeningDisabled") }
  }

  private func configureMenu() {
    let menu = NSMenu()
    listeningItem = NSMenuItem(
      title: "Listening", action: #selector(toggleListening), keyEquivalent: "")
    listeningItem.target = self
    menu.addItem(listeningItem)
    menu.addItem(.separator())
    statusMenuItem = NSMenuItem(title: "Jarvis is starting…", action: nil, keyEquivalent: "")
    statusMenuItem.isEnabled = false
    menu.addItem(statusMenuItem)
    let askItem = NSMenuItem(title: "Ask Jarvis…", action: #selector(askJarvis), keyEquivalent: "")
    askItem.target = self
    menu.addItem(askItem)
    reviewApprovalItem = NSMenuItem(
      title: "Review Pending Action…", action: #selector(reviewPendingApproval), keyEquivalent: "")
    reviewApprovalItem.target = self
    reviewApprovalItem.isHidden = true
    menu.addItem(reviewApprovalItem)
    let activityItem = NSMenuItem(
      title: "View Recent Activity…", action: #selector(openTraceViewer), keyEquivalent: "")
    activityItem.target = self
    menu.addItem(activityItem)
    rateResultItem = NSMenuItem(
      title: "Rate Last Result…", action: #selector(rateLastResult), keyEquivalent: "")
    rateResultItem.target = self
    rateResultItem.isEnabled = false
    menu.addItem(rateResultItem)
    menu.addItem(.separator())
    let memoriesItem = NSMenuItem(
      title: "Saved Memories…", action: #selector(showMemories), keyEquivalent: "")
    memoriesItem.target = self
    menu.addItem(memoriesItem)
    let remindersItem = NSMenuItem(
      title: "Reminders…", action: #selector(showReminders), keyEquivalent: "")
    remindersItem.target = self
    menu.addItem(remindersItem)
    let automationsItem = NSMenuItem(
      title: "Automations…", action: #selector(showAutomations), keyEquivalent: "")
    automationsItem.target = self
    menu.addItem(automationsItem)
    let skillsItem = NSMenuItem(title: "Skills…", action: #selector(showSkills), keyEquivalent: "")
    skillsItem.target = self
    menu.addItem(skillsItem)
    let qualityItem = NSMenuItem(
      title: "Learning & Quality…", action: #selector(showQuality), keyEquivalent: "")
    qualityItem.target = self
    menu.addItem(qualityItem)
    menu.addItem(.separator())
    let setupItem = NSMenuItem(
      title: "Setup Jarvis…", action: #selector(showSetup), keyEquivalent: ",")
    setupItem.target = self
    menu.addItem(setupItem)
    let quit = NSMenuItem(title: "Quit Jarvis", action: #selector(quitApp), keyEquivalent: "q")
    quit.target = self
    menu.addItem(quit)
    statusItem.menu = menu
  }

  private func refreshMenu() {
    listeningItem.state = listeningEnabled ? .on : .off
    rateResultItem.isEnabled = lastRunID != nil
    statusMenuItem.title = listeningEnabled ? "Jarvis is listening" : "Jarvis is paused"
    if let image = menuBarImage {
      image.accessibilityDescription = "Jarvis"
      statusItem.button?.image = image
      statusItem.button?.imagePosition = .imageOnly
      statusItem.button?.title = ""
    } else {
      statusItem.button?.image = nil
      statusItem.button?.title = "J"
    }
    statusItem.button?.toolTip = listeningEnabled ? "Jarvis — listening" : "Jarvis — paused"
  }

  @objc private func toggleListening() {
    listeningEnabled.toggle()
    refreshMenu()
    if listeningEnabled { requestSpeechAccess() } else { liveVoice?.stop() }
  }

  @objc private func showSetup() {
    setupController.show(apiKeyConfigured: keychainAPIKey() != nil || projectAPIKey() != nil)
  }

  private func changeAPIKey(completion: @escaping (Bool) -> Void) {
    guard let key = promptForAPIKey(cancelTitle: "Cancel") else {
      completion(false)
      return
    }
    liveVoice?.stop()
    backend?.stop()
    configureBackend(apiKey: key)
    configureLiveVoice(apiKey: key)
    if listeningEnabled { requestSpeechAccess() }
    completion(true)
  }

  private func completeSetup() {
    UserDefaults.standard.set(true, forKey: "didCompleteSetup")
    if listeningEnabled,
      AVCaptureDevice.authorizationStatus(for: .audio) == .authorized
    {
      liveVoice?.start()
    }
  }

  @objc private func quitApp() {
    liveVoice?.stop()
    backend?.stop()
    NSApp.terminate(nil)
  }

  @objc private func reviewPendingApproval() {
    guard let approvalID = pendingApprovalID, let backend else { return }
    let alert = makeJarvisAlert()
    alert.alertStyle = .warning
    alert.messageText = "Jarvis needs approval"
    alert.informativeText = pendingApprovalDescription ?? "Review the requested action."
    alert.addButton(withTitle: "Approve")
    alert.addButton(withTitle: "Deny")
    alert.addButton(withTitle: "Not Now")
    let response = alert.runModal()
    guard response != .alertThirdButtonReturn else { return }
    backend.approve(approvalID, approved: response == .alertFirstButtonReturn) {
      [weak self] result in
      guard let self else { return }
      if case .failure(let error) = result {
        self.showError(error.localizedDescription)
        return
      }
      self.pendingApprovalID = nil
      self.pendingApprovalDescription = nil
      self.reviewApprovalItem.isHidden = true
    }
  }

  @objc private func askJarvis() {
    guard let backend else { return }
    let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 420, height: 24))
    field.placeholderString = "What would you like Jarvis to do?"
    let alert = makeJarvisAlert()
    alert.messageText = "Ask Jarvis"
    alert.accessoryView = field
    alert.addButton(withTitle: "Send")
    alert.addButton(withTitle: "Cancel")
    guard alert.runModal() == .alertFirstButtonReturn else { return }
    let request = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !request.isEmpty else { return }
    setStatus("Working — say stop to cancel")
    backend.chat(request) { [weak self] result in
      guard let self else { return }
      switch result {
      case .failure(let error): self.showError(error.localizedDescription)
      case .success(let response):
        if let runID = response.runID {
          self.lastRunID = runID
          self.refreshMenu()
        }
        if let taskID = response.taskID { self.activeTaskID = taskID }
        if response.status == "approval_required", let approvalID = response.approvalID {
          self.pendingApprovalID = approvalID
          self.pendingApprovalDescription = response.approvalDescription ?? response.message
          self.reviewApprovalItem.isHidden = false
          self.reviewPendingApproval()
        } else {
          let answer = makeJarvisAlert()
          answer.messageText = "Jarvis"
          answer.informativeText = response.message
          answer.addButton(withTitle: "Useful")
          answer.addButton(withTitle: "Correct…")
          answer.addButton(withTitle: "Done")
          let rating = answer.runModal()
          if rating == .alertFirstButtonReturn {
            self.saveFeedback(useful: true, correction: nil)
          } else if rating == .alertSecondButtonReturn, let correction = self.promptForCorrection()
          {
            self.saveFeedback(useful: false, correction: correction)
          }
        }
      }
    }
  }

  @objc private func rateLastResult() {
    guard lastRunID != nil else {
      showError("There is no completed Jarvis result to rate yet.")
      return
    }
    let alert = makeJarvisAlert()
    alert.messageText = "Help Jarvis learn"
    alert.informativeText =
      "Your explicit feedback controls whether this result can enter a future training dataset."
    alert.addButton(withTitle: "Useful")
    alert.addButton(withTitle: "Correct…")
    alert.addButton(withTitle: "Not Useful")
    let response = alert.runModal()
    if response == .alertFirstButtonReturn {
      saveFeedback(useful: true, correction: nil)
    } else if response == .alertSecondButtonReturn, let correction = promptForCorrection() {
      saveFeedback(useful: false, correction: correction)
    } else if response == .alertThirdButtonReturn {
      saveFeedback(useful: false, correction: nil)
    }
  }

  private func promptForCorrection() -> String? {
    let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 420, height: 24))
    field.placeholderString = "What should Jarvis have done or said?"
    let alert = makeJarvisAlert()
    alert.messageText = "Correct Jarvis"
    alert.informativeText =
      "This correction becomes the preferred target and a durable regression case."
    alert.accessoryView = field
    alert.addButton(withTitle: "Save Correction")
    alert.addButton(withTitle: "Cancel")
    guard alert.runModal() == .alertFirstButtonReturn else { return nil }
    let correction = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
    return correction.isEmpty ? nil : correction
  }

  private func saveFeedback(useful: Bool, correction: String?) {
    guard let runID = lastRunID, let backend else { return }
    backend.feedback(runID: runID, rating: useful ? 1 : -1, correction: correction) {
      [weak self] result in
      switch result {
      case .success: self?.setStatus(correction == nil ? "Feedback saved" : "Correction saved")
      case .failure(let error): self?.showError(error.localizedDescription)
      }
    }
  }

  @objc private func openTraceViewer() {
    guard let backend else { return }
    backend.traceViewer { [weak self] result in
      switch result {
      case .failure(let error): self?.showError(error.localizedDescription)
      case .success(let response):
        guard let value = response.details["url"] as? String, let url = URL(string: value) else {
          self?.showError("The local trace viewer URL is unavailable.")
          return
        }
        NSWorkspace.shared.open(url)
      }
    }
  }

  @objc private func showMemories() {
    guard let backend else { return }
    backend.listMemories { [weak self] result in
      guard let self else { return }
      switch result {
      case .failure(let error): self.showError(error.localizedDescription)
      case .success(let response):
        self.cachedMemories = response.details["memories"] as? [[String: Any]] ?? []
        let lines = self.cachedMemories.enumerated().map { index, item in
          let kind = item["kind"] as? String ?? "memory"
          let content = item["content"] as? String ?? ""
          return "\(index + 1). [\(kind)] \(content)"
        }
        let alert = makeJarvisAlert()
        alert.messageText = "Saved Memories"
        alert.informativeText =
          lines.isEmpty ? "Jarvis has no saved memories yet." : lines.joined(separator: "\n\n")
        alert.addButton(withTitle: "Done")
        if !lines.isEmpty { alert.addButton(withTitle: "Forget One…") }
        if alert.runModal() == .alertSecondButtonReturn { self.promptToForgetMemory() }
      }
    }
  }

  private func promptToForgetMemory() {
    guard !cachedMemories.isEmpty, let backend else { return }
    guard
      let number = promptForItemNumber(
        title: "Forget a Memory",
        message: "Enter the number shown beside the memory to remove it.",
        maximum: cachedMemories.count
      )
    else { return }
    guard let id = cachedMemories[number - 1]["id"] as? String else { return }
    backend.forgetMemory(id) { [weak self] result in
      if case .failure(let error) = result { self?.showError(error.localizedDescription) }
    }
  }

  @objc private func showReminders() {
    guard let backend else { return }
    backend.listReminders { [weak self] result in
      guard let self else { return }
      switch result {
      case .failure(let error): self.showError(error.localizedDescription)
      case .success(let response):
        self.cachedReminders = response.details["reminders"] as? [[String: Any]] ?? []
        let lines = self.cachedReminders.enumerated().map { index, item in
          let message = item["message"] as? String ?? "Reminder"
          let dueAt = item["due_at"] as? String ?? ""
          return "\(index + 1). \(message)\n   \(dueAt)"
        }
        let alert = makeJarvisAlert()
        alert.messageText = "Active Reminders"
        alert.informativeText =
          lines.isEmpty ? "Jarvis has no active reminders." : lines.joined(separator: "\n\n")
        alert.addButton(withTitle: "Done")
        if !lines.isEmpty { alert.addButton(withTitle: "Cancel One…") }
        if alert.runModal() == .alertSecondButtonReturn { self.promptToCancelReminder() }
      }
    }
  }

  private func promptToCancelReminder() {
    guard !cachedReminders.isEmpty, let backend else { return }
    guard
      let number = promptForItemNumber(
        title: "Cancel a Reminder",
        message: "Enter the number shown beside the reminder to cancel it.",
        maximum: cachedReminders.count
      )
    else { return }
    guard let id = cachedReminders[number - 1]["id"] as? String else { return }
    backend.cancelReminder(id) { [weak self] result in
      if case .failure(let error) = result { self?.showError(error.localizedDescription) }
    }
  }

  @objc private func showAutomations() {
    guard let backend else { return }
    backend.listAutomations { [weak self] result in
      guard let self else { return }
      switch result {
      case .failure(let error): self.showError(error.localizedDescription)
      case .success(let response):
        self.cachedAutomations = response.details["automations"] as? [[String: Any]] ?? []
        let lines = self.cachedAutomations.enumerated().map { index, item in
          let name = item["name"] as? String ?? "Automation"
          let status = item["status"] as? String ?? "unknown"
          let next = item["next_run_at"] as? String ?? ""
          return "\(index + 1). \(name) [\(status)]\n   Next: \(next)"
        }
        let alert = makeJarvisAlert()
        alert.messageText = "Agent Automations"
        alert.informativeText =
          lines.isEmpty ? "Jarvis has no active automations." : lines.joined(separator: "\n\n")
        alert.addButton(withTitle: "Done")
        if !lines.isEmpty { alert.addButton(withTitle: "Manage One…") }
        if alert.runModal() == .alertSecondButtonReturn { self.promptToManageAutomation() }
      }
    }
  }

  private func promptToManageAutomation() {
    guard !cachedAutomations.isEmpty, let backend else { return }
    guard
      let number = promptForItemNumber(
        title: "Manage an Automation",
        message: "Enter the number shown beside the automation.",
        maximum: cachedAutomations.count
      )
    else { return }
    let item = cachedAutomations[number - 1]
    guard let id = item["id"] as? String else { return }
    let currentStatus = item["status"] as? String ?? "active"
    let alert = makeJarvisAlert()
    alert.messageText = item["name"] as? String ?? "Automation"
    if currentStatus == "paused" {
      alert.addButton(withTitle: "Resume")
    } else {
      alert.addButton(withTitle: "Pause")
    }
    alert.addButton(withTitle: "Cancel Automation")
    alert.addButton(withTitle: "Keep")
    let response = alert.runModal()
    guard response != .alertThirdButtonReturn else { return }
    let operation =
      response == .alertSecondButtonReturn
      ? "cancel" : (currentStatus == "paused" ? "resume" : "pause")
    backend.manageAutomation(id, operation: operation) { [weak self] result in
      if case .failure(let error) = result { self?.showError(error.localizedDescription) }
    }
  }

  @objc private func showSkills() {
    guard let backend else { return }
    backend.listSkills { [weak self] result in
      guard let self else { return }
      switch result {
      case .failure(let error): self.showError(error.localizedDescription)
      case .success(let response):
        let skills = response.details["skills"] as? [[String: Any]] ?? []
        let lines = skills.map { item in
          let title = item["title"] as? String ?? "Skill"
          let name = item["name"] as? String ?? ""
          return "• \(title) (\(name))"
        }
        let alert = makeJarvisAlert()
        alert.messageText = "Jarvis Skills"
        alert.informativeText =
          lines.isEmpty ? "No skills are installed." : lines.joined(separator: "\n\n")
        alert.addButton(withTitle: "Done")
        alert.runModal()
      }
    }
  }

  @objc private func showQuality() {
    guard let backend else { return }
    backend.qualityReport { [weak self] result in
      guard let self else { return }
      switch result {
      case .failure(let error): self.showError(error.localizedDescription)
      case .success(let response):
        let metrics = response.details["metrics"] as? [String: Any] ?? [:]
        let total = metrics["total"] as? Int ?? 0
        let eligible = metrics["training_eligible"] as? Int ?? 0
        let positive = metrics["positive"] as? Int ?? 0
        let negative = metrics["negative"] as? Int ?? 0
        let failed = metrics["failed"] as? Int ?? 0
        let alert = makeJarvisAlert()
        alert.messageText = "Jarvis Learning & Quality"
        alert.informativeText = """
          Completed history: \(total) runs
          Explicitly useful: \(positive)
          Explicitly negative: \(negative)
          Failed runs: \(failed)
          Training-eligible examples: \(eligible)
          """
        alert.addButton(withTitle: "Done")
        alert.addButton(withTitle: "Open Local Trace Viewer")
        if alert.runModal() == .alertSecondButtonReturn { self.openTraceViewer() }
      }
    }
  }

  private func promptForItemNumber(title: String, message: String, maximum: Int) -> Int? {
    let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 220, height: 24))
    field.placeholderString = "1–\(maximum)"
    let alert = makeJarvisAlert()
    alert.messageText = title
    alert.informativeText = message
    alert.accessoryView = field
    alert.addButton(withTitle: "Continue")
    alert.addButton(withTitle: "Cancel")
    guard alert.runModal() == .alertFirstButtonReturn,
      let number = Int(field.stringValue),
      (1...maximum).contains(number)
    else { return nil }
    return number
  }

  private func deliverReminder(_ message: String, id: String) {
    deliverNotification(title: "Jarvis Reminder", message: message, id: "jarvis-reminder-\(id)")
  }

  private func deliverNotification(title: String, message: String, id: String) {
    let content = UNMutableNotificationContent()
    content.title = title
    content.body = message
    content.sound = .default
    let trigger = UNTimeIntervalNotificationTrigger(timeInterval: 0.1, repeats: false)
    UNUserNotificationCenter.current().add(
      UNNotificationRequest(identifier: id, content: content, trigger: trigger)
    ) { [weak self] error in
      guard error != nil else { return }
      DispatchQueue.main.async {
        self?.setStatus("\(title): \(message)")
        NSSound.beep()
        NSApp.requestUserAttention(.informationalRequest)
      }
    }
  }

  func userNotificationCenter(
    _ center: UNUserNotificationCenter,
    willPresent notification: UNNotification,
    withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
  ) {
    completionHandler([.banner, .sound])
  }

  private func requestSpeechAccess(completion: ((Bool) -> Void)? = nil) {
    AVCaptureDevice.requestAccess(for: .audio) { [weak self] allowed in
      DispatchQueue.main.async {
        guard let self else { return }
        if allowed {
          self.listeningEnabled = true
          self.refreshMenu()
          self.liveVoice?.start()
        } else {
          self.listeningEnabled = false
          self.refreshMenu()
          self.showError("Microphone permission is required for live voice.")
        }
        completion?(allowed)
      }
    }
  }

  private func configureLiveVoice(apiKey: String) {
    let voice = RealtimeVoiceController(apiKey: apiKey)
    voice.onStatus = { [weak self] status in self?.setStatus(status) }
    voice.onToolCall = { [weak self] name, arguments, completion in
      self?.handleLiveTool(name: name, arguments: arguments, completion: completion)
    }
    voice.onCompletedTurn = { [weak self] userText, assistantText, metadata in
      guard let self, let backend = self.backend else { return }
      backend.recordRealtimeTurn(
        userText: userText, assistantText: assistantText, metadata: metadata
      ) { [weak self] result in
        if case .success(let response) = result, let runID = response.runID {
          self?.lastRunID = runID
          self?.refreshMenu()
        }
      }
    }
    liveVoice = voice
  }

  private func configureBackend(apiKey: String) {
    let controller = BackendController(apiKey: apiKey)
    controller.onEvent = { [weak self] event in
      guard let self else { return }
      let status = event["status"] as? String ?? ""
      let message = event["message"] as? String ?? "Jarvis Core"
      let taskID = event["task_id"] as? String
      if status == "reminder" {
        let reminderID = event["reminder_id"] as? String ?? UUID().uuidString
        self.deliverReminder(message, id: reminderID)
        self.setStatus("Reminder: \(message)")
        return
      }
      if status == "automation_completed" {
        let automationID = event["automation_id"] as? String ?? UUID().uuidString
        let name = event["automation_name"] as? String ?? "Automation"
        self.deliverNotification(
          title: "Jarvis — \(name)",
          message: message,
          id: "jarvis-automation-\(automationID)"
        )
        self.setStatus("Automation completed")
        return
      }
      if status == "automation_failed" {
        let automationID = event["automation_id"] as? String ?? UUID().uuidString
        self.deliverNotification(
          title: "Jarvis automation failed",
          message: message,
          id: "jarvis-automation-failed-\(automationID)"
        )
        self.setStatus("Automation failed")
        return
      }
      if ["running", "awaiting_approval"].contains(status), let taskID {
        self.activeTaskID = taskID
      } else if ["completed", "failed", "cancelled"].contains(status), taskID == self.activeTaskID {
        self.activeTaskID = nil
      }
      if status == "ready" {
        if self.listeningEnabled { self.setStatus("Listening — live") }
      } else if status == "running" {
        self.setStatus("Working — say stop to cancel")
      } else if status == "awaiting_approval" {
        if let approvalID = event["approval_id"] as? String {
          self.pendingApprovalID = approvalID
          self.pendingApprovalDescription = message
          self.reviewApprovalItem.isHidden = false
          self.deliverNotification(
            title: "Jarvis needs approval",
            message: message,
            id: "jarvis-approval-\(approvalID)"
          )
        }
        self.setStatus("Approval needed")
      } else if status == "failed" {
        self.setStatus("Task failed: \(message)")
      } else if ["completed", "cancelled"].contains(status) {
        self.setStatus(self.listeningEnabled ? "Listening — live" : "Jarvis is paused")
      }
    }
    backend = controller
    controller.status { [weak self] result in
      switch result {
      case .success:
        if self?.listeningEnabled == true { self?.setStatus("Listening — live") }
      case .failure(let error):
        self?.setStatus("Jarvis Core error: \(error.localizedDescription)")
      }
    }
  }

  private func handleLiveTool(
    name: String,
    arguments: [String: Any],
    completion: @escaping ([String: Any]) -> Void
  ) {
    guard let backend else {
      completion(["ok": false, "error": "The local agent is unavailable."])
      return
    }
    let bridgeCompletion: (Result<BridgeResult, Error>) -> Void = { [weak self] result in
      switch result {
      case .failure(let error):
        completion(["ok": false, "error": error.localizedDescription])
      case .success(let response):
        if let runID = response.runID {
          self?.lastRunID = runID
          self?.refreshMenu()
        }
        if let taskID = response.taskID { self?.activeTaskID = taskID }
        var payload = response.details
        payload["ok"] = response.status != "error"
        payload["status"] = response.status
        payload["message"] = response.message
        payload.merge([
          "ok": response.status != "error",
          "status": response.status,
          "message": response.message,
        ]) { _, new in new }
        if let runID = response.runID { payload["run_id"] = runID }
        if let taskID = response.taskID { payload["task_id"] = taskID }
        if let approvalID = response.approvalID { payload["approval_id"] = approvalID }
        if let description = response.approvalDescription {
          payload["approval_description"] = description
        }
        completion(payload)
      }
    }

    switch name {
    case "run_local_agent":
      guard let request = arguments["request"] as? String, !request.isEmpty else {
        completion(["ok": false, "error": "The local request was empty."])
        return
      }
      backend.chat(request, completion: bridgeCompletion)
    case "resolve_local_approval":
      guard let approvalID = arguments["approval_id"] as? String,
        let approved = arguments["approved"] as? Bool
      else {
        completion(["ok": false, "error": "Approval details were incomplete."])
        return
      }
      backend.approve(approvalID, approved: approved, completion: bridgeCompletion)
    case "get_agent_status":
      backend.status(completion: bridgeCompletion)
    case "cancel_agent_task":
      backend.cancel(taskID: activeTaskID, completion: bridgeCompletion)
    case "remember_memory":
      guard let content = arguments["content"] as? String, !content.isEmpty else {
        completion(["ok": false, "error": "The memory was empty."])
        return
      }
      let kind = arguments["kind"] as? String ?? "fact"
      backend.remember(content, kind: kind) { result in
        switch result {
        case .success(let response):
          let memory = response.details["memory"] as? [String: Any]
          completion([
            "ok": true,
            "status": response.status,
            "message": "Saved locally.",
            "memory_id": memory?["id"] as? String ?? "",
          ])
        case .failure(let error):
          completion(["ok": false, "error": error.localizedDescription])
        }
      }
    case "schedule_reminder":
      guard let message = arguments["message"] as? String,
        let dueAt = arguments["due_at"] as? String
      else {
        completion(["ok": false, "error": "Reminder details were incomplete."])
        return
      }
      let repeatSeconds = arguments["repeat_seconds"] as? Int
      backend.scheduleReminder(
        message: message,
        dueAt: dueAt,
        repeatSeconds: repeatSeconds,
        completion: bridgeCompletion
      )
    case "list_reminders":
      backend.listReminders(completion: bridgeCompletion)
    case "cancel_reminder":
      guard let reminderID = arguments["reminder_id"] as? String else {
        completion(["ok": false, "error": "Reminder ID was missing."])
        return
      }
      backend.cancelReminder(reminderID, completion: bridgeCompletion)
    case "schedule_agent_automation":
      guard let name = arguments["name"] as? String,
        let prompt = arguments["prompt"] as? String,
        let scheduleType = arguments["schedule_type"] as? String
      else {
        completion(["ok": false, "error": "Automation details were incomplete."])
        return
      }
      backend.createAutomation(
        name: name,
        prompt: prompt,
        scheduleType: scheduleType,
        runAt: arguments["run_at"] as? String,
        intervalSeconds: arguments["interval_seconds"] as? Int,
        localTime: arguments["local_time"] as? String,
        timezone: arguments["timezone"] as? String,
        completion: bridgeCompletion
      )
    case "list_agent_automations":
      backend.listAutomations(completion: bridgeCompletion)
    case "manage_agent_automation":
      guard let automationID = arguments["automation_id"] as? String,
        let operation = arguments["operation"] as? String
      else {
        completion(["ok": false, "error": "Automation details were incomplete."])
        return
      }
      backend.manageAutomation(automationID, operation: operation, completion: bridgeCompletion)
    case "rate_last_turn":
      guard let runID = lastRunID else {
        completion(["ok": false, "error": "There is no completed turn to rate yet."])
        return
      }
      let useful = arguments["useful"] as? Bool ?? false
      let correction = arguments["correction"] as? String
      backend.feedback(runID: runID, rating: useful ? 1 : -1, correction: correction) { result in
        switch result {
        case .success: completion(["ok": true, "message": "Feedback saved."])
        case .failure(let error): completion(["ok": false, "error": error.localizedDescription])
        }
      }
    default:
      completion(["ok": false, "error": "Unknown live tool: \(name)"])
    }
  }

  private func setStatus(_ value: String) {
    statusMenuItem.title = value
    statusItem.button?.toolTip = value
  }

  private func promptForAPIKey(cancelTitle: String = "Quit") -> String? {
    let field = NSSecureTextField(frame: NSRect(x: 0, y: 0, width: 360, height: 24))
    field.placeholderString = "sk-…"
    let alert = makeJarvisAlert()
    alert.messageText = "Connect Jarvis to OpenAI"
    alert.informativeText =
      "Enter your OpenAI API key. It will be stored in macOS Keychain, not in the project."
    alert.accessoryView = field
    alert.addButton(withTitle: "Save")
    alert.addButton(withTitle: cancelTitle)

    // LSUIElement menu-bar apps do not normally become the active app. Temporarily
    // adopt a regular activation policy so macOS reliably presents this dialog.
    let previousPolicy = NSApp.activationPolicy()
    NSApp.setActivationPolicy(.regular)
    NSApp.unhide(nil)
    NSRunningApplication.current.activate(options: [.activateAllWindows])
    alert.window.level = .floating
    alert.window.collectionBehavior.insert(.canJoinAllSpaces)
    alert.window.center()
    alert.window.makeKeyAndOrderFront(nil)
    alert.window.orderFrontRegardless()
    defer { NSApp.setActivationPolicy(previousPolicy) }

    guard alert.runModal() == .alertFirstButtonReturn else { return nil }
    let value = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !value.isEmpty, saveAPIKey(value) else {
      showError("The API key could not be saved to Keychain.")
      return nil
    }
    return value
  }

  private func showError(_ message: String) {
    let alert = makeJarvisAlert()
    alert.alertStyle = .warning
    alert.messageText = "Jarvis"
    alert.informativeText = message
    alert.runModal()
  }
}

private func configureApplicationMenu(_ application: NSApplication) {
  let mainMenu = NSMenu()

  let applicationMenuItem = NSMenuItem()
  let applicationMenu = NSMenu(title: "Jarvis")
  applicationMenu.addItem(
    withTitle: "Quit Jarvis",
    action: #selector(NSApplication.terminate(_:)),
    keyEquivalent: "q"
  )
  applicationMenuItem.submenu = applicationMenu
  mainMenu.addItem(applicationMenuItem)

  let editMenuItem = NSMenuItem()
  let editMenu = NSMenu(title: "Edit")
  editMenu.addItem(withTitle: "Undo", action: #selector(UndoManager.undo), keyEquivalent: "z")
  editMenu.addItem(.separator())
  editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
  editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
  editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
  editMenu.addItem(.separator())
  editMenu.addItem(
    withTitle: "Select All",
    action: #selector(NSText.selectAll(_:)),
    keyEquivalent: "a"
  )
  editMenuItem.submenu = editMenu
  mainMenu.addItem(editMenuItem)

  application.mainMenu = mainMenu
}

@main
enum JarvisMain {
  @MainActor
  static func main() {
    let application = NSApplication.shared
    configureApplicationMenu(application)
    let delegate = JarvisApp()
    application.delegate = delegate
    application.run()
    withExtendedLifetime(delegate) {}
  }
}
