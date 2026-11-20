import assert from 'node:assert/strict';
import test from 'node:test';
import {PassThrough} from 'node:stream';
import React from 'react';
import {render} from 'ink';

import {App} from './App.js';

const nextLoopTurn = (): Promise<void> => new Promise((resolve) => setImmediate(resolve));
const stripAnsi = (value: string): string => value.replace(/\u001B\[[0-9;?]*[ -/]*[@-~]/g, '');

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

function createTestStdout(columns = 120, rows = 40): InkTestStdout {
	return Object.assign(new PassThrough(), {
		isTTY: true,
		columns,
		rows,
		cursorTo: () => true,
		clearLine: () => true,
		moveCursor: () => true,
	});
}

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

const CTRL_T = '\u0014';

async function settle(): Promise<void> {
	for (let i = 0; i < 20; i += 1) {
		await nextLoopTurn();
	}
}

function lastFrame(writes: string[]): string {
	return stripAnsi(writes[writes.length - 1] ?? '');
}

async function waitForFrame(
	writes: string[],
	predicate: (frame: string) => boolean,
	timeoutMs = 4000,
): Promise<string> {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		const frame = lastFrame(writes);
		if (predicate(frame)) {
			return frame;
		}
		await new Promise((resolve) => setTimeout(resolve, 25));
		await settle();
	}
	return lastFrame(writes);
}

const script = [
	"const emit = (event) => console.log('OHJSON:' + JSON.stringify(event));",
	"const state = (value) => ({model: 'test-model', todo_panel: value});",
	"let todoPanel = 'full';",
	"emit({type: 'ready', state: state(todoPanel), commands: [], command_descriptions: {}});",
	"emit({type: 'todo_update', todo_markdown: '- [x] inspect repository\\n- [ ] write plan'});",
	'process.stdin.resume();',
	"let buf = '';",
	"process.stdin.on('data', (chunk) => {",
	'  buf += chunk.toString();',
	'  let idx;',
	"  while ((idx = buf.indexOf('\\n')) >= 0) {",
	'    const line = buf.slice(0, idx); buf = buf.slice(idx + 1);',
	'    if (!line) continue;',
	'    let msg;',
	'    try { msg = JSON.parse(line); } catch { continue; }',
	"    if (msg.type === 'set_ui_preference' && msg.key === 'todo_panel') {",
	'      todoPanel = msg.value;',
	"      emit({type: 'state_snapshot', state: state(todoPanel)});",
	'    }',
	'  }',
	'});',
].join('\n');

type Harness = {
	stdin: InkTestStdin;
	writes: string[];
	/** Terminal model of everything Ink wrote: use `visible()` for "what the user sees". */
	screen: Screen;
	stop: () => Promise<void>;
};

/**
 * Minimal terminal model. Ink erases its previous frame with `eraseLines()`
 * (`\x1b[2K` + `\x1b[1A` steps), so replaying the raw stream gives the screen
 * the user actually ends up looking at — which is what the visibility
 * assertions are about.
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

	/** The last `rows` lines, i.e. what fits on the terminal. */
	visible(rows: number): string {
		return this.lines
			.slice(Math.max(0, this.lines.length - rows))
			.map((line) => stripAnsi(line))
			.join('\n');
	}
}

async function startApp(
	script: string,
	columns = 120,
	options: {debug?: boolean; rows?: number} = {},
): Promise<Harness> {
	const stdin = createTestStdin();
	const stdout = createTestStdout(columns, options.rows ?? 40);
	const writes: string[] = [];
	const screen = new Screen();
	stdout.on('data', (chunk) => {
		writes.push(chunk.toString());
		screen.write(chunk.toString());
	});

	const instance = render(
		<App config={{backend_command: [process.execPath, '-e', script]}} />,
		{
			stdin: stdin as unknown as NodeJS.ReadStream & {fd: 0},
			stdout: stdout as unknown as NodeJS.WriteStream,
			exitOnCtrlC: false,
			debug: options.debug ?? true,
			patchConsole: false,
		},
	);
	const exitPromise = instance.waitUntilExit();
	await waitForFrame(writes, (frame) => frame.includes('test-model'));
	return {
		stdin,
		writes,
		screen,
		stop: async () => {
			instance.unmount();
			await exitPromise;
			instance.cleanup();
			stdin.destroy();
			stdout.destroy();
		},
	};
}

test('the persisted todo_panel value wins on startup', async () => {
	const hiddenScript = [
		"const emit = (event) => console.log('OHJSON:' + JSON.stringify(event));",
		"emit({type: 'ready', state: {model: 'test-model', todo_panel: 'hidden'}, commands: [], command_descriptions: {}});",
		"emit({type: 'todo_update', todo_markdown: '- [x] inspect repository\\n- [ ] write plan'});",
		'process.stdin.resume();',
		"process.stdin.on('data', () => {});",
	].join('\n');
	const harness = await startApp(hiddenScript);
	try {
		const frame = await waitForFrame(harness.writes, (frame) =>
			frame.includes('ctrl+t: show the todo panel'),
		);
		assert.doesNotMatch(frame, /1\/2 done/);
		assert.doesNotMatch(frame, /write plan/);
	} finally {
		await harness.stop();
	}
});

