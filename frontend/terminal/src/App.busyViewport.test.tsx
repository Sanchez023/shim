import assert from 'node:assert/strict';
import test from 'node:test';
import {PassThrough} from 'node:stream';
import React from 'react';
import {render} from 'ink';

import {App} from './App.js';

type InkTestStdout = PassThrough & {
	isTTY: boolean;
	columns: number;
	rows: number;
	cursorTo: () => boolean;
	clearLine: () => boolean;
	moveCursor: () => boolean;
};
type InkTestStdin = PassThrough & {
	isTTY: boolean;
	setRawMode: (_mode: boolean) => void;
	resume: () => InkTestStdin;
	pause: () => InkTestStdin;
	ref: () => InkTestStdin;
	unref: () => InkTestStdin;
};

const stripAnsi = (value: string): string =>
	value.replace(/\u001B\[[0-9;?]*[ -/]*[@-~]/g, '');

function createTestStdin(): InkTestStdin {
	return Object.assign(new PassThrough(), {
		isTTY: true,
		setRawMode: () => undefined,
		resume() {
			return this;
		},
		pause() {
			return this;
		},
		ref() {
			return this;
		},
		unref() {
			return this;
		},
	});
}

function createTestStdout(columns: number, rows: number): InkTestStdout {
	return Object.assign(new PassThrough(), {
		isTTY: true,
		columns,
		rows,
		cursorTo: () => true,
		clearLine: () => true,
		moveCursor: () => true,
	});
}

/**
 * Minimal terminal model. Ink erases its previous frame with `eraseLines()`
 * (`\x1b[2K` + `\x1b[1A` steps), so replaying the raw stream gives the screen
 * the user actually ends up looking at.
 */
class Screen {
	lines: string[] = [];
	cursor = 0;

	write(raw: string): void {
		let index = 0;
		while (index < raw.length) {
			const rest = raw.slice(index);
			const escape = /^\u001B\[(\d*)([A-Za-z])/.exec(rest);
			if (escape) {
				const amount = escape[1] === '' ? 1 : Number(escape[1]);
				const code = escape[2];
				if (code === 'A') {
					this.cursor = Math.max(0, this.cursor - amount);
				} else if (code === 'K') {
					if (this.cursor < this.lines.length) {
						this.lines[this.cursor] = '';
					}
				} else if (code === 'J') {
					this.lines = [];
					this.cursor = 0;
				} else if (code === 'H') {
					this.cursor = 0;
				}
				index += escape[0].length;
				continue;
			}
			const newline = rest.indexOf('\n');
			const chunk = newline === -1 ? rest : rest.slice(0, newline);
			while (this.lines.length <= this.cursor) {
				this.lines.push('');
			}
			this.lines[this.cursor] = (this.lines[this.cursor] ?? '') + chunk;
			if (newline === -1) {
				index = raw.length;
				continue;
			}
			this.cursor += 1;
			index += newline + 1;
		}
	}

	visible(rows: number): string {
		return this.lines
			.slice(Math.max(0, this.lines.length - rows))
			.map((line) => stripAnsi(line))
			.join('\n');
	}
}

async function settle(): Promise<void> {
	for (let i = 0; i < 20; i += 1) {
		await new Promise((resolve) => setImmediate(resolve));
	}
}

async function waitFor(
	screen: Screen,
	rows: number,
	predicate: (view: string) => boolean,
	timeoutMs = 8000,
): Promise<string> {
	const deadline = Date.now() + timeoutMs;
	let view = screen.visible(rows);
	while (Date.now() < deadline) {
		await settle();
		view = screen.visible(rows);
		if (predicate(view)) {
			return view;
		}
		await new Promise((resolve) => setTimeout(resolve, 25));
	}
	return view;
}

const answerLines = Array.from(
	{length: 40},
	(_, i) => `LINE-${String(i + 1).padStart(2, '0')}`,
);
const newestAnswerLine = answerLines[answerLines.length - 1];

const script = [
	"const emit = (event) => console.log('OHJSON:' + JSON.stringify(event));",
	"emit({type: 'ready', state: {model: 'test-model'}, commands: [], command_descriptions: {}});",
	"for (let i = 1; i <= 6; i += 1) {",
	"  emit({type: 'transcript_item', item: {role: i % 2 ? 'user' : 'assistant', text: 'HIST-' + i}});",
	'}',
	'process.stdin.resume();',
	"let buf = '';",
	"process.stdin.on('data', (chunk) => {",
	'  buf += chunk.toString();',
	'  let idx;',
	"  while ((idx = buf.indexOf('\\n')) >= 0) {",
	'    const line = buf.slice(0, idx); buf = buf.slice(idx + 1);',
	'    if (!line.trim()) continue;',
	'    let msg;',
	'    try { msg = JSON.parse(line); } catch { continue; }',
	"    if (msg.type !== 'submit_line') continue;",
	`    emit({type: 'assistant_delta', message: ${JSON.stringify(answerLines.join('\n'))}});`,
	'  }',
	'});',
].join('\n');

const completedAnswerScript = [
	"const emit = (event) => console.log('OHJSON:' + JSON.stringify(event));",
	"emit({type: 'ready', state: {model: 'test-model'}, commands: [], command_descriptions: {}});",
	"for (let i = 1; i <= 6; i += 1) {",
	"  emit({type: 'transcript_item', item: {role: i % 2 ? 'user' : 'assistant', text: 'HIST-' + i}});",
	'}',
	'process.stdin.resume();',
	"let buf = '';",
	"process.stdin.on('data', (chunk) => {",
	'  buf += chunk.toString();',
	'  let idx;',
	"  while ((idx = buf.indexOf('\\n')) >= 0) {",
	'    const line = buf.slice(0, idx); buf = buf.slice(idx + 1);',
	'    if (!line.trim()) continue;',
	'    let msg;',
	'    try { msg = JSON.parse(line); } catch { continue; }',
	"    if (msg.type !== 'submit_line') continue;",
	`    const answer = ${JSON.stringify(answerLines.join('\n'))};`,
	"    emit({type: 'assistant_delta', message: answer});",
	"    emit({type: 'assistant_complete', message: answer});",
	"    emit({type: 'line_complete'});",
	'  }',
	'});',
].join('\n');

test('a finished answer keeps its tail instead of folding the conclusion away', async () => {
	const columns = 70;
	const rows = 24;
	const stdin = createTestStdin();
	const stdout = createTestStdout(columns, rows);
	const screen = new Screen();
	stdout.on('data', (chunk) => {
		screen.write(chunk.toString());
	});

	const instance = render(
		<App config={{backend_command: [process.execPath, '-e', completedAnswerScript]}} />,
		{
			stdin: stdin as unknown as NodeJS.ReadStream & {fd: 0},
			stdout: stdout as unknown as NodeJS.WriteStream,
			exitOnCtrlC: false,
			debug: false,
			patchConsole: false,
		},
	);
	const exitPromise = instance.waitUntilExit();
	try {
		await waitFor(screen, rows, (view) => view.includes('test-model'));
		for (const char of 'go') {
			stdin.write(char);
		}
		await settle();
		stdin.write('\r');
		const view = await waitFor(screen, rows, (value) => value.includes('LINE-40'));
		await settle();

		// Once the turn completes the transcript item replaces the streaming
		// buffer; it must keep the same tail alignment instead of flipping to a
		// head clamp that hides the conclusion behind "… N more lines".
		assert.match(view, /LINE-40/, `the finished answer's conclusion should stay visible:\n${view}`);
		assert.match(view, /earlier lines/, 'the folded head needs a hint');
		assert.doesNotMatch(view, /\d+ more lines/, 'the conclusion must not be folded away');
		assert.doesNotMatch(view, /LINE-01/, 'the head of a long answer should not crowd out its tail');
	} finally {
		instance.unmount();
		await exitPromise;
		instance.cleanup();
		stdin.destroy();
		stdout.destroy();
	}
});

test('a streaming answer keeps its tail and leaves room for the conversation', async () => {
	// 70 columns keeps the checklist inline (below the conversation) and 24 rows
	// is the small window where the live answer used to eat the whole frame.
	const columns = 70;
	const rows = 24;
	const stdin = createTestStdin();
	const stdout = createTestStdout(columns, rows);
	const screen = new Screen();
	stdout.on('data', (chunk) => {
		screen.write(chunk.toString());
	});

	const instance = render(
		<App config={{backend_command: [process.execPath, '-e', script]}} />,
		{
			stdin: stdin as unknown as NodeJS.ReadStream & {fd: 0},
			stdout: stdout as unknown as NodeJS.WriteStream,
			exitOnCtrlC: false,
			debug: false,
			patchConsole: false,
		},
	);
	const exitPromise = instance.waitUntilExit();
	try {
		await waitFor(screen, rows, (view) => view.includes('test-model'));
		for (const char of 'go') {
			stdin.write(char);
		}
		await settle();
		stdin.write('\r');
		const view = await waitFor(screen, rows, (value) => /LINE-\d\d/.test(value));

		// The newest text is what the user is reading while the turn streams; the
		// buffer used to keep its head and hide everything being written.
		assert.ok(
			view.includes(newestAnswerLine),
			`the newest streamed line should be visible:\n${view}`,
		);
		assert.match(view, /earlier lines/, 'folded stream rows need a hint');
		assert.doesNotMatch(
			view,
			/LINE-01/,
			'the head of a long stream should not crowd out its tail',
		);
		// The answer used to squeeze the transcript to one row, which reads as
		// "my earlier conversation disappeared".
		assert.match(view, /HIST-5/, `recent history should stay on screen:\n${view}`);
		assert.match(view, /HIST-6/, 'the newest history item should stay on screen');
	} finally {
		instance.unmount();
		await exitPromise;
		instance.cleanup();
		stdin.destroy();
		stdout.destroy();
	}
});
