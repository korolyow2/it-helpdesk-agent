// Parse only the public answer protocol; never expose model or tool event payloads.
export async function readAnswerStream(response, onText) {
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.message || `Request failed (${response.status}).`);
  }
  if (!response.headers.get("content-type")?.includes("text/event-stream") || !response.body) {
    throw new Error("The server did not return an answer stream.");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let text = "";
  let completion = null;
  try {
    while (true) {
      const chunk = await reader.read();
      buffer += decoder.decode(chunk.value, {stream: !chunk.done});
      let boundary;
      while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
        const block = buffer.slice(0, boundary.index);
        buffer = buffer.slice(boundary.index + boundary[0].length);
        const lines = block.split(/\r?\n/);
        const event = lines.find((line) => line.startsWith("event:"))?.slice(6).trim();
        const data = lines.filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trimStart()).join("\n");
        if (!event || !data) continue;
        if (completion) throw new Error("Unexpected data after the completed reply.");
        if (!["text_delta", "done", "error"].includes(event)) throw new Error("Unexpected answer event.");
        const payload = JSON.parse(data);
        if (event === "error") throw new Error(payload.message || "The request failed.");
        if (event === "text_delta") {
          if (typeof payload.text !== "string") throw new Error("Invalid answer text.");
          text += payload.text;
          await onText(payload.text);
        } else {
          if (typeof payload.reply !== "string" || !payload.reply || payload.reply !== text) {
            throw new Error("The completed reply does not match the received text.");
          }
          completion = payload;
        }
      }
      if (buffer.length > 131072) throw new Error("The answer event exceeded the supported size.");
      if (chunk.done) break;
    }
    if (!completion || buffer.trim()) throw new Error("The connection closed before a complete reply was confirmed.");
    return completion;
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