test('rapid ctrl+t taps advance before the backend replies', async () => {
	const slowScript = [
		"const emit = (event) => console.log('OHJSON:' + JSON.stringify(event));",
		"const state = (value) => ({model: 'test-model', todo_panel: value});",
		"let todoPanel = 'full';",
		"emit({type: 'ready', state: state(todoPanel), commands: [], command_descriptions: {}});",
		"emit({type: 'todo_update', todo_markdown: '- [x] inspect repository\\n- [ ] write plan'});",
		'process.stdin.resume();',
		"let buf = '';",
		"process.stdin.on('data', (chunk) => {",
		'  buf += chunk.toString();',
		'  let idx;',
		"  while ((idx = buf.indexOf('\\n')) >= 0) {",
		'    const line = buf.slice(0, idx); buf = buf.slice(idx + 1);',
		'    if (!line) continue;',
		'    let msg;',
		'    try { msg = JSON.parse(line); } catch { continue; }',
		"    if (msg.type !== 'set_ui_preference') continue;",
		'    todoPanel = msg.value;',
		"    setTimeout(() => emit({type: 'state_snapshot', state: state(todoPanel)}), 400);",
		'  }',
		'});',
	].join('\n');
	const harness = await startApp(slowScript);
	try {
		await waitForFrame(harness.writes, (frame) => frame.includes('write plan'));
		harness.stdin.write(CTRL_T);
		await settle();
		harness.stdin.write(CTRL_T);
		await settle();
		const frame = await waitForFrame(harness.writes, (frame) =>
			frame.includes('ctrl+t: show the todo panel'),
		);
		assert.doesNotMatch(frame, /1\/2 done/);
	} finally {
		await harness.stop();
	}
});

test('ctrl+t cycles the todo panel full -> compact -> hidden (persisted by the backend)', async () => {
	const harness = await startApp(script);
	try {
		const full = await waitForFrame(harness.writes, (frame) => frame.includes('write plan'));
		assert.match(full, /1\/2 done/);
		assert.match(full, /\[ctrl\+t compact\]/);

		harness.stdin.write(CTRL_T);
		await settle();
		const compact = await waitForFrame(harness.writes, (frame) => frame.includes('[ctrl+t hide]'));
		assert.match(compact, /1\/2 done/);
		assert.doesNotMatch(compact, /write plan/);

		harness.stdin.write(CTRL_T);
		await settle();
		const hidden = await waitForFrame(harness.writes, (frame) =>
			frame.includes('ctrl+t: show the todo panel'),
		);
		assert.doesNotMatch(hidden, /1\/2 done/);
		assert.doesNotMatch(hidden, /write plan/);

		harness.stdin.write(CTRL_T);
		await settle();
		const restored = await waitForFrame(harness.writes, (frame) => frame.includes('write plan'));
		assert.match(restored, /1\/2 done/);
	} finally {
		await harness.stop();
	}
});

// A tall checklist used to squeeze the live frame until the just-finished answer
// was clipped or left behind in scrollback, where the fixed-height frame hides
// everything but its last few rows.
const answerLines = Array.from({length: 6}, (_, i) => `FINAL ANSWER LINE ${i + 1}`);
const finishedTurnScript = [
	"const emit = (event) => console.log('OHJSON:' + JSON.stringify(event));",
	"emit({type: 'ready', state: {model: 'test-model', todo_panel: 'full'}, commands: [], command_descriptions: {}});",
	"for (let i = 0; i < 12; i += 1) emit({type: 'transcript_item', item: {role: 'assistant', text: 'old-' + i}});",
	`const answer = ${JSON.stringify(answerLines.join('\n'))};`,
	'emit({type: \'assistant_delta\', message: answer});',
	"emit({type: 'assistant_complete', message: answer});",
	"emit({type: 'line_complete'});",
	"const items = Array.from({length: 20}, (_, i) => '- [ ] task-' + (i + 1)).join('\\n');",
	"emit({type: 'todo_update', todo_markdown: items});",
	'process.stdin.resume();',
].join('\n');

test('a finished answer stays visible when a long checklist is showing', async () => {
	// 70 columns puts the checklist inline (below the conversation), where
	// squeezing the frame actually hides the newest rows. `debug: false` keeps
	// Ink's real erase-based painting so the screen model below matches what a
	// terminal would show.
	const harness = await startApp(finishedTurnScript, 70, {debug: false, rows: 24});
	try {
		const seen = await waitForFrame(harness.writes, (value) => value.includes('task-1'));
		assert.ok(seen.includes('task-1'));
		// Let React run the effects that move settled items into `<Static>`.
		await new Promise((resolve) => setTimeout(resolve, 200));
		await settle();

		const visible = harness.screen.visible(24);
		// The answer must still be on screen after the turn settles: either in full
		// or with its head folded behind an "… earlier lines" hint.  The conclusion
		// is what the user waited for, so the folded part must never be the tail.
		assert.ok(
			visible.includes(answerLines[answerLines.length - 1]),
			`the end of the answer should still be on screen after the turn finished:\n${visible}`,
		);
		const shown = answerLines.filter((line) => visible.includes(line)).length;
		assert.ok(
			shown === answerLines.length || /earlier lines/.test(visible),
			`only ${shown}/${answerLines.length} answer lines are visible and there is no truncation hint`,
		);
		// The checklist is capped so it cannot eat the frame the answer lives in.
		assert.match(visible, /more/);
	} finally {
		await harness.stop();
	}
});
