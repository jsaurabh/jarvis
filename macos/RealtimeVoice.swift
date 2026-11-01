import AVFoundation
import Foundation

let jarvisRealtimeModel = "gpt-realtime-2.1-mini"

final class RealtimeVoiceController: NSObject, URLSessionWebSocketDelegate {
  typealias ToolHandler = (String, [String: Any], @escaping ([String: Any]) -> Void) -> Void

  var onStatus: ((String) -> Void)?
  var onToolCall: ToolHandler?
  var onCompletedTurn: ((String, String, [String: Any]) -> Void)?

  private let apiKey: String
  private var urlSession: URLSession?
  private var socket: URLSessionWebSocketTask?
  private var reconnectWorkItem: DispatchWorkItem?
  private var keepaliveTimer: Timer?
  private var running = false
  private var reconnectPending = false

  private let audioEngine = AVAudioEngine()
  private let player = AVAudioPlayerNode()
  private var inputTapInstalled = false
  private var playerAttached = false
  private let playbackFormat = AVAudioFormat(standardFormatWithSampleRate: 24_000, channels: 1)!
  private let audioSendQueue = DispatchQueue(label: "com.cray.jarvis.audio-upload")
  private var pendingMicrophoneAudio = Data()
  private var queuedAudioChunks: [Data] = []
  private var audioSendInFlight = false
  private var lastMicrophoneLevelLog = Date.distantPast

  private var currentAssistantItemID: String?
  private var currentAssistantContentIndex = 0
  private var playbackStartedAt: Date?
  private var scheduledAudioMilliseconds = 0
  private var interruptedItems = Set<String>()
  private var interruptionCount = 0

  private let traceLedger = VoiceTraceLedger()
  private var responseTranscripts: [String: String] = [:]
  private var turnToolItems: [[String: Any]] = []
  private var speechStartedAt: Date?
  private var speechStoppedAt: Date?
  private var firstAudioAt: Date?
  private var realtimeHarness: [String: Any] = [:]

  init(apiKey: String) {
    self.apiKey = apiKey
    super.init()
    traceLedger.emit = { [weak self] user, answer, metadata in
      self?.onCompletedTurn?(user, answer, metadata)
    }
  }

  func start() {
    guard !running else { return }
    running = true
    reconnectPending = false
    connect()
  }

  func stop() {
    running = false
    reconnectPending = false
    reconnectWorkItem?.cancel()
    reconnectWorkItem = nil
    keepaliveTimer?.invalidate()
    keepaliveTimer = nil
    traceLedger.disconnected()
    stopAudio()
    socket?.cancel(with: .goingAway, reason: nil)
    socket = nil
    urlSession?.invalidateAndCancel()
    urlSession = nil
    onStatus?("Paused")
  }

  private func connect() {
    guard running else { return }
    onStatus?("Connecting live voice")
    var components = URLComponents(string: "wss://api.openai.com/v1/realtime")!
    components.queryItems = [URLQueryItem(name: "model", value: jarvisRealtimeModel)]
    var request = URLRequest(url: components.url!)
    request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
    request.timeoutInterval = 30

    let queue = OperationQueue()
    queue.maxConcurrentOperationCount = 1
    let session = URLSession(configuration: .default, delegate: self, delegateQueue: queue)
    let task = session.webSocketTask(with: request)
    urlSession = session
    socket = task
    task.resume()
    receiveNext(from: task)
  }

  func urlSession(
    _ session: URLSession,
    webSocketTask: URLSessionWebSocketTask,
    didOpenWithProtocol protocol: String?
  ) {
    guard running, webSocketTask === socket else { return }
    NSLog("Jarvis Realtime connected with %@", jarvisRealtimeModel)
    reconnectPending = false
    sendSessionConfiguration()
    DispatchQueue.main.async { [weak self] in
      guard let self, self.running else { return }
      self.startKeepalive(for: webSocketTask)
      do {
        try self.startAudio()
      } catch {
        self.onStatus?("Audio error: \(error.localizedDescription)")
        self.scheduleReconnect()
      }
    }
  }

  func urlSession(
    _ session: URLSession,
    webSocketTask: URLSessionWebSocketTask,
    didCloseWith closeCode: URLSessionWebSocketTask.CloseCode,
    reason: Data?
  ) {
    guard webSocketTask === socket else { return }
    let closeReason = reason.flatMap { String(data: $0, encoding: .utf8) } ?? ""
    NSLog("Jarvis Realtime closed: code=%ld reason=%@", closeCode.rawValue, closeReason)
    scheduleReconnect()
  }

