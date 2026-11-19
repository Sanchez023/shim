import assert from 'node:assert/strict';
import test from 'node:test';
import {PassThrough} from 'node:stream';
import React from 'react';
import {render} from 'ink';
import stringWidth from 'string-width';

import {ThemeProvider} from '../theme/ThemeContext.js';
import {MarkdownText} from './MarkdownText.js';

const stripAnsi = (value: string): string => value.replace(/\u001B\[[0-9;?]*[ -/]*[@-~]/g, '');
const nextLoopTurn = (): Promise<void> => new Promise((resolve) => setImmediate(resolve));

type InkTestStdout = PassThrough & {
	isTTY: boolean;
	columns: number;
	rows: number;
	cursorTo: () => boolean;
	clearLine: () => boolean;
	moveCursor: () => boolean;
};

function createTestStdout(columns = 120): InkTestStdout {
	return Object.assign(new PassThrough(), {
		isTTY: true,
		columns,
		rows: 40,
		cursorTo: () => true,
		clearLine: () => true,
		moveCursor: () => true,
	});
}

async function waitForOutputToStabilize(getOutput: () => string): Promise<string> {
	let previous = '';
	let sawOutput = false;

	for (let i = 0; i < 50; i++) {
		await nextLoopTurn();
		const current = getOutput();
		sawOutput ||= current.length > 0;
		if (sawOutput && current === previous) {
			return current;
		}

		previous = current;
	}

	throw new Error(`Ink output did not stabilize: ${JSON.stringify(previous)}`);
}

async function renderMarkdownLines(content: string, columns = 120): Promise<string[]> {
	const stdout = createTestStdout(columns);

	let output = '';
	stdout.on('data', (chunk) => {
		output += chunk.toString();
	});

	const instance = render(
		<ThemeProvider initialTheme="default">
			<MarkdownText content={content} />
		</ThemeProvider>,
		{stdout: stdout as unknown as NodeJS.WriteStream, debug: true, patchConsole: false},
	);

	const exitPromise = instance.waitUntilExit();
	const stableOutput = await waitForOutputToStabilize(() => output);
	instance.unmount();
	await exitPromise;
	await waitForOutputToStabilize(() => output);
	instance.cleanup();

	return stripAnsi(stableOutput)
		.split('\n')
		.filter(Boolean);
}

async function renderTableLines(content: string): Promise<string[]> {
	return (await renderMarkdownLines(content))
		.filter((line) => /[┌├│└]/.test(line))
		.slice(0, 5);
}

test('keeps table borders aligned when cells contain inline markdown', async () => {
	const lines = await renderTableLines('| `aa` | bb |\n|------|----|\n| c | **ddd** |');

	assert.equal(lines.length, 5);

	const widths = lines.map((line) => [...line].length);
	assert.ok(
		widths.every((width) => width === widths[0]),
		`Expected table lines to share a width, got ${JSON.stringify(
			lines.map((line, index) => ({line, width: widths[index]})),
		)}`,
	);
});

test('renders unknown inline table tokens using the visible token text fallback', async () => {
	const lines = await renderTableLines('| ![alt](https://example.com/img.png) | ok |\n|---|---|\n| x | y |');

	assert.equal(lines.length, 5);
	assert.match(lines[1] ?? '', /\balt\b/);
	assert.doesNotMatch(lines[1] ?? '', /!\[alt\]/);

	const widths = lines.map((line) => [...line].length);
	assert.ok(
		widths.every((width) => width === widths[0]),
		`Expected fallback-token table lines to share a width, got ${JSON.stringify(
			lines.map((line, index) => ({line, width: widths[index]})),
		)}`,
	);
});

test('wraps a table wider than the terminal instead of letting Ink split rows', async () => {
	const content = [
		'| Command | Description | Exit |',
		'|---|---|---|',
		`| ${'cmd-'.repeat(25)} | ${'a long explanation '.repeat(6)} | 1 |`,
		'| ok | short | 0 |',
	].join('\n');

	const lines = await renderMarkdownLines(content, 60);
	const tableLines = lines.filter((line) => /[┌├└│]/.test(line));
	assert.ok(tableLines.length > 5, `expected a bordered table, got ${JSON.stringify(lines)}`);

	const widths = tableLines.map((line) => stringWidth(line));
	assert.ok(
		widths.every((width) => width <= 60),
		`every table line must fit the terminal: ${JSON.stringify({widths, tableLines})}`,
	);
	assert.ok(
		widths.every((width) => width === widths[0]),
		`table lines must share one width: ${JSON.stringify(tableLines)}`,
	);
});

test('keeps wide-character cells aligned when a CJK table wraps', async () => {
	const content = [
		'| 项目 | 说明 | 状态 |',
		'|---|---|---|',
		`| ${'很长的项目名称'.repeat(6)} | ${'这是很长的说明文字'.repeat(5)} | 正常 |`,
	].join('\n');

	const lines = await renderMarkdownLines(content, 50);
	const tableLines = lines.filter((line) => /[┌├└│]/.test(line));
	assert.ok(tableLines.length > 5, `expected a bordered table, got ${JSON.stringify(lines)}`);

	const widths = tableLines.map((line) => stringWidth(line));
	assert.ok(widths.every((width) => width <= 50), `table must fit 50 columns: ${JSON.stringify(widths)}`);
	assert.ok(
		widths.every((width) => width === widths[0]),
		`table lines must share one width: ${JSON.stringify(tableLines)}`,
	);
});

test('falls back to a record layout when too many columns cannot fit', async () => {
	const columns = Array.from({length: 10}, (_, i) => `C${i + 1}`);
	const content = [
		`| ${columns.join(' | ')} |`,
		`|${columns.map(() => '---').join('|')}|`,
		`| ${columns.map((_, i) => `v${i + 1}`).join(' | ')} |`,
	].join('\n');

	const lines = await renderMarkdownLines(content, 30);

	assert.ok(lines.some((line) => line.includes('C1: v1')), JSON.stringify(lines));
	assert.ok(!lines.some((line) => /[┌├└]/.test(line)), JSON.stringify(lines));
});

test('preserves nested markdown structure inside blockquotes', async () => {
	const lines = await renderMarkdownLines('> - first\n> - second');

	assert.ok(lines.some((line) => line.includes('• first')), `Expected blockquote output to include a rendered bullet: ${JSON.stringify(lines)}`);
	assert.ok(lines.some((line) => line.includes('• second')), `Expected blockquote output to include the second rendered bullet: ${JSON.stringify(lines)}`);
});

test('caps long code blocks and points at the full transcript', async () => {
	const code = Array.from({length: 55}, (_, index) => `const value${index} = ${index};`).join('\n');
	const lines = await renderMarkdownLines('```typescript\n' + code + '\n```');
	const joined = lines.join('\n');

	assert.match(joined, /typescript {2}55 lines/);
	assert.match(joined, /… 15 more lines · ctrl\+o full transcript/);
	assert.match(joined, /const value39/);
	assert.doesNotMatch(joined, /const value40/);
});
