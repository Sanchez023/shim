import assert from 'node:assert/strict';
import test from 'node:test';
import {PassThrough} from 'node:stream';
import React from 'react';
import {render} from 'ink';

import {StatusBar} from './StatusBar.js';

const stripAnsi = (value: string): string => value.replace(/\u001B\[[0-9;?]*[ -/]*[@-~]/g, '');

type TestStdout = PassThrough & {
	isTTY: boolean;
	columns: number;
	rows: number;
	cursorTo: () => boolean;
	clearLine: () => boolean;
	moveCursor: () => boolean;
};

function createStdout(columns: number): TestStdout {
	return Object.assign(new PassThrough(), {
		isTTY: true,
		columns,
		rows: 40,
		cursorTo: () => true,
		clearLine: () => true,
		moveCursor: () => true,
	});
}

const STATUS = {
	model: 'deepreasoning-ds-v4.1flash',
	permission_mode: 'Auto',
	input_tokens: 61_900,
	output_tokens: 37_900,
	cache_read_input_tokens: 3_558_500,
	context_tokens: 75_800,
	context_window_tokens: 200_000,
};

function createStdin() {
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

async function renderStatus(
	columns: number,
	status: Record<string, unknown> = STATUS,
	activeToolName?: string,
): Promise<string> {
	const stdin = createStdin();
	const stdout = createStdout(columns);
	const writes: string[] = [];
	stdout.on('data', (chunk) => writes.push(chunk.toString()));
	const instance = render(
		<StatusBar
			status={status}
			tasks={[]}
			activeToolName={activeToolName}
			lastTurnSeconds={672}
		/>,
		{
			stdin: stdin as unknown as NodeJS.ReadStream & {fd: 0},
			stdout: stdout as unknown as NodeJS.WriteStream,
			exitOnCtrlC: false,
			debug: true,
			patchConsole: false,
		},
	);
	const deadline = Date.now() + 2000;
	const hasFrame = (): boolean => writes.some((write) => write.includes('model:'));
	while (Date.now() < deadline && !hasFrame()) {
		await new Promise((resolve) => setTimeout(resolve, 20));
	}
	const frames = writes.map(stripAnsi).filter((write) => write.includes('model:'));
	const frame = frames[frames.length - 1] ?? '';
	instance.unmount();
	instance.cleanup();
	stdin.destroy();
	stdout.destroy();
	return frame;
}

function statusLines(frame: string): string[] {
	return frame
		.split('\n')
		.map((line) => line.trimEnd())
		.filter((line) => line.length > 0 && !line.startsWith('─'));
}

test('status bar keeps every field on one line at 120 columns', async () => {
	const frame = await renderStatus(120);
	const lines = statusLines(frame);
	const statusLine = lines[0] ?? '';

	assert.equal(lines.length, 1, `expected one status line, got ${lines.length}`);
	assert.match(statusLine, /model: deepreasoning-ds-v4\.1flash/);
	assert.match(statusLine, /ctx ███░░░░░ 38%/);
	assert.match(statusLine, /mode: Auto/);
	assert.match(statusLine, /tok: 61\.9k↓ 37\.9k↑/);
	assert.match(statusLine, /cache: 3\.6M/);
	assert.match(statusLine, /11:12/);
	assert.ok(statusLine.length <= 120);
});

for (const width of [90, 60, 40]) {
	test(`status bar stays on one line at ${width} columns`, async () => {
		const frame = await renderStatus(width);
		const lines = statusLines(frame);
		const statusLine = lines[0] ?? '';

		assert.equal(lines.length, 1, `expected one status line, got ${lines.length}`);
		assert.match(statusLine, /^model: deepreasoning-ds-v4\.1flash/);
		assert.ok(statusLine.length <= width, `line should fit ${width} columns: ${statusLine.length}`);
	});
}


test('plan mode calls out blocked write tools', async () => {
	const frame = await renderStatus(120, {...STATUS, permission_mode: 'Plan Mode'}, 'bash');
	assert.match(frame, /⛔ bash/);

	const readable = await renderStatus(120, {...STATUS, permission_mode: 'Plan Mode'}, 'read_file');
	assert.doesNotMatch(readable, /⛔ read_file/);
});
