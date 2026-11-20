import assert from 'node:assert/strict';
import test from 'node:test';

import {
	CLAUDE_DIFF,
	highlightClaudeToLines,
	highlightToLines,
	languageForPath,
} from './highlight.js';
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

test('maps file extensions to highlighter languages', () => {
	assert.equal(languageForPath('src/app.ts'), 'typescript');
	assert.equal(languageForPath('C:\\repo\\main.py'), 'python');
	assert.equal(languageForPath('README.md'), 'markdown');
	assert.equal(languageForPath('Makefile'), undefined);
});

test('Claude diff highlighting uses its fixed token palette', () => {
	const lines = highlightClaudeToLines(
		'const x: number = 1; // note\nconst s = "hi";',
		'typescript',
	);
	const first = lines[0]!;
	const span = (text: string) => first.find((item) => item.text === text);

	assert.equal(span('const')?.color, 'blueBright');
	assert.equal(span('number')?.color, 'cyan');
	assert.equal(span('1')?.color, 'green');
	assert.ok(
		first.some((item) => item.text.includes('// note') && item.color === 'green'),
		'comments are green',
	);
	const second = lines[1]!;
	assert.ok(
		second.some((item) => item.text.includes('"hi"') && item.color === 'red'),
		'strings are red',
	);
});

test('Claude diff backgrounds match the CLI diff theme', () => {
	assert.equal(CLAUDE_DIFF.addedBackground, '#225c2b');
	assert.equal(CLAUDE_DIFF.removedBackground, '#7a2936');
	assert.equal(CLAUDE_DIFF.addedWord, '#38a660');
	assert.equal(CLAUDE_DIFF.removedWord, '#b3596b');
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
