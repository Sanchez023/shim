/**
 * Return the next transcript offset, or ``null`` when the offset would not
 * change. Callers use ``null`` to skip the state update entirely: repeatedly
 * paging down at the bottom used to re-render the whole Ink frame and flicker.
 */
export function nextTranscriptOffset(
	current: number,
	max: number,
	delta: number,
): number | null {
	const upper = Math.max(0, max);
	const next = Math.min(upper, Math.max(0, current + delta));
	return next === current ? null : next;
}
