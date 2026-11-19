/**
 * Wrap terminal writes in DEC synchronized-output markers (mode 2026).
 *
 * Ink redraws the whole frame with ``eraseLines(n) + output`` for every state
 * change. On Windows terminals that repaint the erased region, that is visible
 * as a full-screen flash on every key press (notably Up/Down history and
 * transcript scrolling). Terminals supporting mode 2026 apply both writes in
 * one atomic paint; terminals that do not support it ignore the private mode.
 */

export function installSynchronizedOutput(stream: NodeJS.WriteStream): () => void {
	if (!stream.isTTY) {
		return () => undefined;
	}

	const originalWrite = stream.write.bind(stream) as typeof stream.write;
	const wrapped = ((
		chunk: string | Uint8Array,
		encoding?: BufferEncoding | ((error?: Error | null) => void),
		callback?: (error?: Error | null) => void,
	) => {
		if (typeof chunk !== 'string' || chunk.length === 0) {
			return originalWrite(chunk as never, encoding as never, callback as never);
		}
		return originalWrite(
			`\u001B[?2026h${chunk}\u001B[?2026l`,
			encoding as never,
			callback as never,
		);
	}) as typeof stream.write;

	stream.write = wrapped;
	return () => {
		stream.write = originalWrite as typeof stream.write;
	};
}
