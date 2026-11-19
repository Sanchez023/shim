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

function createTestStdout(): InkTestStdout {
	return Object.assign(new PassThrough(), {
		isTTY: true,
		columns: 120,
		rows: 40,
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
	timeoutMs = 6000,
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

/** One turn: stream some text, print an out-of-band item, then finish. */
const script = [
	"const emit = (event) => console.log('OHJSON:' + JSON.stringify(event));",
	"emit({type: 'ready', state: {model: 'test-model'}, commands: [], command_descriptions: {}});",
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
	"    if (msg.type !== 'submit_line') continue;",
	"    emit({type: 'assistant_delta', message: 'Let me check '});",
	"    setTimeout(() => emit({type: 'transcript_item', item: {role: 'system', text: 'GOT STATUS'}}), 150);",
	"    setTimeout(() => emit({type: 'assistant_delta', message: 'the files.'}), 350);",
	'    setTimeout(() => {',
	"      emit({type: 'assistant_complete', message: 'Let me check the files.'});",
	"      emit({type: 'line_complete'});",
	'    }, 650);',
	'  }',
	'});',
].join('\n');

type Harness = {
	stdin: InkTestStdin;
	writes: string[];
	stop: () => Promise<void>;
};

async function startApp(): Promise<Harness> {
	const stdin = createTestStdin();
	const stdout = createTestStdout();
	const writes: string[] = [];
	stdout.on('data', (chunk) => {
		writes.push(chunk.toString());
	});

	const instance = render(
		<App config={{backend_command: [process.execPath, '-e', script]}} />,
		{
			stdin: stdin as unknown as NodeJS.ReadStream & {fd: 0},
			stdout: stdout as unknown as NodeJS.WriteStream,
			exitOnCtrlC: false,
			debug: true,
			patchConsole: false,
		},
	);
	const exitPromise = instance.waitUntilExit();
	await waitForFrame(writes, (frame) => frame.includes('test-model'));
	return {
		stdin,
		writes,
		stop: async () => {
			instance.unmount();
			await exitPromise;
			instance.cleanup();
			stdin.destroy();
			stdout.destroy();
		},
	};
}

function countOccurrences(haystack: string, needle: string): number {
	return haystack.split(needle).length - 1;
}

test('a mid-turn item lands below the text streamed before it', async () => {
	const harness = await startApp();
	try {
		for (const char of 'go') {
			harness.stdin.write(char);
		}
		await settle();
		harness.stdin.write('\r');
		await settle();

		const frame = await waitForFrame(harness.writes, (frame) =>
			frame.includes('GOT STATUS'),
		);
		const streamedAt = frame.indexOf('Let me check');
		const itemAt = frame.indexOf('GOT STATUS');
		assert.ok(streamedAt >= 0, 'the streamed text should already be visible');
		assert.ok(
			itemAt > streamedAt,
			`out-of-band item rendered above older text:\n${frame}`,
		);

		const finished = await waitForFrame(harness.writes, (frame) =>
			frame.includes('the files.'),
		);
		assert.equal(
			countOccurrences(finished, 'Let me check'),
			1,
			'the committed prefix must not be duplicated at assistant_complete',
		);
		assert.equal(countOccurrences(finished, 'the files.'), 1);
	} finally {
		await harness.stop();
	}
});
