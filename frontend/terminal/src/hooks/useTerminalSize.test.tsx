import assert from 'node:assert/strict';
import test from 'node:test';
import {PassThrough} from 'node:stream';
import React from 'react';
import {Text, render} from 'ink';

import {useTerminalSize} from './useTerminalSize.js';

const stripAnsi = (value: string): string =>
	value
		.replace(/\u001B\][^\u0007]*\u0007/g, '')
		.replace(/\u001B\[[0-9;?]*[ -/]*[@-~]/g, '');

type InkTestStdout = PassThrough & {
	isTTY: boolean;
	columns: number;
	rows: number;
	cursorTo: () => boolean;
	clearLine: () => boolean;
	moveCursor: () => boolean;
};

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

function SizeProbe(): React.JSX.Element {
	const {columns, rows} = useTerminalSize();
	return <Text>{`${columns}x${rows}`}</Text>;
}

function ManyProbes({count}: {count: number}): React.JSX.Element {
	return (
		<>
			{Array.from({length: count}, (_, index) => (
				<SizeProbe key={index} />
			))}
		</>
	);
}

async function waitForFrame(output: () => string, pattern: RegExp): Promise<string> {
	const deadline = Date.now() + 2000;
	while (Date.now() < deadline) {
		const frame = stripAnsi(output());
		if (pattern.test(frame)) {
			return frame;
		}
		await new Promise((resolve) => setTimeout(resolve, 20));
	}
	return stripAnsi(output());
}

test('re-renders with the new dimensions after a terminal resize', async () => {
	const stdout = createTestStdout(120, 40);
	let output = '';
	stdout.on('data', (chunk) => {
		output += chunk.toString();
	});

	const instance = render(<SizeProbe />, {
		stdout: stdout as unknown as NodeJS.WriteStream,
		debug: true,
		patchConsole: false,
	});
	const exitPromise = instance.waitUntilExit();

	const waitFor = async (pattern: RegExp): Promise<string> => {
		const deadline = Date.now() + 2000;
		while (Date.now() < deadline) {
			const frame = stripAnsi(output);
			if (pattern.test(frame)) {
				return frame;
			}
			await new Promise((resolve) => setTimeout(resolve, 20));
		}
		return stripAnsi(output);
	};

	assert.match(await waitFor(/120x40/), /120x40/);

	stdout.columns = 60;
	stdout.rows = 20;
	stdout.emit('resize');

	assert.match(await waitFor(/60x20/), /60x20/);

	instance.unmount();
	await exitPromise;
	instance.cleanup();
	stdout.destroy();
});

test('shares one stdout resize listener across hook instances', async () => {
	const stdout = createTestStdout(100, 30);
	let output = '';
	stdout.on('data', (chunk) => {
		output += chunk.toString();
	});

	// Ink may add its own resize listener unless it detects CI; measure the
	// baseline so the assertion only tracks the hook's contribution.
	const baseline = stdout.listenerCount('resize');

	const instance = render(<ManyProbes count={20} />, {
		stdout: stdout as unknown as NodeJS.WriteStream,
		debug: true,
		patchConsole: false,
	});
	const exitPromise = instance.waitUntilExit();

	assert.match(await waitForFrame(() => output, /100x30/), /100x30/);
	// Every hook instance must share one listener instead of adding one per
	// mounted component (20 probes + Ink would exceed Node's default limit).
	assert.equal(stdout.listenerCount('resize'), baseline + 1);

	stdout.columns = 60;
	stdout.rows = 20;
	stdout.emit('resize');
	assert.match(await waitForFrame(() => output, /60x20/), /60x20/);

	instance.unmount();
	await exitPromise;
	instance.cleanup();

	// React flushes passive effect cleanups on a later macrotask, so poll
	// instead of asserting immediately after unmount.
	const deadline = Date.now() + 2000;
	while (stdout.listenerCount('resize') !== baseline && Date.now() < deadline) {
		await new Promise((resolve) => setTimeout(resolve, 10));
	}
	assert.equal(stdout.listenerCount('resize'), baseline);
	stdout.destroy();
});
