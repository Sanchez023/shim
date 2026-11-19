import type {SelectOption} from './components/SelectModal.js';

const normalize = (value: string): string => value.toLowerCase();

/** Score a fuzzy subsequence match, or return null when characters are missing. */
function subsequenceScore(haystack: string, needle: string): number | null {
	let cursor = -1;
	let gaps = 0;
	for (const character of needle) {
		const next = haystack.indexOf(character, cursor + 1);
		if (next === -1) {
			return null;
		}
		if (cursor >= 0) {
			gaps += next - cursor - 1;
		}
		cursor = next;
	}
	return 100 - Math.min(gaps, 99);
}

/**
 * Score one option against a search query.
 *
 * Substring hits on the label beat substring hits on the description, which
 * beat scattered subsequence hits, so typing "auth" ranks "auth refactor"
 * above a session that merely contains those letters in order.
 */
export function scoreSelectOption(option: SelectOption, query: string): number | null {
	const needle = normalize(query.trim());
	if (!needle) {
		return 0;
	}
	const label = normalize(option.label);
	const description = normalize(option.description ?? '');
	const labelHit = label.indexOf(needle);
	if (labelHit >= 0) {
		return 1000 - labelHit;
	}
	const descriptionHit = description.indexOf(needle);
	if (descriptionHit >= 0) {
		return 500 - descriptionHit;
	}
	return subsequenceScore(`${label} ${description}`, needle);
}

/** Return the options matching *query*, best match first (stable otherwise). */
export function filterSelectOptions(options: SelectOption[], query: string): SelectOption[] {
	if (!query.trim()) {
		return options;
	}
	const scored: Array<{option: SelectOption; score: number; index: number}> = [];
	options.forEach((option, index) => {
		const score = scoreSelectOption(option, query);
		if (score !== null) {
			scored.push({option, score, index});
		}
	});
	scored.sort((left, right) => right.score - left.score || left.index - right.index);
	return scored.map((entry) => entry.option);
}