  private func startKeepalive(for task: URLSessionWebSocketTask) {
    keepaliveTimer?.invalidate()
    keepaliveTimer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) {
      [weak self, weak task] _ in
      guard let self, self.running, let task, task === self.socket else { return }
      task.sendPing { [weak self, weak task] error in
        guard let self, let task, task === self.socket else { return }
        if let error {
          NSLog("Jarvis Realtime heartbeat failed: %@", error.localizedDescription)
          self.scheduleReconnect()
        }
      }
    }
  }

  private func receiveNext(from task: URLSessionWebSocketTask) {
    task.receive { [weak self, weak task] result in
      guard let self, let task, task === self.socket else { return }
      switch result {
      case .failure(let error):
        NSLog("Jarvis Realtime receive error: %@", error.localizedDescription)
        self.scheduleReconnect()
      case .success(let message):
        let data: Data?
        switch message {
        case .string(let text): data = text.data(using: .utf8)
        case .data(let value): data = value
        @unknown default: data = nil
        }
        if let data,
          let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        {
          DispatchQueue.main.async { [weak self] in self?.handleServerEvent(object) }
        }
        self.receiveNext(from: task)
      }
    }
  }

  private func sendSessionConfiguration() {
    let currentTimestamp = ISO8601DateFormatter().string(from: Date())
    let currentTimeZone = TimeZone.current.identifier
    let instructions = """
      You are Jarvis, a private personal voice assistant on the user's Mac.
      Speak naturally, warmly, and concisely. Start useful speech immediately; do not repeat the user's request.
      Treat speech as addressed to you when it begins with "Jarvis" or clearly continues the active conversation. Ignore unrelated ambient speech without responding.
      The user may interrupt you at any time. Yield immediately and respond to the new request.
      For ordinary conversation, answer directly.
      For anything that depends on files, applications, devices, mounted drives, current web information, or taking an action, call run_local_agent with the complete request. Never claim that local work succeeded unless its result says so.
      If run_local_agent returns approval_required, tell the user to review the request in the Jarvis menu. You cannot approve actions yourself.
      If the user asks what you are doing, call get_agent_status. If the user asks you to stop or cancel ongoing work, call cancel_agent_task.
      If the user explicitly says to remember something, call remember_memory. Do not save inferred facts or sensitive information the user did not explicitly ask you to remember. Saved memories are local and are not automatically added to conversations.
      If the user asks to create, list, or cancel a reminder, use the reminder tools. Current time is \(currentTimestamp), timezone \(currentTimeZone). Convert relative times to an ISO 8601 timestamp with an explicit offset; ask if the requested time is ambiguous.
      If the user asks Jarvis to do agent work later or repeatedly, use schedule_agent_automation instead of a reminder. A reminder only notifies; an automation runs the full local agent. Describe the schedule and work precisely. Use at least five minutes for interval schedules.
      If the user marks the last answer useful or gives a correction, call rate_last_turn.
      """
    let tools: [[String: Any]] = [
      [
        "type": "function",
        "name": "run_local_agent",
        "description":
          "Delegate file, device, command, mounted-drive, web-search, or other tool-dependent work to the private local agent.",
        "parameters": [
          "type": "object",
          "properties": [
            "request": ["type": "string", "description": "The user's complete request."]
          ],
          "required": ["request"],
          "additionalProperties": false,
        ],
      ],
      [
        "type": "function",
        "name": "get_agent_status",
        "description": "Get the current durable task and progress status from Jarvis Core.",
        "parameters": [
          "type": "object",
          "properties": [:],
          "required": [],
          "additionalProperties": false,
        ],
      ],
      [
        "type": "function",
        "name": "cancel_agent_task",
        "description": "Cancel the most recent active Jarvis Core task.",
        "parameters": [
          "type": "object",
          "properties": [:],
          "required": [],
          "additionalProperties": false,
        ],
      ],
      [
        "type": "function",
        "name": "remember_memory",
        "description": "Save a fact locally only when the user explicitly says to remember it.",
        "parameters": [
          "type": "object",
          "properties": [
            "content": ["type": "string"],
            "kind": [
              "type": "string",
              "enum": ["fact", "preference", "person", "routine", "project", "procedure"],
            ],
          ],
          "required": ["content", "kind"],
          "additionalProperties": false,
        ],
      ],
      [
        "type": "function",
        "name": "schedule_reminder",
        "description": "Schedule a durable local reminder at an absolute ISO 8601 time.",
        "parameters": [
          "type": "object",
          "properties": [
            "message": ["type": "string"],
            "due_at": [
              "type": "string", "description": "ISO 8601 date-time with an explicit UTC offset.",
            ],
            "repeat_seconds": ["type": ["integer", "null"]],
          ],
          "required": ["message", "due_at", "repeat_seconds"],
          "additionalProperties": false,
        ],
      ],
      [
        "type": "function",
        "name": "list_reminders",
        "description": "List the user's active local reminders after they ask.",
        "parameters": [
          "type": "object",
          "properties": [:],
          "required": [],
          "additionalProperties": false,
        ],
      ],
      [
        "type": "function",
        "name": "cancel_reminder",
        "description": "Cancel a specific reminder selected from the active reminder list.",
        "parameters": [
          "type": "object",
          "properties": ["reminder_id": ["type": "string"]],
          "required": ["reminder_id"],
          "additionalProperties": false,
        ],
      ],
      [
        "type": "function",
        "name": "schedule_agent_automation",
        "description":
          "Schedule the full Jarvis agent to perform work once, at an interval, or daily.",
        "parameters": [
          "type": "object",
          "properties": [
            "name": ["type": "string"],
            "prompt": [
              "type": "string",
              "description": "Complete instructions for each scheduled agent run.",
            ],
            "schedule_type": ["type": "string", "enum": ["once", "interval", "daily"]],
            "run_at": [
              "type": ["string", "null"], "description": "ISO 8601 timestamp for once schedules.",
            ],
            "interval_seconds": ["type": ["integer", "null"]],
            "local_time": [
              "type": ["string", "null"], "description": "HH:MM for daily schedules.",
            ],
            "timezone": [
              "type": ["string", "null"], "description": "IANA timezone for daily schedules.",
            ],
          ],
          "required": [
            "name", "prompt", "schedule_type", "run_at", "interval_seconds", "local_time",
            "timezone",
          ],
          "additionalProperties": false,
        ],
      ],
      [
        "type": "function",
        "name": "list_agent_automations",
        "description": "List active scheduled Jarvis agent work after the user asks.",
        "parameters": [
          "type": "object",
          "properties": [:],
          "required": [],
          "additionalProperties": false,
        ],
      ],
      [
        "type": "function",
        "name": "manage_agent_automation",
        "description": "Pause, resume, or cancel one scheduled Jarvis automation.",
        "parameters": [
          "type": "object",
          "properties": [
            "automation_id": ["type": "string"],
            "operation": ["type": "string", "enum": ["pause", "resume", "cancel"]],
          ],
          "required": ["automation_id", "operation"],
          "additionalProperties": false,
        ],
      ],
      [
        "type": "function",
        "name": "rate_last_turn",
        "description": "Save explicit user feedback about the most recently completed Jarvis turn.",
        "parameters": [
          "type": "object",
          "properties": [
            "useful": ["type": "boolean"],
            "correction": ["type": ["string", "null"]],
          ],
          "required": ["useful", "correction"],
          "additionalProperties": false,
        ],
      ],
    ]
    realtimeHarness = [
      "version": "realtime-voice-v2",
      "instructions": instructions,
      "tools": tools,
      "model_settings": [
        "voice": "marin",
        "speed": 1.05,
        "turn_detection": "server_vad",
        "threshold": 0.25,
        "silence_duration_ms": 400,
        "interrupt_response": true,
      ],
    ]
    send([
      "type": "session.update",
      "session": [
        "type": "realtime",
        "model": jarvisRealtimeModel,
        "instructions": instructions,
        "output_modalities": ["audio"],
        "audio": [
          "input": [
            "format": ["type": "audio/pcm", "rate": 24_000],
            "transcription": ["model": "gpt-transcribe"],
            "noise_reduction": ["type": "far_field"],
            "turn_detection": [
              "type": "server_vad",
              "threshold": 0.25,
              "prefix_padding_ms": 300,
              "silence_duration_ms": 400,
              "create_response": true,
              "interrupt_response": true,
            ],
          ],
          "output": [
            "format": ["type": "audio/pcm", "rate": 24_000],
            "voice": "marin",
            "speed": 1.05,
          ],
        ],
        "tools": tools,
        "tool_choice": "auto",
      ],
    ])
  }

  private func startAudio() throws {
    stopAudio()
    audioEngine.attach(player)
    playerAttached = true
    audioEngine.connect(player, to: audioEngine.mainMixerNode, format: playbackFormat)

    let input = audioEngine.inputNode
    if #available(macOS 10.15, *) {
      try input.setVoiceProcessingEnabled(true)
    }
    let inputFormat = input.outputFormat(forBus: 0)
    guard inputFormat.commonFormat == .pcmFormatFloat32,
      inputFormat.channelCount > 0,
      inputFormat.sampleRate >= 24_000
    else {
      throw NSError(
        domain: "Jarvis", code: 20,
        userInfo: [NSLocalizedDescriptionKey: "Unsupported microphone audio format."])
    }
    input.installTap(onBus: 0, bufferSize: 960, format: inputFormat) { [weak self] buffer, _ in
      self?.streamMicrophoneBuffer(buffer)
    }
    inputTapInstalled = true
    audioEngine.prepare()
    try audioEngine.start()
  }

  private func stopAudio() {
    if inputTapInstalled {
      audioEngine.inputNode.removeTap(onBus: 0)
      inputTapInstalled = false
    }
    if playerAttached { player.stop() }
    if audioEngine.isRunning { audioEngine.stop() }
    if playerAttached {
      audioEngine.disconnectNodeOutput(player)
      audioEngine.detach(player)
      playerAttached = false
    }
    playbackStartedAt = nil
    scheduledAudioMilliseconds = 0
    audioSendQueue.async { [weak self] in
      self?.pendingMicrophoneAudio.removeAll(keepingCapacity: true)
      self?.queuedAudioChunks.removeAll(keepingCapacity: true)
      self?.audioSendInFlight = false
    }
  }

  private func streamMicrophoneBuffer(_ buffer: AVAudioPCMBuffer) {
    guard running,
      socket != nil,
      buffer.frameLength > 0,
      let source = buffer.floatChannelData?[0]
    else { return }
    let sourceCount = Int(buffer.frameLength)
    let sampleRateRatio = buffer.format.sampleRate / 24_000
    let outputCount = max(1, Int(Double(sourceCount) / sampleRateRatio))
    var samples = [Int16](repeating: 0, count: outputCount)
    for outputIndex in 0..<outputCount {
      let sourceIndex = min(sourceCount - 1, Int(Double(outputIndex) * sampleRateRatio))
      let value = max(-1, min(1, source[sourceIndex]))
      samples[outputIndex] = Int16(value * Float(Int16.max))
    }
    logMicrophoneLevel(samples: samples)
    let data = samples.withUnsafeBytes { Data($0) }
    enqueueMicrophoneAudio(data)
  }

  private func logMicrophoneLevel(samples: [Int16]) {
    let now = Date()
    guard now.timeIntervalSince(lastMicrophoneLevelLog) >= 1, !samples.isEmpty else { return }
    lastMicrophoneLevelLog = now
    var peak: Int16 = 0
    for sample in samples {
      let magnitude = sample == .min ? Int16.max : abs(sample)
      peak = max(peak, magnitude)
    }
    NSLog("Jarvis microphone peak: %.3f", Double(peak) / Double(Int16.max))
  }

  private func enqueueMicrophoneAudio(_ data: Data) {
    let chunkBytes = 4_800  // 100 ms of 24 kHz mono PCM16 audio.
    audioSendQueue.async { [weak self] in
      guard let self, self.running else { return }
      self.pendingMicrophoneAudio.append(data)
      while self.pendingMicrophoneAudio.count >= chunkBytes {
        self.queuedAudioChunks.append(Data(self.pendingMicrophoneAudio.prefix(chunkBytes)))
        self.pendingMicrophoneAudio.removeFirst(chunkBytes)
      }
      if self.queuedAudioChunks.count > 50 {
        self.queuedAudioChunks.removeFirst(self.queuedAudioChunks.count - 50)
      }
      self.drainAudioQueue()
    }
  }

  private func drainAudioQueue() {
    guard !audioSendInFlight,
      running,
      !queuedAudioChunks.isEmpty,
      let socket
    else { return }
    let data = queuedAudioChunks.removeFirst()
    guard
      let text = jsonString([
        "type": "input_audio_buffer.append",
        "audio": data.base64EncodedString(),
      ])
    else { return }
    audioSendInFlight = true
    socket.send(.string(text)) { [weak self, weak socket] error in
      guard let self else { return }
      self.audioSendQueue.async {
        self.audioSendInFlight = false
        guard let socket, socket === self.socket else { return }
        if let error {
          NSLog("Jarvis Realtime audio send error: %@", error.localizedDescription)
          self.scheduleReconnect()
          return
        }
        self.drainAudioQueue()
      }
    }
  }

  private func handleServerEvent(_ event: [String: Any]) {
    guard let type = event["type"] as? String else { return }
    switch type {
    case "input_audio_buffer.committed":
      if let item = event["item_id"] as? String { traceLedger.committed(item) }
    case "response.created":
      if let response = event["response"] as? [String: Any], let id = response["id"] as? String {
        traceLedger.started(id)
      }
    case "session.updated":
      NSLog("Jarvis Realtime session ready")
      onStatus?("Listening — live")
    case "input_audio_buffer.speech_started":
      NSLog("Jarvis Realtime detected speech")
      speechStartedAt = Date()
      speechStoppedAt = nil
      firstAudioAt = nil
      onStatus?("Listening")
      interruptPlaybackIfNeeded()
    case "input_audio_buffer.speech_stopped":
      NSLog("Jarvis Realtime detected end of speech")
      speechStoppedAt = Date()
      onStatus?("Thinking")
    case "conversation.item.input_audio_transcription.completed":
      if let item = event["item_id"] as? String {
        traceLedger.transcribed(item, text: event["transcript"] as? String ?? "")
      }
    case "conversation.item.input_audio_transcription.failed":
      if let item = event["item_id"] as? String {
        traceLedger.transcribed(item, text: "[transcription failed]")
      }
    case "response.output_audio.delta":
      guard let itemID = event["item_id"] as? String,
        !interruptedItems.contains(itemID),
        let encoded = event["delta"] as? String,
        let data = Data(base64Encoded: encoded)
      else { return }
      if firstAudioAt == nil { firstAudioAt = Date() }
      currentAssistantContentIndex = event["content_index"] as? Int ?? 0
      playAudio(data, itemID: itemID)
      onStatus?("Speaking — interrupt anytime")
    case "response.output_audio_transcript.delta":
      guard let responseID = event["response_id"] as? String,
        let delta = event["delta"] as? String
      else { return }
      responseTranscripts[responseID, default: ""] += delta
      traceLedger.delta(responseID, text: delta)
    case "response.function_call_arguments.done":
      handleFunctionCall(event)
    case "response.done":
      finishResponse(event)
    case "error":
      let error = event["error"] as? [String: Any]
      let message = error?["message"] as? String ?? "Unknown live voice error"
      NSLog("Jarvis Realtime API error: %@", message)
      onStatus?("Live voice error: \(message)")
    default:
      break
    }
  }

  private func playAudio(_ data: Data, itemID: String) {
    guard data.count >= 2 else { return }
    if currentAssistantItemID != itemID {
      player.stop()
      player.reset()
      currentAssistantItemID = itemID
      playbackStartedAt = nil
      scheduledAudioMilliseconds = 0
    }
    let sampleCount = data.count / MemoryLayout<Int16>.size
    guard
      let buffer = AVAudioPCMBuffer(
        pcmFormat: playbackFormat, frameCapacity: AVAudioFrameCount(sampleCount)),
      let channel = buffer.floatChannelData?[0]
    else { return }
    buffer.frameLength = AVAudioFrameCount(sampleCount)
    data.withUnsafeBytes { raw in
      let samples = raw.bindMemory(to: Int16.self)
      for index in 0..<sampleCount {
        channel[index] = Float(Int16(littleEndian: samples[index])) / 32768.0
      }
    }
    player.scheduleBuffer(buffer)
    scheduledAudioMilliseconds += Int(Double(sampleCount) / 24_000.0 * 1000.0)
    if !player.isPlaying {
      playbackStartedAt = Date()
      player.play()
    }
  }

  private func interruptPlaybackIfNeeded() {
    guard player.isPlaying,
      let itemID = currentAssistantItemID,
      let started = playbackStartedAt
    else { return }
    let elapsed = max(0, Int(Date().timeIntervalSince(started) * 1000.0))
    let playedMilliseconds = min(elapsed, scheduledAudioMilliseconds)
    player.stop()
    player.reset()
    interruptedItems.insert(itemID)
    interruptionCount += 1
    send([
      "type": "conversation.item.truncate",
      "item_id": itemID,
      "content_index": currentAssistantContentIndex,
      "audio_end_ms": playedMilliseconds,
    ])
    currentAssistantItemID = nil
    playbackStartedAt = nil
    scheduledAudioMilliseconds = 0
  }

  private func handleFunctionCall(_ event: [String: Any]) {
    guard let name = event["name"] as? String,
      let callID = event["call_id"] as? String,
      let argumentsText = event["arguments"] as? String,
      let data = argumentsText.data(using: .utf8),
      let arguments = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    else { return }
    turnToolItems.append([
      "type": "function_call",
      "name": name,
      "call_id": callID,
      "arguments": arguments,
    ])
    onStatus?("Working locally")
    guard let handler = onToolCall else {
      provideToolOutput(
        callID: callID, result: ["ok": false, "error": "Local tool handler is unavailable."])
      return
    }
    handler(name, arguments) { [weak self] result in
      DispatchQueue.main.async { self?.provideToolOutput(callID: callID, result: result) }
    }
  }

  private func provideToolOutput(callID: String, result: [String: Any]) {
    turnToolItems.append([
      "type": "function_call_output",
      "call_id": callID,
      "output": result,
    ])
    let output = jsonString(result) ?? "{\"ok\":false,\"error\":\"Could not encode local result.\"}"
    send([
      "type": "conversation.item.create",
      "item": [
        "type": "function_call_output",
        "call_id": callID,
        "output": output,
      ],
    ])
    send(["type": "response.create"])
    onStatus?("Thinking")
  }

  private func finishResponse(_ event: [String: Any]) {
    guard let response = event["response"] as? [String: Any],
      let responseID = response["id"] as? String
    else { return }
    let status = response["status"] as? String ?? ""
    let assistant = (responseTranscripts.removeValue(forKey: responseID) ?? "")
      .trimmingCharacters(in: .whitespacesAndNewlines)
    var metadata: [String: Any] = [
      "interface": "realtime_voice",
      "transport": "realtime-websocket",
      "response_id": responseID,
      "interruptions": interruptionCount,
      "items": turnToolItems,
      "harness": realtimeHarness,
    ]
    if let started = speechStartedAt, let stopped = speechStoppedAt {
      metadata["speech_duration_ms"] = max(0, Int(stopped.timeIntervalSince(started) * 1000))
    }
    if let stopped = speechStoppedAt, let first = firstAudioAt {
      metadata["time_to_first_audio_ms"] = max(0, Int(first.timeIntervalSince(stopped) * 1000))
    }
    metadata["status"] =
      status == "completed" ? "completed" : status == "failed" ? "failed" : "cancelled"
    metadata["status_details"] = response["status_details"]
    traceLedger.completed(responseID, answer: assistant, metadata: metadata)
    turnToolItems.removeAll(keepingCapacity: true)
    interruptionCount = 0
    if status == "failed" {
      onStatus?("Live response failed")
    } else if !player.isPlaying {
      onStatus?("Listening — live")
    }
  }

  private func send(_ object: [String: Any]) {
    guard running, let socket, let text = jsonString(object) else { return }
    socket.send(.string(text)) { [weak self, weak socket] error in
      guard let self, let socket, socket === self.socket else { return }
      if let error {
        NSLog("Jarvis Realtime send error: %@", error.localizedDescription)
        self.scheduleReconnect()
      }
    }
  }

  private func scheduleReconnect() {
    DispatchQueue.main.async { [weak self] in
      guard let self, self.running, !self.reconnectPending else { return }
      self.reconnectPending = true
      self.keepaliveTimer?.invalidate()
      self.keepaliveTimer = nil
      self.traceLedger.disconnected()
      self.responseTranscripts.removeAll()
      self.turnToolItems.removeAll()
      self.stopAudio()
      self.socket?.cancel(with: .abnormalClosure, reason: nil)
      self.socket = nil
      self.urlSession?.invalidateAndCancel()
      self.urlSession = nil
      self.onStatus?("Reconnecting live voice")
      let work = DispatchWorkItem { [weak self] in
        guard let self, self.running else { return }
        self.reconnectPending = false
        self.connect()
      }
      self.reconnectWorkItem = work
      DispatchQueue.main.asyncAfter(deadline: .now() + 2, execute: work)
    }
  }

  private func jsonString(_ object: Any) -> String? {
    guard JSONSerialization.isValidJSONObject(object),
      let data = try? JSONSerialization.data(withJSONObject: object)
    else { return nil }
    return String(data: data, encoding: .utf8)
  }
}
