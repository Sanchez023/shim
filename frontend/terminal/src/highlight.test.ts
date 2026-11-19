import assert from 'node:assert/strict';
import test from 'node:test';

import {highlightToLines} from './highlight.js';
import {defaultTheme} from './theme/builtinThemes.js';

function text(lines: ReturnType<typeof highlightToLines>): string[] {
	return lines.map((spans) => spans.map((span) => span.text).join(''));
}

test('highlights keywords, strings and comments with theme colors', () => {
	const lines = highlightToLines('const x = "hi"; // note\nreturn x;', 'typescript', defaultTheme);

	assert.deepEqual(text(lines), ['const x = "hi"; // note', 'return x;']);
	const colors = lines[0]!.map((span) => span.color);
	assert.ok(colors.includes(defaultTheme.colors.primary), 'keyword color');
	assert.ok(colors.includes(defaultTheme.colors.success), 'string color');
	assert.ok(
		lines[0]!.some((span) => span.color === defaultTheme.colors.muted && span.italic),
		'comment color',
	);
});

test('falls back to plain text for unknown languages', () => {
	const lines = highlightToLines('a\nb', 'not-a-language', defaultTheme);

	assert.deepEqual(text(lines), ['a', 'b']);
	assert.ok(lines[0]!.every((span) => span.color === defaultTheme.colors.foreground));
});

test('expands tabs and preserves blank lines', () => {
	const lines = highlightToLines('\tif (x) {\n\n}', 'javascript', defaultTheme);

	assert.deepEqual(text(lines), ['  if (x) {', '', '}']);
});

test('renders an empty block as a single blank line', () => {
	const lines = highlightToLines('', 'python', defaultTheme);

	assert.deepEqual(text(lines), ['']);
});
