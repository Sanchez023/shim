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
	timeoutMs = 3000,
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

/**
 * Emits a permission modal as soon as a turn starts, like the backend does when
 * a tool call requires confirmation mid-turn.
 */
const permissionBackendScript = [
	"console.log('OHJSON:' + JSON.stringify({type: 'ready', state: {model: 'test-model'}, commands: [], command_descriptions: {}}));",
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
	"    if (msg.type === 'submit_line') {",
	"      console.log('OHJSON:' + JSON.stringify({type: 'transcript_item', item: {role: 'user', text: msg.line}}));",
	"      console.log('OHJSON:' + JSON.stringify({type: 'tool_started', tool_name: 'bash', item: {role: 'tool', text: 'running', tool_name: 'bash'}}));",
	"      console.log('OHJSON:' + JSON.stringify({type: 'modal_request', modal: {kind: 'permission', request_id: 'req-1', tool_name: 'bash', reason: 'Mutating tools require user confirmation in default mode.'}}));",
	'    }',
	"    if (msg.type === 'permission_response') {",
	"      console.log('OHJSON:' + JSON.stringify({type: 'transcript_item', item: {role: 'log', text: 'permission-response:' + line}}));",
	"      console.log('OHJSON:' + JSON.stringify({type: 'line_complete'}));",
	'    }',
	'  }',
	'});',
].join('\n');

type Harness = {
	stdin: InkTestStdin;
	writes: string[];
	stop: () => Promise<void>;
};

async function startApp(script: string): Promise<Harness> {
	const stdin = createTestStdin();
	const stdout = createTestStdout();
	const writes: string[] = [];
	stdout.on('data', (chunk) => {
		writes.push(chunk.toString());
	});

	const instance = render(<App config={{backend_command: [process.execPath, '-e', script]}} />, {
		stdin: stdin as unknown as NodeJS.ReadStream & {fd: 0},
		stdout: stdout as unknown as NodeJS.WriteStream,
		exitOnCtrlC: false,
		debug: true,
		patchConsole: false,
	});
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

async function startTurn(harness: Harness): Promise<void> {
	for (const char of 'hi') {
		harness.stdin.write(char);
	}
	await settle();
	harness.stdin.write('\r');
	await settle();
}

test('permission modal appears above the prompt while the turn is running', async () => {
	const harness = await startApp(permissionBackendScript);
	try {
		await startTurn(harness);
		const frame = await waitForFrame(harness.writes, (value) => value.includes('Allow bash?'));
		assert.match(frame, /Allow bash\?/);
		assert.match(frame, /Mutating tools require user confirmation/);
	} finally {
		await harness.stop();
	}
});

test('y answers the permission modal with allowed=true', async () => {
	const harness = await startApp(permissionBackendScript);
	try {
		await startTurn(harness);
		await waitForFrame(harness.writes, (value) => value.includes('Allow bash?'));

		harness.stdin.write('y');
		const frame = await waitForFrame(harness.writes, (value) => value.includes('permission-response:'));
		assert.match(frame, /"type":"permission_response"/);
		assert.match(frame, /"request_id":"req-1"/);
		assert.match(frame, /"allowed":true/);
	} finally {
		await harness.stop();
	}
});

test('n answers the permission modal with allowed=false', async () => {
	const harness = await startApp(permissionBackendScript);
	try {
		await startTurn(harness);
		await waitForFrame(harness.writes, (value) => value.includes('Allow bash?'));

		harness.stdin.write('n');
		const frame = await waitForFrame(harness.writes, (value) => value.includes('permission-response:'));
		assert.match(frame, /"allowed":false/);
	} finally {
		await harness.stop();
	}
});
