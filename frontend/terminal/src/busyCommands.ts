/**
 * Helpers for slash commands that are allowed to run while a turn streams.
 *
 * The backend decides which commands are read-only (``SlashCommand.busy_safe``)
 * and publishes the list in the ``ready`` event; the TUI only needs to know
 * whether a given input line is one of them.
 */
export function isBusySafeCommand(line: string, allowed: readonly string[]): boolean {
	const trimmed = line.trim();
	if (!trimmed.startsWith('/') || allowed.length === 0) {
		return false;
	}
	const name = trimmed.slice(1).split(/\s+/, 1)[0]?.toLowerCase();
	if (!name) {
		return false;
	}
	return allowed.some((item) => item.toLowerCase() === `/${name}`);
}
