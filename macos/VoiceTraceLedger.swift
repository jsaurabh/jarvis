import Foundation

// Correlate by server IDs: transcription can arrive after a response or a new turn.
final class VoiceTraceLedger {
  var emit: ((String, String, [String: Any]) -> Void)?
  private var currentInput: String?
  private var transcripts: [String: String] = [:]
  private var inputs: [String: String] = [:]
  private var pending: [String: (String, [String: Any])] = [:]
  private var partial: [String: String] = [:]
  private var emitted = Set<String>()
  private var order: [String] = []

  func committed(_ input: String) { currentInput = input }
  func delta(_ response: String, text: String) {
    partial[response, default: ""] += text
  }
  func started(_ response: String) {
    if let input = currentInput { inputs[response] = input }
  }
  func transcribed(_ input: String, text: String) {
    transcripts[input] = text
    for response in Array(pending.keys) where inputs[response] == input {
      if let (answer, metadata) = pending.removeValue(forKey: response) {
        completed(response, answer: answer, metadata: metadata)
      }
    }
  }
  func completed(_ response: String, answer: String, metadata: [String: Any]) {
    guard !emitted.contains(response) else { return }
    if inputs[response] == nil { started(response) }
    guard let input = inputs[response], let user = transcripts[input] else {
      pending[response] = (answer, metadata)
      return
    }
    emitted.insert(response)
    pending.removeValue(forKey: response)
    partial.removeValue(forKey: response)
    order.append(response)
    if order.count > 256 {
      let old = order.removeFirst()
      emitted.remove(old)
      if let input = inputs.removeValue(forKey: old), input != currentInput,
        !inputs.values.contains(input)
      {
        transcripts.removeValue(forKey: input)
      }
    }
    var record = metadata
    record["input_item_id"] = input
    record["response_id"] = response
    emit?(user, answer, record)
  }
  func disconnected() {
    for response in Array(inputs.keys) where !emitted.contains(response) {
      let value = pending[response]
      var metadata = value?.1 ?? [:]
      metadata["status"] = "cancelled"
      metadata["error"] = "Voice connection closed before turn completed"
      // An unavailable transcription is explicitly represented, never borrowed from another turn.
      if let input = inputs[response], transcripts[input] == nil {
        transcripts[input] = "[transcription unavailable]"
      }
      completed(response, answer: value?.0 ?? partial[response] ?? "", metadata: metadata)
    }
    currentInput = nil
    transcripts.removeAll()
    inputs.removeAll()
    pending.removeAll()
    partial.removeAll()
    emitted.removeAll()
    order.removeAll()
  }
}
